"""行情工具 —— 真实数据源，零 Mock。

数据源优先级：
1. baostock（日线/分钟线，库直连，稳定无反爬，覆盖 A 股全市场）
2. 新浪 K 线 HTTP（北交所兜底 + baostock 返回空时兜底）
3. 东方财富 push2 HTTP（实时盘口快照，可能被反爬）
4. yfinance（美股兜底）

A股代码规则：
- 600/601/603/605/688 → 上交所 sh
- 000/001/002/003/300/301 → 深交所 sz
- 4/8/920 开头（6位） → 北交所 bj
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger


# ========== A 股代码标准化 ==========
def normalize_a_share(code: str) -> Optional[str]:
    """把各种格式的 A 股代码统一为 baostock 格式：sh.600519 / sz.000001 / bj.830799。

    支持输入：600519 / sh600519 / 600519.SH / 沪600519 / 贵州茅台
    返回 None 表示无法识别为 A 股。
    """
    code = code.strip().lower()

    # 带市场前缀
    m = re.match(r'^(sh|sz|bj)\.?(\d{6})$', code)
    if m:
        return f"{m.group(1)}.{m.group(2)}"

    # 带后缀
    m = re.match(r'^(\d{6})\.(sh|sz|bj)$', code)
    if m:
        return f"{m.group(2)}.{m.group(1)}"

    # 纯 6 位数字 → 自动判市场
    m = re.match(r'^(\d{6})$', code)
    if m:
        pure = m.group(1)
        if pure.startswith(('600', '601', '603', '605', '688')):
            return f"sh.{pure}"
        if pure.startswith(('000', '001', '002', '003', '300', '301')):
            return f"sz.{pure}"
        if pure.startswith(('4', '8', '920')):
            return f"bj.{pure}"
        # 无法识别前缀但仍是 6 位 → 先猜 sh
        return f"sh.{pure}"

    return None


def is_a_share(symbol: str) -> bool:
    return normalize_a_share(symbol) is not None


# ========== 单只标的实时行情 ==========
# 短时缓存：同一轮对话里组合/分析/预警会重复取同一只票，避免重复请求数据源
_QUOTE_CACHE: "OrderedDict[str, tuple[float, Dict[str, Any]]]" = OrderedDict()
_HIST_CACHE: "OrderedDict[str, tuple[float, Dict[str, Any]]]" = OrderedDict()
_QUOTE_TTL = 30.0    # 行情缓存 30 秒
_HIST_TTL = 300.0    # 历史 K 线缓存 5 分钟
_CACHE_MAX = 512     # 单表最多缓存条数，超出按最久未使用淘汰

# baostock 的会话对象不是线程安全的，并发查询时用它串行化
_BS_LOCK = threading.Lock()


def _cache_get(cache: "OrderedDict[str, tuple[float, Dict[str, Any]]]", key: str, ttl: float):
    hit = cache.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.monotonic() - ts > ttl:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return value


def _cache_set(cache: "OrderedDict[str, tuple[float, Dict[str, Any]]]", key: str, value: Dict[str, Any]):
    """写入缓存并淘汰最久未使用的条目，避免长跑内存持续增长。"""
    cache[key] = (time.monotonic(), value)
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAX:
        cache.popitem(last=False)


async def get_quote(symbol: str, market: str = "auto") -> Dict[str, Any]:
    """获取实时/最新行情。绝不返回假数据，失败就返回 error。"""
    key = symbol.strip().upper()
    cached = _cache_get(_QUOTE_CACHE, key, _QUOTE_TTL)
    if cached is not None:
        return cached

    # 1) A 股走 baostock（日线最新收盘 + 东方财富实时盘口补充）
    if is_a_share(symbol):
        result = await _bs_quote(symbol)
        if result and "error" not in result:
            _cache_set(_QUOTE_CACHE, key, result)
            return result
        return result or {"error": "数据源不可用", "symbol": symbol}

    # 2) 非 A 股（如 AAPL）→ 尝试 yfinance
    result = await _yf_quote(symbol)
    if result and "error" not in result:
        _cache_set(_QUOTE_CACHE, key, result)
        return result

    return {"error": "未找到可用数据源", "symbol": symbol,
            "hint": "A股请输入 6 位代码（如 600519），美股请输入 ticker（如 AAPL）"}


async def get_historical(symbol: str, days: int = 30) -> Dict[str, Any]:
    """历史 K 线。"""
    key = f"{symbol.strip().upper()}:{days}"
    cached = _cache_get(_HIST_CACHE, key, _HIST_TTL)
    if cached is not None:
        return cached

    if is_a_share(symbol):
        result = await _bs_history(symbol, days)
        # baostock 不覆盖北交所，且偶发返回空数据 → 用新浪 K 线兜底
        if not result or not result.get("data"):
            fallback = await _sina_kline(symbol, days)
            if fallback:
                _cache_set(_HIST_CACHE, key, fallback)
                return fallback
        if result and "error" not in result:
            _cache_set(_HIST_CACHE, key, result)
            return result
        return result or {"error": "数据源不可用", "symbol": symbol}

    result = await _yf_history(symbol, days)
    if result and "error" not in result:
        _cache_set(_HIST_CACHE, key, result)
        return result
    return {"error": "未找到可用数据源", "symbol": symbol}


# ========== baostock 会话池（复用连接，避免每次 login 20s）==========
_bs_cache: Dict[str, Any] = {}

def _bs_get():
    """获取/创建 baostock 登录会话（复用，避免每次 login ~20s）。"""
    import baostock as bs
    cached = _bs_cache.get("session")
    if cached is not None:
        # 简单健康检查：发个轻量 query 确认连接没断
        try:
            rs = cached.query_history_k_data_plus(
                "sh.600519", "date,close",
                start_date="2026-11-01", end_date="2026-11-04", frequency="d",
            )
            if rs.error_code == "0":
                return cached
        except Exception:
            pass
    # 重新 login
    lg = bs.login()
    if lg.error_code != "0":
        logger.warning(f"baostock login failed: {lg.error_msg}")
        return None
    _bs_cache["session"] = bs
    return bs


def _bs_close():
    """关闭并清空缓存会话。"""
    cached = _bs_cache.pop("session", None)
    if cached is not None:
        try:
            cached.logout()
        except Exception:
            pass


def _bs_run_in_executor(fn):
    """用已登录的 bs 会话跑查询（在线程池中）。

    baostock 是进程级单例，登录/查询/重连必须整体串行，
    否则行情和 K 线并发时两次 login 会互相踩掉会话。
    """
    import baostock as bs
    try:
        with _BS_LOCK:
            if _bs_cache.get("session") is None:
                lg = bs.login()
                if lg.error_code != "0":
                    logger.warning(f"baostock login failed: {lg.error_msg}")
                    return None
                _bs_cache["session"] = bs
            try:
                return fn(bs)
            except Exception as e:
                logger.warning(f"baostock query error, reconnecting: {e}")
                try:
                    bs.logout()
                except Exception:
                    pass
                _bs_cache.pop("session", None)
                lg = bs.login()
                if lg.error_code != "0":
                    return None
                _bs_cache["session"] = bs
                try:
                    return fn(bs)
                except Exception as e2:
                    logger.warning(f"baostock retry also failed: {e2}")
                    return None
    except Exception as e:
        logger.warning(f"baostock session error: {e}")
        return None


async def _bs_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """用 baostock 获取最新交易日的日线行情（复用已登录连接）。"""
    bs_code = normalize_a_share(symbol)
    if not bs_code:
        return None

    # 拿最近 20 个交易日日线（覆盖周末/节假日）
    today = _dt.date.today()
    start = today - _dt.timedelta(days=20)
    start_str = start.strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")

    def _do_query(bs):
        return bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount",
            start_date=start_str, end_date=end_str, frequency="d", adjustflag="3",
        )

    loop = asyncio.get_event_loop()
    # 东方财富实时盘口与 baostock 日线并行，缩短单只行情耗时
    em_task = asyncio.create_task(_em_push2_realtime(bs_code))
    rs = await loop.run_in_executor(None, lambda: _bs_run_in_executor(_do_query))
    if rs is None or rs.error_code != "0":
        em_task.cancel()
        logger.warning(f"baostock quote query failed: {getattr(rs, 'error_msg', 'unknown')}")
        return None

    rows: List[List[str]] = []
    while rs.next():
        rows.append(rs.get_row_data())

    if not rows:
        return None

    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else latest

    def safe_float(row: List[str], idx: int) -> float:
        try:
            if len(row) <= idx or row[idx] in ("", None):
                return 0.0
            return round(float(row[idx]), 2)
        except (ValueError, IndexError, TypeError):
            return 0.0

    close = safe_float(latest, 4)
    prev_close = safe_float(prev, 4) or close
    change = round(close - prev_close, 2)
    change_pct = round(change / prev_close * 100, 2) if prev_close else 0

    realtime = await _await_optional(em_task)

    result = {
        "symbol": symbol.upper(),
        "bs_code": bs_code,
        "trade_date": latest[0] if len(latest) > 0 else "",
        "current_price": realtime.get("current_price", close),
        "previous_close": prev_close,
        "open": realtime.get("open", safe_float(latest, 1)) or safe_float(latest, 1),
        "high": realtime.get("high", safe_float(latest, 2)) or safe_float(latest, 2),
        "low": realtime.get("low", safe_float(latest, 3)) or safe_float(latest, 3),
        "volume": int(safe_float(latest, 5)),
        "amount": safe_float(latest, 6),
        "change": realtime.get("change", change),
        "change_percent": realtime.get("change_percent", change_pct),
        "data_source": "baostock" + ("+eastmoney" if realtime else ""),
        "is_realtime": bool(realtime),
    }
    # 估值/量能（仅有东财快照时有值）
    for k in ("pe", "pb", "total_mv", "circ_mv", "turnover_rate", "volume_ratio"):
        if realtime.get(k) is not None:
            result[k] = realtime[k]
    return result


async def _bs_history(symbol: str, days: int = 30) -> Optional[Dict[str, Any]]:
    """baostock 历史日线（复用已登录连接）。"""
    bs_code = normalize_a_share(symbol)
    if not bs_code:
        return None

    today = _dt.date.today()
    # 按自然日多留余量：days 指交易日，1.8 倍自然日 + 15 天可覆盖周末与长假
    start = today - _dt.timedelta(days=int(days * 1.8) + 15)
    start_str = start.strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")

    def _do_query(bs):
        return bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume",
            start_date=start_str, end_date=end_str, frequency="d", adjustflag="3",
        )

    loop = asyncio.get_event_loop()
    rs = await loop.run_in_executor(None, lambda: _bs_run_in_executor(_do_query))
    if rs is None or rs.error_code != "0":
        return None

    rows: List[List[str]] = []
    while rs.next():
        rows.append(rs.get_row_data())

    data = []
    for r in rows[-days:]:
        try:
            if len(r) < 6:
                continue
            data.append({
                "date": r[0],
                "open": round(float(r[1]), 2),
                "high": round(float(r[2]), 2),
                "low": round(float(r[3]), 2),
                "close": round(float(r[4]), 2),
                "volume": int(r[5]) if r[5] else 0,
            })
        except (ValueError, IndexError):
            continue

    return {
        "symbol": symbol.upper(),
        "bs_code": bs_code,
        "period": f"{days}d",
        "data": data,
        "count": len(data),
        "data_source": "baostock",
    }


async def _sina_kline(symbol: str, days: int = 30) -> Optional[Dict[str, Any]]:
    """新浪日 K 兜底：覆盖北交所，且 baostock 返回空时可用。"""
    bs_code = normalize_a_share(symbol)
    if not bs_code:
        return None
    sina_code = bs_code.replace(".", "")  # sh600519 / bj920819

    rows = None
    # 新浪偶尔限流/断连，重试一次再放弃
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=6, headers={"User-Agent": "Mozilla/5.0"}) as client:
                resp = await client.get(
                    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                    params={"symbol": sina_code, "scale": 240, "ma": "no", "datalen": max(days, 30)},
                )
                rows = resp.json()
            if isinstance(rows, list) and rows:
                break
        except Exception as e:
            logger.warning(f"sina kline failed {symbol} (attempt {attempt + 1}): {e}")
        if attempt == 0:
            await asyncio.sleep(0.5)
    if rows is None:
        return None

    if not isinstance(rows, list) or not rows:
        return None

    data = []
    for r in rows[-days:]:
        try:
            data.append({
                "date": str(r.get("day", "")),
                "open": round(float(r.get("open", 0)), 2),
                "high": round(float(r.get("high", 0)), 2),
                "low": round(float(r.get("low", 0)), 2),
                "close": round(float(r.get("close", 0)), 2),
                "volume": int(float(r.get("volume") or 0)),
            })
        except (TypeError, ValueError):
            continue

    if not data:
        return None
    return {
        "symbol": symbol.upper(),
        "bs_code": bs_code,
        "period": f"{days}d",
        "data": data,
        "count": len(data),
        "data_source": "sina",
    }


async def _await_optional(task: "asyncio.Task") -> Dict[str, Any]:
    """等待可选数据源任务，超时/异常都返回空 dict（不影响主数据）。"""
    try:
        return await asyncio.wait_for(task, timeout=3)
    except Exception:
        return {}


# ---------- 东方财富 push2 实时盘口（可选补充） ----------
async def _em_push2_realtime(bs_code: str) -> Dict[str, Any]:
    """尝试抓东方财富实时盘口快照。失败返回 {}（不报错，让日线数据兜底）。"""
    try:
        import aiohttp
    except ImportError:
        return {}

    # baostock sh.600519 → 东方财富 1.600519 / 0.000001 / 2.830799
    market_map = {"sh": "1", "sz": "0", "bj": "0"}
    prefix, code = bs_code.split(".")
    secid = f"{market_map.get(prefix, '0')}.{code}"

    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
           f"&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f170,f9,f23,f20,f21,f8,f10")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as sess:
            async with sess.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    return {}
                j = await resp.json(content_type=None)
                d = j.get("data") or {}
                if not d:
                    return {}
                # 东方财富返回的价格是整数 × 100（分）
                cur_price = d.get("f43")
                prev_close = d.get("f60")
                if cur_price in (None, "-") or prev_close in (None, "-"):
                    return {}
                cur_price = float(cur_price) / 100
                prev_close = float(prev_close) / 100
                change = round(cur_price - prev_close, 2)
                change_pct = round(change / prev_close * 100, 2) if prev_close else 0

                d_raw = d

                def div(key: str, d: float):
                    v = d_raw.get(key)
                    if v in (None, "-", ""):
                        return None
                    try:
                        return round(float(v) / d, 2)
                    except (TypeError, ValueError):
                        return None

                return {
                    "current_price": cur_price,
                    "open": round(float(d.get("f46", 0)) / 100, 2) if d.get("f46") not in (None, "-") else None,
                    "high": round(float(d.get("f44", 0)) / 100, 2) if d.get("f44") not in (None, "-") else None,
                    "low": round(float(d.get("f45", 0)) / 100, 2) if d.get("f45") not in (None, "-") else None,
                    "change": change,
                    "change_percent": change_pct,
                    "volume": int(d.get("f47", 0)) if d.get("f47") not in (None, "-") else 0,
                    # 估值与量能字段（东方财富快照）
                    "pe": div("f9", 100),          # 市盈率(动)
                    "pb": div("f23", 100),         # 市净率
                    "total_mv": div("f20", 1e8),   # 总市值（亿元）
                    "circ_mv": div("f21", 1e8),    # 流通市值（亿元）
                    "turnover_rate": div("f8", 100),   # 换手率 %
                    "volume_ratio": div("f10", 100),   # 量比
                }
    except Exception:
        return {}


# ---------- yfinance 美股兜底 ----------
async def _yf_quote(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        import yfinance as yf
        ticker = await asyncio.get_event_loop().run_in_executor(None, lambda: yf.Ticker(symbol))
        info = await asyncio.get_event_loop().run_in_executor(None, lambda: ticker.fast_info)
        price = getattr(info, "last_price", None)
        if price is None:
            return None
        prev = getattr(info, "previous_close", price) or price
        change = round(float(price) - float(prev), 2)
        return {
            "symbol": symbol.upper(),
            "current_price": round(float(price), 2),
            "previous_close": round(float(prev), 2),
            "open": round(float(getattr(info, "open", prev)), 2),
            "high": round(float(getattr(info, "day_high", price)), 2),
            "low": round(float(getattr(info, "day_low", price)), 2),
            "volume": int(getattr(info, "last_volume", 0)),
            "change": change,
            "change_percent": round(change / float(prev) * 100, 2) if prev else 0,
            "data_source": "yfinance",
            "is_realtime": True,
        }
    except Exception:
        return None


async def _yf_history(symbol: str, days: int) -> Optional[Dict[str, Any]]:
    try:
        import yfinance as yf
        ticker = await asyncio.get_event_loop().run_in_executor(None, lambda: yf.Ticker(symbol))
        hist = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ticker.history(period=f"{days}d", interval="1d"))
        if hist is None or hist.empty:
            return None
        data = []
        for dt, row in hist.iterrows():
            data.append({
                "date": dt.strftime("%Y-%m-%d"),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]),
            })
        return {"symbol": symbol.upper(), "period": f"{days}d",
                "data": data[-days:], "data_source": "yfinance"}
    except Exception:
        return None


# ========== 投资组合工具 ==========
async def get_portfolio_summary(user_id: str, session) -> Dict[str, Any]:
    """从数据库查用户投资组合。"""
    from sqlalchemy import select
    from ..models.portfolio import Portfolio, Holding

    result = await session.execute(select(Portfolio).where(Portfolio.user_id == user_id))
    portfolio = result.scalar_one_or_none()
    if not portfolio:
        portfolio = Portfolio(user_id=user_id, name="我的投资组合", initial_capital=100_000,
                              cash_balance=100_000)
        session.add(portfolio)
        await session.commit()
        await session.refresh(portfolio)

    holdings_result = await session.execute(select(Holding).where(Holding.portfolio_id == portfolio.id))
    holdings = holdings_result.scalars().all()

    holdings_out = []
    total_value = float(portfolio.cash_balance)

    # 并发取行情：持仓多时不再逐只串行等待（baostock 内部串行、东财可选源并行）
    sem = asyncio.Semaphore(4)

    async def _quote_of(sym: str) -> Dict[str, Any]:
        async with sem:
            try:
                return await get_quote(sym)
            except Exception:
                return {"error": "行情获取失败"}

    quotes = await asyncio.gather(*[_quote_of(h.symbol) for h in holdings]) if holdings else []

    for h, q in zip(holdings, quotes):
        price = float(q.get("current_price", 0) or 0)
        market_value = price * float(h.quantity)
        pnl = market_value - float(h.avg_cost or 0) * float(h.quantity)
        total_value += market_value
        holdings_out.append({
            "symbol": h.symbol,
            "asset_type": h.asset_type.value,
            "quantity": float(h.quantity),
            "avg_cost": round(float(h.avg_cost), 4),
            "current_price": price,
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pnl_percent": round(pnl / (float(h.avg_cost) * float(h.quantity) or 1) * 100, 2),
            "quote_source": q.get("data_source", "unknown"),
        })

    total_pnl = total_value - float(portfolio.initial_capital)
    return {
        "portfolio_id": portfolio.id,
        "name": portfolio.name,
        "cash_balance": round(float(portfolio.cash_balance), 2),
        "total_value": round(total_value, 2),
        "total_cost": round(total_value - total_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "pnl_percent": round(total_pnl / float(portfolio.initial_capital) * 100, 2),
        "initial_capital": float(portfolio.initial_capital),
        "holdings_count": len(holdings_out),
        "holdings": holdings_out,
    }
