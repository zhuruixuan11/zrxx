"""baostock 统一网关 —— 全进程唯一会话 + 全局限速 + 熔断降级。

为什么需要它
------------
baostock 是进程级单例（底层只有一条 socket）。项目里原先有 4 处代码各自
``bs.login()`` / ``bs.logout()``：行情工具、回测取数、选股扫描、大盘信号。
任何一处 logout 都会把别人正持有的会话踢掉，表现为
``login failed: 网络接收错误``；并发越高越容易触发服务端限流。

本模块把四路调用收敛到一条通道：

1. **单会话复用**：全进程只 login 一次，失败后按冷却窗口节流重试
2. **全局限速**：查询串行执行 + 最小间隔，避免打爆服务端
3. **熔断器**：连续失败达到阈值后进入冷却期，期内所有调用立即返回 None，
   上层直接走新浪兜底，不再空等超时，也不会继续加剧限流

调用约定
--------
``fetch_bars()`` 返回：

- ``None``  → baostock 当前不可用（熔断 / 登录失败 / 超时），调用方应走兜底
- ``[]``    → 查询成功但没有数据（如北交所、停牌）
- ``[...]`` → 正常数据
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

# ---------- 可调参数 ----------
_MIN_QUERY_INTERVAL = 0.05   # 两次查询之间的最小间隔（秒），防止瞬间打满服务端
_MIN_LOGIN_INTERVAL = 3.0    # 两次 login 之间的最小间隔（秒），失败重登同样受限
_LOGIN_COOLDOWN = 5.0        # login 失败后的重试冷却（秒）
_FAIL_THRESHOLD = 6          # 连续失败多少次触发熔断
_BREAK_SECONDS = 90.0        # 熔断持续时间（秒）
_STUCK_SECONDS = 30.0        # 单次查询占用网关超过该时长视为卡死
_DEFAULT_TIMEOUT = 20.0

# 网关锁保证同一时刻只有一个 baostock 调用；多开几个线程只是让
# 「批量扫描」进行中的单只行情查询能在批次间隙插进来，不至于排队百秒。
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="baostock")

# ---------- 内部状态 ----------
_gate = threading.Lock()      # 串行化 login / query（含跨线程）
_state = threading.Lock()     # 保护下面的熔断统计

_logged_in = False
_last_login_at = 0.0
_last_login_fail_at = 0.0
_last_query_at = 0.0
_fail_streak = 0
_break_until = 0.0
_last_error = ""
_login_count = 0
_query_count = 0

# ---------- 短期结果缓存 ----------
# 同一只股票常被多处重复取数（如一只股票跑 3 个策略回测），
# 命中缓存能直接省掉 2/3 的请求，是降低限流压力最有效的一招。
_ROW_CACHE: Dict[tuple, tuple] = {}
_ROW_CACHE_TTL = 300.0   # 5 分钟，与日线更新频率匹配
_ROW_CACHE_MAX = 1024


def _cache_key(code, fields, start_date, end_date, frequency, adjustflag) -> tuple:
    return (code, fields, start_date, end_date, frequency, adjustflag)


def _cache_get(key: tuple) -> Optional[List[List[str]]]:
    with _state:
        hit = _ROW_CACHE.get(key)
        if hit is None:
            return None
        ts, rows = hit
        if time.monotonic() - ts > _ROW_CACHE_TTL:
            _ROW_CACHE.pop(key, None)
            return None
        return rows


def _cache_set(key: tuple, rows: List[List[str]]) -> None:
    with _state:
        if len(_ROW_CACHE) >= _ROW_CACHE_MAX:
            # 简单淘汰：清掉最早的四分之一，避免无界增长
            for k in list(_ROW_CACHE)[: _ROW_CACHE_MAX // 4]:
                _ROW_CACHE.pop(k, None)
        _ROW_CACHE[key] = (time.monotonic(), rows)


# baostock 只认 sh./sz. 前缀的 6 位数字；格式不对时它会直接抛异常，
# 若不预校验，一个脏代码就会把整批扫描打断（甚至被误判成会话故障）。
_CODE_RE = re.compile(r"^(sh|sz)\.\d{6}$")


def _valid_code(code: str) -> bool:
    return bool(_CODE_RE.match(code))


# ---------- 熔断状态 ----------
def is_open() -> bool:
    """熔断是否生效（生效期内不要调用 baostock，直接走兜底）。"""
    with _state:
        return _break_until > time.monotonic()


def stats() -> Dict[str, Any]:
    """当前网关状态，便于排查。"""
    with _state:
        now = time.monotonic()
        return {
            "logged_in": _logged_in,
            "circuit_open": _break_until > now,
            "break_remaining_s": round(max(0.0, _break_until - now), 1),
            "fail_streak": _fail_streak,
            "fail_threshold": _FAIL_THRESHOLD,
            "logins": _login_count,
            "queries": _query_count,
            "last_error": _last_error,
        }


def _record_success() -> None:
    global _fail_streak, _last_error
    with _state:
        _fail_streak = 0
        _last_error = ""


def _record_failure(reason: str) -> None:
    global _fail_streak, _break_until, _last_error
    with _state:
        _fail_streak += 1
        _last_error = reason
        if _fail_streak >= _FAIL_THRESHOLD and _break_until <= time.monotonic():
            _break_until = time.monotonic() + _BREAK_SECONDS
            logger.warning(
                f"[baostock] 连续失败 {_fail_streak} 次，熔断 {_BREAK_SECONDS:.0f}s，"
                f"期间所有请求改走新浪兜底（最后一次错误：{reason}）"
            )


def _drop_session(reason: str) -> None:
    """丢弃当前会话（不调用 logout，避免连接已死时再次阻塞）。"""
    global _logged_in, _last_error
    with _state:
        _logged_in = False
        _last_error = reason


def reset() -> None:
    """外部强制重置（会话毒化后调用）：清状态并打开熔断，让调用方快速降级。"""
    _drop_session("reset")
    _record_failure("session reset")


# ---------- 同步实现（在线程池里跑）----------
def _login_locked(bs) -> bool:
    """在已持有 gate 的前提下登录。"""
    global _logged_in, _last_login_at, _last_login_fail_at, _login_count

    now = time.monotonic()
    if _logged_in:
        return True
    # 刚失败过就先别重试，避免限流期疯狂 login
    if now - _last_login_fail_at < _LOGIN_COOLDOWN:
        return False
    # 登录节流：无论成功与否，两次 login 之间至少间隔 _MIN_LOGIN_INTERVAL
    # （失败重登不该变成 login 风暴，这正是服务端限流的直接诱因）
    if now - _last_login_at < _MIN_LOGIN_INTERVAL:
        return False
    _last_login_at = now

    try:
        lg = bs.login()
    except Exception as e:  # noqa: BLE001
        _last_login_fail_at = now
        logger.warning(f"[baostock] login 异常：{e}")
        return False

    if lg.error_code != "0":
        _last_login_fail_at = now
        logger.warning(f"[baostock] login 失败：{lg.error_msg}")
        return False

    _logged_in = True
    _login_count += 1
    logger.info("[baostock] 登录成功（全进程复用）")
    return True


def _respect_interval() -> None:
    global _last_query_at
    wait = _MIN_QUERY_INTERVAL - (time.monotonic() - _last_query_at)
    if wait > 0:
        time.sleep(wait)
    _last_query_at = time.monotonic()


def _query_blocking(
    code: str,
    fields: str,
    start_date: str,
    end_date: str,
    frequency: str,
    adjustflag: str,
) -> Optional[List[List[str]]]:
    """同步查询。返回 None 表示网关不可用，[] 表示无数据。"""
    global _gate

    if not _valid_code(code):
        # 脏代码直接当作无数据，不占用网关、不影响会话
        return []

    gate = _gate
    if not gate.acquire(timeout=_STUCK_SECONDS):
        # 上一个查询卡死超过阈值：换一把新锁，旧线程自行退出后释放旧对象
        _drop_session("gate stuck")
        _record_failure("query stuck > %.0fs" % _STUCK_SECONDS)
        _gate = threading.Lock()
        return None

    try:
        import baostock as bs
    except ImportError:
        return None

    try:
        if not _login_locked(bs):
            return None

        _respect_interval()
        try:
            rs = bs.query_history_k_data_plus(
                code, fields,
                start_date=start_date, end_date=end_date,
                frequency=frequency, adjustflag=adjustflag,
            )
        except Exception as e:  # noqa: BLE001
            _drop_session(f"query exception: {e}")
            return None

        if rs is None or rs.error_code != "0":
            msg = getattr(rs, "error_msg", "unknown")
            # 服务端返回错误码通常意味着会话已失效，丢弃后下次重登
            _drop_session(f"query failed: {msg}")
            return None

        rows: List[List[str]] = []
        while rs.next():
            rows.append(rs.get_row_data())
        return rows
    finally:
        gate.release()


# ---------- 异步对外接口 ----------
async def fetch_rows(
    code: str,
    fields: str,
    start_date: str,
    end_date: str,
    frequency: str = "d",
    adjustflag: str = "3",
    timeout: float = _DEFAULT_TIMEOUT,
) -> Optional[List[List[str]]]:
    """低层查询，返回原始行。None=不可用，[]=无数据。"""
    global _query_count

    key = _cache_key(code, fields, start_date, end_date, frequency, adjustflag)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    if is_open():
        return None

    loop = asyncio.get_running_loop()
    try:
        rows = await asyncio.wait_for(
            loop.run_in_executor(
                _EXECUTOR, _query_blocking,
                code, fields, start_date, end_date, frequency, adjustflag,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        _drop_session("asyncio timeout")
        _record_failure(f"timeout > {timeout:.0f}s ({code})")
        return None

    if rows is None:
        _record_failure(f"query unavailable ({code})")
        return None

    _record_success()
    with _state:
        _query_count += 1
    _cache_set(key, rows)
    return rows


async def fetch_bars(
    code: str,
    start_date: str,
    end_date: str,
    frequency: str = "d",
    adjustflag: str = "3",
    timeout: float = _DEFAULT_TIMEOUT,
    extra_fields: str = "",
) -> Optional[List[Dict[str, Any]]]:
    """拉日线/分钟线。返回 None=不可用，[]=无数据，否则为 bar 列表（按时间升序）。

    extra_fields: 逗号分隔的附加字段（如 "preclose,tradestatus,isST"），
    会原样透传给 baostock 并挂在每个 bar 上（数值转 float，tradestatus 保留字符串）。
    """
    extra_keys = [f.strip() for f in extra_fields.split(",") if f.strip()]
    fields = "date,open,high,low,close,volume" + ("," + ",".join(extra_keys) if extra_keys else "")
    rows = await fetch_rows(
        code, fields,
        start_date, end_date, frequency, adjustflag, timeout,
    )
    if rows is None:
        return None

    bars: List[Dict[str, Any]] = []
    for r in rows:
        try:
            if len(r) < 6:
                continue
            bar: Dict[str, Any] = {
                "date": r[0],
                "open": float(r[1]) if r[1] else 0.0,
                "high": float(r[2]) if r[2] else 0.0,
                "low": float(r[3]) if r[3] else 0.0,
                "close": float(r[4]) if r[4] else 0.0,
                "volume": float(r[5]) if r[5] else 0.0,
            }
            for idx, key in enumerate(extra_keys, start=6):
                raw = r[idx] if idx < len(r) else ""
                if key == "tradestatus":
                    bar[key] = str(raw or "1")
                else:
                    try:
                        bar[key] = float(raw) if raw not in ("", None) else 0.0
                    except (TypeError, ValueError):
                        bar[key] = 0.0
            bars.append(bar)
        except (ValueError, IndexError):
            continue
    return bars


async def fetch_bars_many(
    codes: Sequence[str],
    start_date: str,
    end_date: str,
    frequency: str = "d",
    adjustflag: str = "3",
    timeout: float = 120.0,
) -> Optional[Dict[str, List[Dict[str, float]]]]:
    """批量拉取（一次登录、一次占用网关、中间带限速）。

    返回 None 表示 baostock 整体不可用（调用方应中断扫描）；
    否则返回 {code: bars}，缺失的 code 表示无数据，不含在结果里。
    """
    if is_open():
        return None

    def _run() -> Optional[Dict[str, List[Dict[str, float]]]]:
        try:
            import baostock as bs
        except ImportError:
            return None

        out: Dict[str, List[Dict[str, float]]] = {}
        errs = 0
        for code in codes:
            if not _valid_code(code):
                continue
            # 按只获取闸门，而不是整批独占：
            # 这样批量扫描期间，单只行情查询能在批次间隙插入，不会被阻塞几十秒。
            gate = _gate
            if not gate.acquire(timeout=_STUCK_SECONDS):
                _drop_session("gate stuck (batch)")
                return None
            try:
                if not _login_locked(bs):
                    return None

                _respect_interval()
                try:
                    rs = bs.query_history_k_data_plus(
                        code, "date,open,high,low,close,volume",
                        start_date=start_date, end_date=end_date,
                        frequency=frequency, adjustflag=adjustflag,
                    )
                except Exception as e:  # noqa: BLE001
                    # 单只异常可能是个股问题，容忍少量；连续多只异常才认为会话已死
                    errs += 1
                    if errs >= 5:
                        _drop_session(f"batch query exception: {e}")
                        return None
                    continue
                if rs is None or rs.error_code != "0":
                    errs += 1
                    if errs >= 5:
                        _drop_session("batch query failed")
                        return None
                    continue
                errs = 0

                bars: List[Dict[str, float]] = []
                while rs.next():
                    r = rs.get_row_data()
                    try:
                        bars.append({
                            "date": r[0],
                            "open": float(r[1]), "high": float(r[2]),
                            "low": float(r[3]), "close": float(r[4]),
                            "volume": float(r[5]) if r[5] else 0.0,
                        })
                    except (ValueError, IndexError):
                        continue
                if bars:
                    out[code] = bars
            finally:
                gate.release()
        return out

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(loop.run_in_executor(_EXECUTOR, _run), timeout=timeout)
    except asyncio.TimeoutError:
        _drop_session("batch asyncio timeout")
        _record_failure(f"batch timeout > {timeout:.0f}s")
        return None

    if result is None:
        _record_failure("batch unavailable")
        return None

    _record_success()
    global _query_count
    with _state:
        _query_count += len(codes)
    return result


# ---------- 股票基础信息（名称 + 申万行业）----------
_BASICS_CACHE: Optional[tuple] = None
_BASICS_TTL = 24 * 3600.0


async def fetch_stock_basics(timeout: float = 40.0) -> Optional[Dict[str, Dict[str, str]]]:
    """一次拉全市场股票名称与行业（baostock query_stock_industry，24h 缓存）。

    返回 ``{sh.600519: {"name": "贵州茅台", "industry": "白酒", "classification": "申万一级"}}``；
    None 表示 baostock 当前不可用（调用方可回退其他来源）。
    """
    global _BASICS_CACHE

    with _state:
        if _BASICS_CACHE and time.monotonic() - _BASICS_CACHE[0] < _BASICS_TTL:
            return _BASICS_CACHE[1]

    if is_open():
        return None

    def _run() -> Optional[Dict[str, Dict[str, str]]]:
        try:
            import baostock as bs
        except ImportError:
            return None

        gate = _gate
        if not gate.acquire(timeout=_STUCK_SECONDS):
            _drop_session("gate stuck (basics)")
            _record_failure("basics query stuck")
            return None
        try:
            if not _login_locked(bs):
                return None
            _respect_interval()
            try:
                rs = bs.query_stock_industry()
            except Exception as e:  # noqa: BLE001
                _drop_session(f"basics exception: {e}")
                return None
            if rs is None or rs.error_code != "0":
                _drop_session("basics query failed")
                return None

            out: Dict[str, Dict[str, str]] = {}
            while rs.next():
                r = rs.get_row_data()
                # updateDate, code, code_name, industry, industryClassification
                if len(r) >= 5 and r[1]:
                    out[r[1]] = {
                        "name": r[2],
                        "industry": r[3],
                        "classification": r[4],
                    }
            return out
        finally:
            gate.release()

    loop = asyncio.get_running_loop()
    try:
        data = await asyncio.wait_for(loop.run_in_executor(_EXECUTOR, _run), timeout=timeout)
    except asyncio.TimeoutError:
        _record_failure(f"basics timeout > {timeout:.0f}s")
        return None

    if data is None:
        _record_failure("basics unavailable")
        return None

    _record_success()
    with _state:
        _BASICS_CACHE = (time.monotonic(), data)
    logger.info(f"[baostock] 股票基础信息已缓存：{len(data)} 条")
    return data
