"""回测历史行情获取（baostock 主源 + 新浪 K 线兜底，供 backtest_engine 使用）。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List

from loguru import logger

from .tools import normalize_a_share, _sina_kline


async def fetch_history_bars(symbol: str, days: int = 90, frequency: str = "d") -> List[Dict[str, float]]:
    """拉历史 bar（baostock 主源，登录/查询失败时自动回退新浪 K 线）。

    frequency: d=日线（新浪兜底仅覆盖日线）
    返回 [{date, open, high, low, close, volume}, ...] 按时间升序。
    """
    bs_code = normalize_a_share(symbol)
    if not bs_code:
        raise ValueError(f"无法识别的股票代码: {symbol}")

    today = datetime.now()
    start = today - timedelta(days=days + 30)  # 多取 30 天算 MA 预热

    # 1) 优先尝试 baostock（25s 超时防挂死）
    bars = await _fetch_bs_bars(bs_code, start, today, frequency)
    if bars:
        return bars

    # 2) baostock 不可用 → 新浪 K 线兜底（日线）
    if frequency == "d":
        logger.warning(f"baostock 无数据 ({symbol})，走新浪 K 线兜底")
        sina = await _sina_kline(symbol, days + 30)
        if sina and sina.get("data"):
            return sina["data"]

    raise RuntimeError(f"历史行情获取失败：baostock + 新浪均不可用 ({symbol})")


async def _fetch_bs_bars(bs_code: str, start: datetime, end: datetime, frequency: str) -> List[Dict[str, float]]:
    """统一走 baostock 网关（单会话 + 限速 + 熔断）。失败返回空列表让上层兜底。"""
    from .. import baostock_gateway

    bars = await baostock_gateway.fetch_bars(
        bs_code,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
        frequency=frequency,
        adjustflag="3",
        timeout=25,
    )
    if bars is None:
        logger.warning(f"baostock 不可用 ({bs_code})，本次回测取数降级")
        return []
    return bars
