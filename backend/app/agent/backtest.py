"""回测历史行情获取（baostock 日线/分钟线，供 backtest_engine 使用）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

from .tools import normalize_a_share


async def fetch_history_bars(symbol: str, days: int = 90, frequency: str = "d") -> List[Dict[str, float]]:
    """从 baostock 拉历史 bar。

    frequency: d=日线, w=周线, m=月线, 5/15/30/60=分钟线
    返回 [{date, open, high, low, close, volume}, ...] 按时间升序。
    """
    import baostock as bs

    bs_code = normalize_a_share(symbol)
    if not bs_code:
        raise ValueError(f"无法识别的股票代码: {symbol}")

    today = datetime.now()
    start = today - timedelta(days=days + 30)  # 多取 30 天算 MA 预热

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")

    try:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=today.strftime("%Y-%m-%d"),
            frequency=frequency,
            adjustflag="3",  # 前复权
        )
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 查询失败: {rs.error_msg}")

        bars: List[Dict[str, float]] = []
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
        return bars
    finally:
        bs.logout()
