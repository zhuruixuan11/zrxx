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
