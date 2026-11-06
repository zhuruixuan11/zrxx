"""市场信号 API —— 基于 baostock 监控大盘指数异动。

监控指数：沪深300、上证指数、深证成指、创业板指、科创50
逻辑：当日涨跌幅超过阈值（默认 ±2%）或成交量异常放大 → 生成信号

数据源：baostock（免费、无需注册）
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Query
from loguru import logger

router = APIRouter(prefix="/api/signals", tags=["signals"])

# 监控的主要指数
# baostock 指数代码前缀：sh. / sz.
WATCH_INDICES = [
    {"code": "sh.000300", "name": "沪深300", "desc": "大盘蓝筹核心指数"},
    {"code": "sh.000001", "name": "上证指数", "desc": "上海市场整体表现"},
    {"code": "sz.399001", "name": "深证成指", "desc": "深圳市场整体表现"},
    {"code": "sz.399006", "name": "创业板指", "desc": "成长型中小企业"},
    {"code": "sh.000688", "name": "科创50", "desc": "科技创新企业"},
]


async def _fetch_all_indices_once() -> Dict[str, List[Dict[str, float]]]:
    """拉所有监控指数日线，统一走 baostock 网关（单会话 + 限速 + 熔断）。"""
    from .. import baostock_gateway

    today = datetime.now()
    start = today - timedelta(days=70)
    start_str = start.strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")

    results: Dict[str, List[Dict[str, float]]] = {}
    for idx in WATCH_INDICES:
        bars = await baostock_gateway.fetch_bars(
            idx["code"], start_str, end_str, frequency="d", adjustflag="3", timeout=20,
        )
        if bars is None:
            logger.warning("[signals] baostock 不可用（限流/熔断），本轮跳过剩余指数")
            break
        if len(bars) >= 20:
            results[idx["name"]] = bars

    logger.info(f"[signals] fetched {len(results)}/{len(WATCH_INDICES)} indices")
    return results


def _analyze_index(bars: List[Dict[str, float]], name: str) -> Optional[Dict[str, Any]]:
    """分析单只指数，返回信号（如果有）或 None。"""
    closes = np.array([b["close"] for b in bars])
    volumes = np.array([b["volume"] for b in bars])

    latest = closes[-1]
    prev = closes[-2]
    change_pct = (latest - prev) / prev * 100

    avg_vol = volumes[:-1].mean()
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    # 检测 MA5 和 MA20 的位置
    ma5 = closes[-5:].mean()
    ma20 = closes[-20:].mean()

    # 检测信号
    signals = []

    if abs(change_pct) >= 2.0:
        direction = "暴涨" if change_pct > 0 else "暴跌"
        signals.append({
            "type": "sharp_move",
            "level": "high" if abs(change_pct) >= 3.0 else "medium",
            "message": f"📈 {name} {direction} {change_pct:+.2f}%，单日波动显著",
        })

    if vol_ratio >= 2.0:
        signals.append({
            "type": "volume_surge",
            "level": "medium",
            "message": f"🔊 {name} 成交量放大 {vol_ratio:.1f}x（20日均量），市场情绪异常",
        })

    # MA 金叉/死叉
    prev_ma5 = closes[-6:-1].mean()
    prev_ma20 = closes[-21:-1].mean()
    if prev_ma5 <= prev_ma20 and ma5 > ma20:
        signals.append({
            "type": "ma_golden_cross",
            "level": "medium",
            "message": f"✨ {name} MA5 上穿 MA20（金叉），短期趋势转多",
        })
    elif prev_ma5 >= prev_ma20 and ma5 < ma20:
        signals.append({
            "type": "ma_death_cross",
            "level": "medium",
            "message": f"⚠️ {name} MA5 下穿 MA20（死叉），短期趋势转空",
        })

    return {
        "index": name,
        "latest_close": round(latest, 2),
        "change_pct": round(change_pct, 2),
        "volume_ratio": round(vol_ratio, 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "signals": signals,
    }


async def _scan_all_indices() -> List[Dict[str, Any]]:
    """扫描所有监控指数，返回带信号的分析结果（baostock 网关统一取数）。"""
    all_data = await _fetch_all_indices_once()

    results = []
    for name, bars in all_data.items():
        analysis = _analyze_index(bars, name)
        if analysis:
            results.append(analysis)

    return results


@router.get("/market")
async def market_signals(
    threshold: float = Query(default=2.0, ge=0.5, le=10.0, description="涨跌幅阈值 %"),
):
    """大盘异动信号（沪深300/上证/深成/创业板/科创50）。"""
    results = await _scan_all_indices()

    # 过滤出有信号的
    triggered = []
    for r in results:
        if r["signals"]:
            triggered.append(r)

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "indices_scanned": len(results),
            "indices_with_signals": len(triggered),
            "triggered": triggered,
            "all_indices": results,
            "note": f"涨跌幅阈值 ±{threshold}%。数据源：baostock 真实日线。",
        },
    }
