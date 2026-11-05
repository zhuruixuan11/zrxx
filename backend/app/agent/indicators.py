"""技术指标计算（纯 Python，无额外依赖）。所有输入为 baostock 日线 bar。"""
from __future__ import annotations

import math
from typing import Any, Dict, List


def _ma(values: List[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return round(sum(values[-n:]) / n, 2)


def _rsi(closes: List[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100 - 100 / (1 + rs), 2)


def _pct_change(closes: List[float], n: int) -> float | None:
    if len(closes) <= n or closes[-n - 1] == 0:
        return None
    return round((closes[-1] / closes[-n - 1] - 1) * 100, 2)


def compute_indicators(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """基于日线 bar 计算常用技术指标，数据不足时返回空 dict 或省略字段。"""
    closes = [float(b["close"]) for b in bars if b.get("close")]
    volumes = [float(b.get("volume") or 0) for b in bars]
    if len(closes) < 20:
        return {}

    out: Dict[str, Any] = {"bars": len(closes), "last_close": round(closes[-1], 2)}
    out["ma5"] = _ma(closes, 5)
    out["ma10"] = _ma(closes, 10)
    out["ma20"] = _ma(closes, 20)
    out["ma60"] = _ma(closes, 60)
    out["rsi14"] = _rsi(closes)
    out["ret5"] = _pct_change(closes, 5)
    out["ret20"] = _pct_change(closes, 20)

    recent = closes[-60:] if len(closes) >= 60 else closes
    hi, lo = max(recent), min(recent)
    out["high60"] = round(hi, 2)
    out["low60"] = round(lo, 2)
    out["pos60"] = round((closes[-1] - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0

    # 20 日年化波动率
    rets = [closes[i] / closes[i - 1] - 1 for i in range(-20, 0) if closes[i - 1]]
    if len(rets) >= 5:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        out["volatility20"] = round(math.sqrt(var) * math.sqrt(252) * 100, 1)

    # 量能：5 日均量 / 20 日均量
    if len(volumes) >= 20 and sum(volumes[-20:]) > 0:
        avg5 = sum(volumes[-5:]) / 5
        avg20 = sum(volumes[-20:]) / 20
        out["vol_ratio_5_20"] = round(avg5 / avg20, 2)

    return {k: v for k, v in out.items() if v is not None}
