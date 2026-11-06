"""行情 API —— 真实数据源（baostock + akshare 多源），绝不返回假数据。

新增：
  - /api/market/screener        — 全部 A 股实时行情（新浪源，5500+ 只）
  - /api/market/boards/concept  — 腾讯概念板块列表（170+ 个，带涨跌）
  - /api/market/boards/zt       — 涨停池排行（东方财富源）

所有全景接口加 5 分钟 TTL 内存缓存（akshare 全量拉一次要 10 秒+）。
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from loguru import logger
from slowapi import Limiter
from slowapi.util import get_ipaddr

from ..agent.tools import get_historical, get_quote
from ..config import get_settings

router = APIRouter(prefix="/api/market", tags=["market"])
_executor = ThreadPoolExecutor(max_workers=4)
# 部署在 nginx 之后时 request.client.host 是代理 IP，用 get_ipaddr 读取 X-Forwarded-For
limiter = Limiter(key_func=get_ipaddr)

# 内存缓存：{key: (timestamp, value)}，最多保留 32 个 key（全景数据 3 个，其余为临时键）
_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_CACHE_TTL = 300  # 5 分钟
_CACHE_MAX = 32


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if hit is None:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    return val


def _cache_set(key: str, val):
    _CACHE[key] = (time.time(), val)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


# 同一份全景数据只允许一个协程去拉，避免启动预热与首个请求并发重复拉取（akshare 一次 10s+）
_FETCH_LOCK = asyncio.Lock()


async def _cached_fetch(key: str, fn) -> List[Dict[str, Any]]:
    cached = _cache_get(key)
    if cached is not None:
        return cached
    async with _FETCH_LOCK:
        # 双检：等锁期间可能已被别的协程拉好
        cached = _cache_get(key)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(_executor, fn)
        _cache_set(key, items)
        return items


# ===================== 单只查询（原有）=====================

@router.get("/quote/{symbol}")
async def quote(symbol: str):
    """单只行情。A股 6 位数字自动识别沪深北。
    数据源优先级：baostock（优先，支持历史K线）→ 新浪全景兜底（秒回）。"""
    data = await get_quote(symbol)
    if "error" in data:
        # baostock 挂了，用全景数据兜底
        full_items = _cache_get("screener_full")
        if full_items is None:
            # 缓存也空，主动拉一次新浪全景（秒级）
            loop = asyncio.get_event_loop()
            try:
                full_items = await loop.run_in_executor(_executor, _fetch_screener_sync)
                _cache_set("screener_full", full_items)
            except Exception:
                raise HTTPException(503, "行情数据源暂时不可用（baostock 和 akshare 均离线）。请稍后重试。")

        bare = symbol.replace("sh", "").replace("sz", "").replace("bj", "").replace(".", "")
        for it in full_items:
            if it["code"] == bare:
                return {
                    "code": 0, "msg": "ok",
                    "data": {
                        "symbol": bare.upper(),
                        "bs_code": "",
                        "current_price": it["price"],
                        "previous_close": it["prev_close"],
                        "open": it["open"],
                        "high": it["high"],
                        "low": it["low"],
                        "volume": int(it["volume"]),
                        "amount": it["amount"],
                        "change": it["change"],
                        "change_percent": it["change_pct"],
                        "data_source": "sina-fallback",
                        "is_realtime": True,
                        "trade_date": time.strftime("%Y-%m-%d"),
                    },
                }
        # 全景里也找不到这只股票
        raise HTTPException(404, f"股票代码 {bare} 在 A 股全景中未找到（可能已停牌或代码有误）。")
    return {"code": 0, "msg": "ok", "data": data}


@router.get("/history/{symbol}")
async def history(symbol: str, days: int = Query(default=30, ge=1, le=365)):
    """历史 K 线（baostock）。取数失败时返回空列表，由前端展示空状态，不弹 404 错误。"""
    data = await get_historical(symbol, days=days)
    if "error" in data:
        logger.warning(f"[market/history] {symbol} failed: {data['error']}")
        return {"code": 0, "msg": "ok", "data": {"symbol": symbol, "data": [], "note": "暂无 K 线数据，请稍后重试"}}
    return {"code": 0, "msg": "ok", "data": data}


# ===================== 全部 A 股行情 =====================

def _fetch_screener_sync() -> List[Dict[str, Any]]:
    """akshare 新浪源：全部 A 股实时行情（5500+ 只）。"""
    import akshare as ak
    df = ak.stock_zh_a_spot()
    items = []
    for _, row in df.iterrows():
        raw_code = str(row.get("代码", ""))  # 带 sh./sz./bj. 前缀
        code = raw_code.replace("sh", "").replace("sz", "").replace("bj", "").replace(".", "")
        name = str(row.get("名称", "")).strip()
        if not code or not name:
            continue
        try:
            items.append({
                "code": code,
                "name": name,
                "market": "SH" if raw_code.startswith("sh") else ("SZ" if raw_code.startswith("sz") else "BJ"),
                "price": float(row.get("最新价", 0) or 0),
                "change": float(row.get("涨跌额", 0) or 0),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "open": float(row.get("今开", 0) or 0),
                "high": float(row.get("最高", 0) or 0),
                "low": float(row.get("最低", 0) or 0),
                "prev_close": float(row.get("昨收", 0) or 0),
                "volume": float(row.get("成交量", 0) or 0),
                "amount": float(row.get("成交额", 0) or 0),
                "timestamp": str(row.get("时间戳", "")),
            })
        except (ValueError, TypeError):
            continue
    # 过滤掉价格为 0 的（停牌）
    items = [x for x in items if x["price"] > 0]
    logger.info(f"[market/screener] got {len(items)} stocks")
    return items


@router.get("/screener")
@limiter.limit(lambda: get_settings().MARKET_RATE_LIMIT)
async def screener(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=30, ge=5, le=200),
    sort_by: str = Query(default="change_pct"),
    sort_order: str = Query(default="desc"),
    search: str = Query(default=""),
    market: str = Query(default="", description="SH / SZ / BJ / 空=全部"),
    change_min: Optional[float] = Query(default=None, ge=-20),
    change_max: Optional[float] = Query(default=None, le=20),
):
    """全部 A 股实时行情（新浪源）。支持分页/排序/搜索/涨跌筛选。
    全量数据 5 分钟缓存（拉一次要 10 秒+）。"""
    full_items = await _cached_fetch("screener_full", _fetch_screener_sync)

    items = full_items

    # 搜索（代码或名称包含）
    if search:
        s = search.strip().lower()
        items = [x for x in items if s in x["code"].lower() or s in x["name"]]

    # 市场过滤
    if market in ("SH", "SZ", "BJ"):
        items = [x for x in items if x["market"] == market]

    # 涨跌幅范围
    if change_min is not None:
        items = [x for x in items if x["change_pct"] >= change_min]
    if change_max is not None:
        items = [x for x in items if x["change_pct"] <= change_max]

    # 排序
    valid_sort = {"price", "change", "change_pct", "volume", "amount", "name", "code"}
    sb = sort_by if sort_by in valid_sort else "change_pct"
    reverse = sort_order.lower() != "asc"
    items.sort(key=lambda x: x.get(sb, 0) or 0, reverse=reverse)

    total = len(items)

    # 分页
    start = (page - 1) * size
    page_items = items[start:start + size]

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "total": total,
            "page": page,
            "size": size,
            "total_pages": (total + size - 1) // size,
            "items": page_items,
            "note": "数据源：新浪财经实时行情（akshare）。",
        },
    }


# ===================== 概念板块 / 涨停池 =====================

def _fetch_concept_sync() -> List[Dict[str, Any]]:
    """腾讯概念板块（170+ 个，带涨跌）。"""
    import akshare as ak
    df = ak.stock_sector_spot(indicator="概念")
    items = []
    for _, row in df.iterrows():
        try:
            items.append({
                "label": str(row.get("label", "")).strip(),
                "name": str(row.get("板块", "")).strip(),
                "company_count": int(row.get("公司家数", 0) or 0),
                "avg_price": float(row.get("平均价格", 0) or 0),
                "change": float(row.get("涨跌额", 0) or 0),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "volume": float(row.get("总成交量", 0) or 0),
                "amount": float(row.get("总成交额", 0) or 0),
                "lead_stock_code": str(row.get("股票代码", "")).strip(),
                "lead_stock_name": str(row.get("股票名称", "")).strip(),
                "lead_stock_change_pct": float(row.get("个股-涨跌幅", 0) or 0),
            })
        except (ValueError, TypeError):
            continue
    # 按涨跌幅排序
    items.sort(key=lambda x: x["change_pct"], reverse=True)
    logger.info(f"[market/boards/concept] got {len(items)} concepts")
    return items


def _fetch_zt_pool_sync() -> List[Dict[str, Any]]:
    """涨停池（东方财富源）。"""
    import akshare as ak
    from datetime import datetime
    df = ak.stock_zt_pool_em(date=datetime.now().strftime("%Y%m%d"))
    items = []
    for _, row in df.iterrows():
        raw_code = str(row.get("代码", "")).strip()
        try:
            items.append({
                "code": raw_code,
                "name": str(row.get("名称", "")).strip(),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "price": float(row.get("最新价", 0) or 0),
                "amount": float(row.get("成交额", 0) or 0),
                "float_mv": float(row.get("流通市值", 0) or 0),
                "turnover": float(row.get("换手率", 0) or 0),
                "fund_sealed": float(row.get("封板资金", 0) or 0),
                "first_seal_time": str(row.get("首次封板时间", "")),
                "last_seal_time": str(row.get("最后封板时间", "")),
                "broken_times": int(row.get("炸板次数", 0) or 0),
                "consecutive": int(row.get("连板数", 0) or 0),
                "industry": str(row.get("所属行业", "")).strip(),
            })
        except (ValueError, TypeError):
            continue
    logger.info(f"[market/boards/zt] got {len(items)} limit-up stocks")
    return items


@router.get("/boards/concept")
@limiter.limit(lambda: get_settings().MARKET_RATE_LIMIT)
async def boards_concept(request: Request):
    """概念板块列表（腾讯，带涨跌幅）。5 分钟缓存。"""
    items = await _cached_fetch("concept", _fetch_concept_sync)
    return {"code": 0, "msg": "ok", "data": {"items": items, "note": "数据源：腾讯财经（涨跌幅实时）"}}


@router.get("/boards/zt")
@limiter.limit(lambda: get_settings().MARKET_RATE_LIMIT)
async def boards_zt(request: Request):
    """今日涨停池排行（东方财富源）。5 分钟缓存。"""
    try:
        items = await _cached_fetch("zt", _fetch_zt_pool_sync)
    except Exception as e:
        logger.warning(f"[market/boards/zt] failed: {e}")
        raise HTTPException(503, "涨停池数据暂时不可用，请稍后重试")
    return {"code": 0, "msg": "ok", "data": {"items": items, "note": "数据源：东方财富"}}


# ===================== 策略选股（全市场扫描）=====================

# 支持扫描的"轻量信号"：只需最近一根 K 线 + N 日窗口统计，不跑完整回测
# key → (名称, 描述, 需要的 days, 判定函数)
_STRATEGY_SIGNALS = {}  # 运行时在 _init_strategy_signals 里填充


def _init_strategy_signals() -> None:
    """懒加载，避免 import 循环。"""
    if _STRATEGY_SIGNALS:
        return

    def _sma(close_series, window: int) -> float:
        return float(close_series.iloc[-window:].mean()) if len(close_series) >= window else float("nan")

    _STRATEGY_SIGNALS.update({
        # —— 均线金叉类 ——
        "ma5_cross_ma20": (
            "MA5 上穿 MA20（金叉）",
            "短期均线上穿长期均线，趋势反转向上",
            21,
            lambda df: (
                df["close"].iloc[-2] < df["close"].rolling(5).mean().iloc[-2]
                and df["close"].iloc[-1] > df["close"].rolling(5).mean().iloc[-1]
                and df["close"].rolling(5).mean().iloc[-1] > df["close"].rolling(20).mean().iloc[-1]
                and df["close"].rolling(5).mean().iloc[-2] <= df["close"].rolling(20).mean().iloc[-2]
            ) if len(df) >= 21 else False,
        ),
        "ma_bullish": (
            "多头排列（MA5>MA10>MA20）",
            "短中长均线全部多头排列，上升趋势明确",
            21,
            lambda df: (
                df["close"].rolling(5).mean().iloc[-1]
                > df["close"].rolling(10).mean().iloc[-1]
                > df["close"].rolling(20).mean().iloc[-1]
            ) if len(df) >= 21 else False,
        ),
        # —— RSI 类 ——
        "rsi_oversold": (
            "RSI14 超卖（<30）",
            "RSI14 低于 30，可能反弹",
            15,
            lambda df: _rsi14(df) < 30 if len(df) >= 15 else False,
        ),
        "rsi_overbought": (
            "RSI14 超买（>70）",
            "RSI14 高于 70，注意回调风险",
            15,
            lambda df: _rsi14(df) > 70 if len(df) >= 15 else False,
        ),
        # —— 突破类 ——
        "breakout_20d": (
            "突破 20 日高点",
            "收盘价创 20 个交易日新高",
            21,
            lambda df: float(df["close"].iloc[-1])
            >= float(df["high"].iloc[-21:-1].max()) if len(df) >= 21 else False,
        ),
        # —— 量价类 ——
        "vol_surge": (
            "放量（量比 > 2）",
            "今日成交量是近 5 日均量的 2 倍以上",
            6,
            lambda df: (
                float(df["volume"].iloc[-1])
                > 2.0 * float(df["volume"].iloc[-6:-1].mean())
            ) if len(df) >= 6 else False,
        ),
        # —— 反转类 ——
        "hammer": (
            "锤子线（下影线 ≥ 实体 2 倍）",
            "典型底部反转 K 线形态",
            1,
            lambda df: _is_hammer(df.iloc[-1]) if len(df) >= 1 else False,
        ),
    })


def _rsi14(df) -> float:
    """简易 RSI14（只算最新值，避免重算整列）。"""
    close = df["close"]
    deltas = close.diff().dropna()
    gain = deltas.where(deltas > 0, 0).tail(14)
    loss = (-deltas.where(deltas < 0, 0)).tail(14)
    avg_gain = float(gain.mean())
    avg_loss = float(loss.mean())
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _is_hammer(row) -> bool:
    try:
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    except (KeyError, ValueError, TypeError):
        return False
    body = abs(c - o)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    return lower_shadow >= body * 2 and upper_shadow < body


@router.get("/screener/strategic")
@limiter.limit(lambda: get_settings().MARKET_RATE_LIMIT)
async def screener_strategic(
    request: Request,
    signal: str = Query(..., description="信号 id，见 /api/market/screener/signals"),
    top_n: int = Query(default=20, ge=5, le=100),
):
    """按轻量信号扫描全市场股票。

    不走完整回测，只拉每只股票所需 N 日 K 线（信号特定），
    判定函数返回 True 就入选。支持 baostock + 新浪兜底，全串行限速（5500 只约 2-3 分钟）。
    """
    _init_strategy_signals()
    if signal not in _STRATEGY_SIGNALS:
        known = ", ".join(_STRATEGY_SIGNALS.keys())
        raise HTTPException(400, f"未知信号 id：{signal}。可用：{known}")

    name, desc, need_days, check_fn = _STRATEGY_SIGNALS[signal]

    # 先拿全市场列表（5 分钟缓存）
    full_items = await _cached_fetch("screener_full", _fetch_screener_sync)

    # 批量查 K 线：分块 + baostock 一次 login 多只 query（比单只快 10x+）
    from ..agent.tools import normalize_a_share
    bs_groups: list[tuple[str, dict]] = []  # (bs_code, item_dict)
    for item in full_items:
        bc = normalize_a_share(item["code"])
        if bc:
            bs_groups.append((bc, item))

    _BATCH_SIZE = 80
    _MAX_SCAN = min(len(bs_groups), 2000)  # 先扫前 2000 只（沪深 + 热门，北交所放后面）
    hits: List[Dict[str, Any]] = []
    scanned = 0
    degraded = False

    for batch_start in range(0, _MAX_SCAN, _BATCH_SIZE):
        if len(hits) >= top_n * 3:
            break
        batch = bs_groups[batch_start : batch_start + _BATCH_SIZE]

        # 跳过已缓存的
        uncached_codes = []
        cached_bars: dict[str, list] = {}
        for bc, _ in batch:
            cb = _cache_get(f"screener_hist:{bc}:{need_days}")
            if cb is not None:
                cached_bars[bc] = cb
            else:
                uncached_codes.append(bc)

        # 批量拉 uncached
        if uncached_codes:
            batch_result = await _bs_query_batch(uncached_codes, need_days)
            if batch_result is None:
                # baostock 已熔断/登录失败：立即停止扫描，返回已有命中，不再空耗
                logger.warning("[screener] baostock 不可用（限流/熔断），提前结束扫描")
                degraded = True
                break
            for bc, bars in batch_result.items():
                _cache_set(f"screener_hist:{bc}:{need_days}", bars)
            cached_bars.update(batch_result)

        scanned += len(batch)

        import pandas as pd
        for bc, item in batch:
            if len(hits) >= top_n * 3:
                break
            bars = cached_bars.get(bc)
            if not bars or len(bars) < need_days:
                continue
            try:
                df = pd.DataFrame(bars).sort_values("date").reset_index(drop=True)
                if check_fn(df):
                    hits.append({**item, "signal": signal, "signal_name": name})
            except Exception:
                continue

    # 按当日涨幅排序取 top_n
    hits.sort(key=lambda x: x.get("change_pct", 0), reverse=True)
    hits = hits[:top_n]

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "signal": signal,
            "signal_name": name,
            "signal_desc": desc,
            "total_scanned": scanned,
            "degraded": degraded,
            "results": hits,
        },
    }


@router.get("/screener/signals")
async def screener_signals():
    """列出所有可用的策略信号（前端下拉用）。"""
    _init_strategy_signals()
    items = []
    for sid, (name, desc, need_days, _) in _STRATEGY_SIGNALS.items():
        items.append({"id": sid, "name": name, "desc": desc, "need_days": need_days})
    return {"code": 0, "msg": "ok", "data": {"signals": items}}


async def fetch_history_bars_for_screener(symbol: str, days: int = 20) -> List[Dict[str, Any]]:
    """单只 K 线拉取（走缓存；baostock 不可用时新浪兜底）。"""
    cached = _cache_get(f"screener_hist:{symbol}:{days}")
    if cached is not None:
        return cached

    from datetime import datetime, timedelta
    from ..agent.tools import normalize_a_share, _sina_kline

    bs_code = normalize_a_share(symbol)
    if not bs_code:
        return []

    bars = await _bs_query_one(bs_code, days)
    if not bars:
        # 新浪兜底
        try:
            sina = await asyncio.wait_for(_sina_kline(symbol, days + 30), timeout=10)
            if sina and sina.get("data"):
                bars = sina["data"]
        except Exception:
            pass

    if bars:
        _cache_set(f"screener_hist:{symbol}:{days}", bars)
    return bars


async def _bs_query_one(bs_code: str, days: int) -> list:
    """单只查询，统一走 baostock 网关。不可用时返回 []（上层走新浪兜底）。"""
    from datetime import datetime, timedelta
    from .. import baostock_gateway

    start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    bars = await baostock_gateway.fetch_bars(
        bs_code, start, end, frequency="d", adjustflag="3", timeout=15,
    )
    return bars or []


async def _bs_query_batch(bs_codes: list[str], days: int) -> dict[str, list] | None:
    """批量查询，统一走 baostock 网关（单会话 + 限速 + 熔断）。

    返回 None 表示 baostock 当前不可用（已熔断/登录失败），调用方应中断扫描，
    避免继续对 2000 只股票做无谓尝试。
    """
    from datetime import datetime, timedelta
    from .. import baostock_gateway

    start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    return await baostock_gateway.fetch_bars_many(
        bs_codes, start, end, frequency="d", adjustflag="3", timeout=120,
    )


# ===================== 启动预热 =====================

async def prewarm_caches() -> None:
    """启动后在后台预热全景数据，让首个用户请求直接命中缓存。

    akshare 全量拉一次要 10 秒以上，放在请求路径上首屏会很慢；这里逐个拉，
    任一数据源失败只记日志，不影响服务启动。
    """
    targets = [
        ("screener_full", _fetch_screener_sync),
        ("concept", _fetch_concept_sync),
        ("zt", _fetch_zt_pool_sync),
    ]
    for key, fn in targets:
        if _cache_get(key) is not None:
            continue
        try:
            items = await _cached_fetch(key, fn)
            logger.info(f"[warmup] {key} 预热完成：{len(items)} 条")
        except Exception as e:
            logger.warning(f"[warmup] {key} 预热失败：{e}")
