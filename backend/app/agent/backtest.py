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
    """带超时的 baostock 取数。失败/超时返回空列表（让上层走兜底）。"""
    import baostock as bs

    loop = asyncio.get_event_loop()

    def _do_login_and_query():
        try:
            lg = bs.login()
            if lg.error_code != "0":
                return None, f"login failed: {lg.error_msg}"
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                    frequency=frequency,
                    adjustflag="3",  # 前复权
                )
                if rs.error_code != "0":
                    return None, f"query failed: {rs.error_msg}"
                bars = []
                while rs.next():
                    row = rs.get_row_data()
                    try:
                        bars.append({
                            "date": row[0],
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]) if row[5] else 0.0,
                        })
                    except (ValueError, IndexError):
                        continue
                return bars, None
            finally:
                bs.logout()
        except Exception as e:
            return None, str(e)

    try:
        result, err = await asyncio.wait_for(
            loop.run_in_executor(None, _do_login_and_query), timeout=25)
    except asyncio.TimeoutError:
        logger.warning(f"baostock timeout ({bs_code})")
        # 超时后毒化会话，强制清理
        try:
            bs.logout()
        except Exception:
            pass
        return []

    if err:
        logger.warning(f"baostock fetch failed ({bs_code}): {err}")
        return []
    return result or []
