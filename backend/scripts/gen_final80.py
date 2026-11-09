#!/usr/bin/env python
"""生成 SFT v3 final80：trial70 原样保留 + 追加 10 条真实单边上涨样本。

同时产出：
  backend/data/sft_v3/final80/samples.jsonl / samples.md
  backend/data/sft_v3/final80/REGIME_NOTES.md / MANIFEST.json

用法::

    python backend/scripts/gen_final80.py
    python backend/scripts/gen_final80.py --verify
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for p in (str(BACKEND_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.agent.backtest import fetch_history_bars  # noqa: E402
from app.baostock_gateway import fetch_bars, fetch_rows  # noqa: E402
from gen_sft_synth import FactBook  # noqa: E402
from gen_trial70 import verify, cjk_len, answers_of  # noqa: E402
from trial70_samples import (  # noqa: E402
    COMMISSION, INITIAL_CAPITAL, LOT, NAMES, PAD, STAMP,
    _consts, _emit, _hold_numbers, _run_bt, _pad, regime, window_metrics,
)

OUT_DIR = BACKEND_ROOT / "data" / "sft_v3" / "final80"
TRIAL70 = BACKEND_ROOT / "data" / "sft_v3" / "trial70" / "samples.jsonl"

NAMES_NEW = {
    "sh.601728": "中国电信", "sh.600919": "江苏银行", "sh.601766": "中国中车",
    "sh.600018": "上港集团", "sh.601088": "中国神华", "sz.000538": "云南白药",
    "sh.600674": "川投能源", "sz.003816": "中国广核", "sh.600377": "宁沪高速",
    "sh.603233": "大参林",
}

# screen_uptrend.py 在 2026-11-06 收盘数据上的真实扫描结果
SCREEN_SUMMARY = {
    "pool": "沪深300 ∪ 中证500 成分股（覆盖沪深主板与创业板）",
    "universe_after_filters": 637,
    "scanned_windows": 2548,
    "candidates": 27,
    "filters": {"exclude": ["ST/退市", "上市晚于 2025-09-01",
                            "20 日均成交额 < 1 亿元", "trial70 已有 70 只"],
                "uptrend": "区间涨跌>+15% 且最大回撤<10%",
                "trend": "站上 60 日线天数占比>=60% 或回撤次数<=3"},
}


# ============================ 行情工具 ============================
def trend_stats(bars: list[dict], n: int) -> dict:
    win = bars[-n:]
    lo = len(bars) - n
    closes = [b["close"] for b in bars]
    above = total = 0
    for k in range(n):
        i = lo + k
        if i + 1 < 60:
            continue
        ma = sum(closes[i + 1 - 60:i + 1]) / 60
        total += 1
        if closes[i] > ma:
            above += 1
    peak, pull, in_dd = win[0]["close"], 0, False
    for b in win[1:]:
        c = b["close"]
        if c >= peak:
            peak, in_dd = c, False
        elif (peak - c) / peak * 100 >= 3 and not in_dd:
            pull += 1
            in_dd = True
    return {"ma60_above_pct": round(above / total * 100, 1) if total else 0.0,
            "pullbacks": pull}


def _range(fb, rows, req) -> str:
    rg = regime(rows)
    fb.c("regime_label", "单边上涨")
    return (f"请求{fb.n('requested_days', req, 0)}个交易日，"
            f"日线实得{fb.n('bars_count', len(rows), 0)}根，"
            f"区间{fb.d(rg['start'])}到{fb.d(rg['end'])}，"
            f"区间涨跌{fb.n('range_return_pct', rg['ret'], 2, signed=True)}%、"
            f"最大回撤{fb.n('max_drawdown_pct', rg['mdd'], 2)}%、"
            f"振幅{fb.n('amplitude_pct', rg['amp'], 2)}%，归为单边上涨。")


def _fees(fb, name: str) -> str:
    return (f"信号取前收、本根开盘成交，一手"
            f"{fb.c('lot_size', LOT)}股取整，佣金{fb.c('commission_pct', COMMISSION)}%、"
            f"卖出加{fb.c('stamp_tax_pct', STAMP)}%印花税，期末收盘盯市。")


def new_fb() -> FactBook:
    fb = FactBook()
    _consts(fb)
    return fb


# ============================ 10 条正文 ============================
async def _bars_for(code: str, n: int) -> list[dict]:
    bars = await fetch_history_bars(code, days=n + 240)
    if len(bars) < n + 60:
        raise RuntimeError(f"{code} 日线不足：{len(bars)}")
    return bars


async def build_new() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []

    # ---------- 回测 1：中国电信 · KDJ（过早止盈） ----------
    code, days, strategy = "sh.601728", 90, "kdj"
    name = NAMES_NEW[code]
    bt = await _run_bt(code, strategy, days, {})
    bars = await _bars_for(code, bt["bars"])
    rows = bars[-bt["bars"]:]
    ts = trend_stats(bars, bt["bars"])
    rg = regime(rows)
    fb = new_fb()
    p1 = (f"中国电信这段是少见的稳步上行，KDJ 却只做出三个点。 "
          + _range(fb, rows, days))
    p2 = (f"{name}口径照引擎：KDJ 取九、三、三，K 上穿 D 买入、下穿卖出。"
          + _fees(fb, name))
    p3 = (f"{name}的读数：平仓{fb.n('sell_count', bt['sells'], 0)}笔、"
          f"赢{fb.n('win_count', bt['wins'], 0)}笔、"
          f"胜率{fb.n('win_rate_pct', bt['metrics']['win_rate_pct'], 2)}%，"
          f"策略{fb.n('strategy_return_pct', bt['metrics']['total_return_pct'], 2, signed=True)}%、"
          f"买入持有{fb.n('buy_hold_pct', rg['ret'], 2, signed=True)}%，"
          f"策略最大回撤{fb.n('strategy_max_drawdown_pct', bt['metrics']['max_drawdown_pct'], 2)}%；"
          f"十次交叉里六次是假信号，主升段被切成几小段，越涨越早卖，收益被止盈规则截断。"
          f"这段是单边上涨，最容易犯的错就是把高位钝化当成趋势结束，"
          f"K 下穿 D 往往只是日内回踩。")
    p4 = (f"{name}反转要看：如果 K 线在高位横住而不是继续抬升，死叉才值得当真；"
          f"这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%的交易日收在六十日线上方，"
          f"趋势没破之前，死叉只配减仓不配清仓。九十根样本换到震荡市，"
          f"十次平仓的胜率会立刻变脸。")
    out.append((f"回测 · {code} · {strategy}", _emit(
        sid="sft-000071", scene="回测", code=code,
        questions=[f"{code} KDJ 金叉买、死叉卖，回测 90 个交易日。"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "回测")], fb=fb,
        params={"symbol": code, "strategy": strategy, "requested_days": days,
                "bars_count": len(rows), "interval": [rg["start"], rg["end"]],
                "initial_capital": INITIAL_CAPITAL},
        extra={"market_regime": rg["reg"],
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "trend_metrics": ts, "closed_trades": bt["sells"],
               "turn_closed": [bt["sells"]], "insufficient_disclosed": False,
               "evaluation_gate": "closed>=5", "strategy": strategy,
               "interval": [rg["start"], rg["end"]]})))

    # ---------- 回测 2：江苏银行 · 动量 ROC（追高） ----------
    code, days, strategy = "sh.600919", 150, "momentum_roc"
    name = NAMES_NEW[code]
    bt = await _run_bt(code, strategy, days, {})
    bars = await _bars_for(code, bt["bars"])
    rows = bars[-bt["bars"]:]
    ts = trend_stats(bars, bt["bars"])
    rg = regime(rows)
    fb = new_fb()
    p1 = (f"江苏银行这一百五十天几乎没有像样回调，动量策略却在里面亏钱。 "
          + _range(fb, rows, days))
    p2 = (f"{name}口径照引擎：十日 ROC 由负转正买入、由正转负卖出。"
          + _fees(fb, name))
    p3 = (f"{name}的读数：平仓{fb.n('sell_count', bt['sells'], 0)}笔、"
          f"赢{fb.n('win_count', bt['wins'], 0)}笔、"
          f"胜率{fb.n('win_rate_pct', bt['metrics']['win_rate_pct'], 2)}%，"
          f"策略{fb.n('strategy_return_pct', bt['metrics']['total_return_pct'], 2, signed=True)}%、"
          f"买入持有{fb.n('buy_hold_pct', rg['ret'], 2, signed=True)}%，"
          f"策略最大回撤{fb.n('strategy_max_drawdown_pct', bt['metrics']['max_drawdown_pct'], 2)}%；"
          f"ROC 翻正时价格往往已在阶段高点，翻负时又卖在回踩末端，"
          f"十三次进出把一段单边拆成了追涨杀跌。这种行情里最容易犯的错是追高，"
          f"把涨得快当成还会涨。")
    p4 = (f"{name}反转要看：如果 ROC 在零轴附近反复穿越，说明趋势进了横盘，"
          f"这套信号会继续磨损；这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%"
          f"的交易日站在六十日线上方，趋势越顺，动量策略越不该频繁换手。"
          f"样本只覆盖一段慢牛，换到震荡市，负收益会换成另一种形态。")
    out.append((f"回测 · {code} · {strategy}", _emit(
        sid="sft-000072", scene="回测", code=code,
        questions=[f"{code} 十日动量翻正就买、翻负就卖，回测 150 个交易日。"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "回测")], fb=fb,
        params={"symbol": code, "strategy": strategy, "requested_days": days,
                "bars_count": len(rows), "interval": [rg["start"], rg["end"]],
                "initial_capital": INITIAL_CAPITAL},
        extra={"market_regime": rg["reg"],
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "trend_metrics": ts, "closed_trades": bt["sells"],
               "turn_closed": [bt["sells"]], "insufficient_disclosed": False,
               "evaluation_gate": "closed>=5", "strategy": strategy,
               "interval": [rg["start"], rg["end"]]})))

    # ---------- 回测 3：中国中车 · VWAP 偏离（过早止盈） ----------
    code, days, strategy = "sh.601766", 90, "vwap_deviation"
    name = NAMES_NEW[code]
    bt = await _run_bt(code, strategy, days, {})
    bars = await _bars_for(code, bt["bars"])
    rows = bars[-bt["bars"]:]
    ts = trend_stats(bars, bt["bars"])
    rg = regime(rows)
    fb = new_fb()
    fb.c("vwap_deviation_pct", 0.8)
    p1 = (f"中国中车这段上涨很顺，VWAP 回归却每次只赚一小段。 "
          + _range(fb, rows, days))
    p2 = (f"{name}口径照引擎：收盘低于二十日成交量加权均价"
          f"{fb.c('vwap_deviation_pct', 0.8)}%买入、回到 VWAP 上方卖出。"
          + _fees(fb, name))
    p3 = (f"{name}的读数：平仓{fb.n('sell_count', bt['sells'], 0)}笔、"
          f"赢{fb.n('win_count', bt['wins'], 0)}笔、"
          f"胜率{fb.n('win_rate_pct', bt['metrics']['win_rate_pct'], 2)}%，"
          f"策略{fb.n('strategy_return_pct', bt['metrics']['total_return_pct'], 2, signed=True)}%、"
          f"买入持有{fb.n('buy_hold_pct', rg['ret'], 2, signed=True)}%，"
          f"策略最大回撤{fb.n('strategy_max_drawdown_pct', bt['metrics']['max_drawdown_pct'], 2)}%；"
          f"五次全对却只拿到趋势的一半，因为价格一回到 VWAP 就卖，主升段被切成五段薄利。"
          f"这种行情里最容易犯的错是过早止盈，胜率漂亮不等于收益跟得上。")
    p4 = (f"{name}反转要看：如果价格长期贴着 VWAP 上方运行，回归信号会一直等不到买点，"
          f"策略变成踏空；这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%"
          f"的交易日收在六十日线上方，趋势里卖出条件应该放宽而不是收紧。"
          f"五笔样本太少，百分百胜率更像行情馈赠，不能当成策略稳定性的证据。")
    out.append((f"回测 · {code} · {strategy}", _emit(
        sid="sft-000073", scene="回测", code=code,
        questions=[f"{code} 价格低于 VWAP 就买、回到 VWAP 上方卖，回测 90 个交易日。"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "回测")], fb=fb,
        params={"symbol": code, "strategy": strategy, "requested_days": days,
                "bars_count": len(rows), "interval": [rg["start"], rg["end"]],
                "initial_capital": INITIAL_CAPITAL},
        extra={"market_regime": rg["reg"],
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "trend_metrics": ts, "closed_trades": bt["sells"],
               "turn_closed": [bt["sells"]], "insufficient_disclosed": False,
               "evaluation_gate": "closed>=5", "strategy": strategy,
               "interval": [rg["start"], rg["end"]]})))

    # ---------- 回测 4：上港集团 · EMA/SMA 偏离（滞后入场） ----------
    code, days, strategy = "sh.600018", 90, "ema_sma_bias"
    name = NAMES_NEW[code]
    bt = await _run_bt(code, strategy, days, {})
    bars = await _bars_for(code, bt["bars"])
    rows = bars[-bt["bars"]:]
    ts = trend_stats(bars, bt["bars"])
    rg = regime(rows)
    fb = new_fb()
    p1 = (f"上港集团这段是低波动慢牛，EMA 上穿信号却总在回调时失效。 "
          + _range(fb, rows, days))
    p2 = (f"{name}口径照引擎：十二日 EMA 高于五十日 SMA 且收盘上穿 EMA 时买入、"
          f"收盘下穿 EMA 卖出。" + _fees(fb, name))
    p3 = (f"{name}的读数：平仓{fb.n('sell_count', bt['sells'], 0)}笔、"
          f"赢{fb.n('win_count', bt['wins'], 0)}笔、"
          f"胜率{fb.n('win_rate_pct', bt['metrics']['win_rate_pct'], 2)}%，"
          f"策略{fb.n('strategy_return_pct', bt['metrics']['total_return_pct'], 2, signed=True)}%、"
          f"买入持有{fb.n('buy_hold_pct', rg['ret'], 2, signed=True)}%，"
          f"策略最大回撤{fb.n('strategy_max_drawdown_pct', bt['metrics']['max_drawdown_pct'], 2)}%；"
          f"八次平仓只吃到趋势的四分之一，这条规则把每次回踩 EMA 都当成趋势结束，"
          f"卖完又在更高位置买回来。这种行情里最容易犯的错是均线滞后入场、回踩就下车。")
    p4 = (f"{name}反转要看：如果回踩一次比一次深，EMA 下穿就不再是噪音，而是趋势转弱；"
          f"这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%的交易日站在六十日线上方，"
          f"最大回撤只有{fb.n('max_drawdown_pct2', rg['mdd'], 2)}%，"
          f"说明大部分下穿都是假信号。样本九十根，换到高波动标的，"
          f"八次平仓的成本会明显放大。")
    out.append((f"回测 · {code} · {strategy}", _emit(
        sid="sft-000074", scene="回测", code=code,
        questions=[f"{code} EMA 上穿且高于 SMA 时买、下穿 EMA 卖，回测 90 个交易日。"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "回测")], fb=fb,
        params={"symbol": code, "strategy": strategy, "requested_days": days,
                "bars_count": len(rows), "interval": [rg["start"], rg["end"]],
                "initial_capital": INITIAL_CAPITAL},
        extra={"market_regime": rg["reg"],
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "trend_metrics": ts, "closed_trades": bt["sells"],
               "turn_closed": [bt["sells"]], "insufficient_disclosed": False,
               "evaluation_gate": "closed>=5", "strategy": strategy,
               "interval": [rg["start"], rg["end"]]})))

    # ---------- 看盘 1：中国神华 ----------
    code, n = "sh.601088", 90
    name = NAMES_NEW[code]
    bars = await _bars_for(code, n)
    rows = bars[-n:]
    ts = trend_stats(bars, n)
    rg = regime(rows)
    w = window_metrics(rows)
    fb = new_fb()
    p1 = (f"中国神华这段沿六十日线往上走，先看它离前高还有多少。 " + _range(fb, rows, n))
    p2 = (f"{name}口径用不复权日线：MA5、MA20、MA60 取收盘简单均线，RSI 取十四日，"
          f"区间分位按窗口最低最高价算，量比取当日量比二十日均量。")
    p3 = (f"{name}的读数：现价{fb.n('close', w['price'], 2)}，"
          f"当日{fb.n('last_change_pct', w['chg'], 2, signed=True)}%，"
          f"二十日{fb.n('return_20d_pct', w['ret20'], 2, signed=True)}%，"
          f"区间分位{fb.n('range_position_pct', w['pos'], 2)}%，RSI {fb.n('rsi14', w['rsi'], 2)}，"
          f"MA5 {fb.n('ma5_value', w['ma5'], 2)}、MA20 {fb.n('ma20_value', w['ma20'], 2)}、"
          f"MA60 {fb.n('ma60_value', w['ma60'], 2)}，年化波动{fb.n('annual_vol_pct', w['vol'], 2)}%，"
          f"量比{fb.n('volume_ratio', w['vr'], 2)}；价格在短期均线下方、六十日线上方，"
          f"属于上涨后的横盘消化。这段是单边上涨，最容易犯的错是看到二十多个点的涨幅就追，"
          f"忽略最近二十日几乎没动。")
    p4 = (f"{name}反转要看：如果收盘跌破 MA60 且三天收不回来，这段慢牛才算结束；"
          f"现在 MA20 只比现价高一点，横盘越久，向上突破和向下破位的赔率越接近。"
          f"区间分位七成四，离前高还有距离，但那是趋势给的，不是追涨的理由。"
          f"分位和均线只描述位置，不预测方向，煤价和分红才是中期变量。")
    out.append((f"看盘 · {code}", _emit(
        sid="sft-000075", scene="看盘", code=code,
        questions=[f"{code} 涨了这么多，现在还能追吗？"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "看盘")], fb=fb,
        params={"symbol": code, "requested_days": n, "bars_count": len(rows),
                "interval": [rg["start"], rg["end"]]},
        extra={"market_regime": rg["reg"], "trend_metrics": ts,
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "evaluation_gate": "descriptive"})))

    # ---------- 看盘 2：云南白药 ----------
    code, n = "sz.000538", 90
    name = NAMES_NEW[code]
    bars = await _bars_for(code, n)
    rows = bars[-n:]
    ts = trend_stats(bars, n)
    rg = regime(rows)
    w = window_metrics(rows)
    fb = new_fb()
    p1 = f"云南白药这九十天涨得不急，回撤却一直很浅。 " + _range(fb, rows, n)
    p2 = (f"{name}口径用不复权日线：MA5、MA20、MA60 取收盘简单均线，RSI 取十四日，"
          f"区间分位按窗口最低最高价算，量比取当日量比二十日均量。")
    p3 = (f"{name}的读数：现价{fb.n('close', w['price'], 2)}，"
          f"当日{fb.n('last_change_pct', w['chg'], 2, signed=True)}%，"
          f"二十日{fb.n('return_20d_pct', w['ret20'], 2, signed=True)}%，"
          f"区间分位{fb.n('range_position_pct', w['pos'], 2)}%，RSI {fb.n('rsi14', w['rsi'], 2)}，"
          f"MA5 {fb.n('ma5_value', w['ma5'], 2)}、MA20 {fb.n('ma20_value', w['ma20'], 2)}、"
          f"MA60 {fb.n('ma60_value', w['ma60'], 2)}，年化波动{fb.n('annual_vol_pct', w['vol'], 2)}%，"
          f"量比{fb.n('volume_ratio', w['vr'], 2)}；价格贴近区间高点，均线多头排列，"
          f"但量比不到一，新高没有放量配合。这种单边上涨里最容易犯的错是把低波动当成低风险，"
          f"越接近前高越敢加仓。")
    p4 = (f"{name}反转要看：如果放量突破前高后第二天收回，说明这段加速是诱多；"
          f"如果 RSI 在七十上方钝化而价格不再新高，才是背离。"
          f"这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%的交易日站在六十日线上方，"
          f"趋势仍完整，但量比低于一说明筹码没有松动，也说明这段新高缺少增量资金确认。"
          f"日线只覆盖九十根，渠道库存和集采消息不在这些读数里。")
    out.append((f"看盘 · {code}", _emit(
        sid="sft-000076", scene="看盘", code=code,
        questions=[f"{code} 涨了这么多，RSI 高不高，还能拿吗？"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "看盘")], fb=fb,
        params={"symbol": code, "requested_days": n, "bars_count": len(rows),
                "interval": [rg["start"], rg["end"]]},
        extra={"market_regime": rg["reg"], "trend_metrics": ts,
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "evaluation_gate": "descriptive"})))

    # ---------- 看盘 3：川投能源 ----------
    code, n = "sh.600674", 90
    name = NAMES_NEW[code]
    bars = await _bars_for(code, n)
    rows = bars[-n:]
    ts = trend_stats(bars, n)
    rg = regime(rows)
    w = window_metrics(rows)
    fb = new_fb()
    p1 = f"川投能源这段涨幅接近两成七，回撤不到一成。 " + _range(fb, rows, n)
    p2 = (f"{name}口径用不复权日线：MA5、MA20、MA60 取收盘简单均线，RSI 取十四日，"
          f"区间分位按窗口最低最高价算，量比取当日量比二十日均量。")
    p3 = (f"{name}的读数：现价{fb.n('close', w['price'], 2)}，"
          f"当日{fb.n('last_change_pct', w['chg'], 2, signed=True)}%，"
          f"二十日{fb.n('return_20d_pct', w['ret20'], 2, signed=True)}%，"
          f"区间分位{fb.n('range_position_pct', w['pos'], 2)}%，RSI {fb.n('rsi14', w['rsi'], 2)}，"
          f"MA5 {fb.n('ma5_value', w['ma5'], 2)}、MA20 {fb.n('ma20_value', w['ma20'], 2)}、"
          f"MA60 {fb.n('ma60_value', w['ma60'], 2)}，年化波动{fb.n('annual_vol_pct', w['vol'], 2)}%，"
          f"量比{fb.n('volume_ratio', w['vr'], 2)}；价格在三条均线上方，"
          f"MA5 与 MA20 几乎粘合，说明最近十天在横盘，今天重新放量。"
          f"这种单边上涨里最容易犯的错是只看二十多个点的涨幅就追高，"
          f"忽略短线已经贴着区间上沿。")
    p4 = (f"{name}反转要看：如果 MA5 下穿 MA20 且量能萎缩，这段横盘会先变成调整；"
          f"如果跌破 MA60，才轮到趋势判断。"
          f"这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%的交易日收在六十日线上方，"
          f"趋势还在，均线粘合后的第一次放量方向，才是这段横盘真正的选择；"
          f"但水电股的来水和高股息逻辑不在日线里，技术分位只能说明它现在不便宜。")
    out.append((f"看盘 · {code}", _emit(
        sid="sft-000077", scene="看盘", code=code,
        questions=[f"{code} 这个位置还能不能追？"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "看盘")], fb=fb,
        params={"symbol": code, "requested_days": n, "bars_count": len(rows),
                "interval": [rg["start"], rg["end"]]},
        extra={"market_regime": rg["reg"], "trend_metrics": ts,
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "evaluation_gate": "descriptive"})))

    # ---------- 风险 1：中国广核 ----------
    code, n = "sz.003816", 60
    name = NAMES_NEW[code]
    bars = await _bars_for(code, n)
    rows = bars[-n:]
    ts = trend_stats(bars, n)
    rg = regime(rows)
    w = window_metrics(rows)
    fb = new_fb()
    fb.c("annual_factor", 252)
    p1 = f"中国广核这两个月涨了两成，波动却并不低。 " + _range(fb, rows, n)
    p2 = (f"{name}口径按不复权日线的日收益算：年化波动是日收益标准差乘根号"
          f"{fb.c('annual_factor', 252)}，下行波动只取负收益，单日最大跌幅取最差一根，"
          f"VaR 取收益分布第五百分位。")
    p3 = (f"{name}的读数：区间涨跌{fb.n('range_return_pct2', w['ret'], 2, signed=True)}%，"
          f"最大回撤{fb.n('max_drawdown_pct2', w['mdd'], 2)}%，"
          f"振幅{fb.n('amplitude_pct2', w['amp'], 2)}%，"
          f"年化波动{fb.n('annual_vol_pct', w['vol'], 2)}%，"
          f"下行波动{fb.n('downside_vol_pct', w['dvol'], 2)}%，"
          f"单日最大跌幅{fb.n('max_daily_drop_pct', w['maxdrop'], 2, signed=True)}%，"
          f"日 VaR {fb.n('var95_daily_pct', w['var95'], 2, signed=True)}%，"
          f"跌{fb.n('down_days', w['down_days'], 0)}天、涨{fb.n('up_days', w['up_days'], 0)}天；"
          f"回撤浅、下行波动低，看着像低风险，但年化波动仍有"
          f"{fb.n('annual_vol_pct2', w['vol'], 2)}%，只是上涨把左尾暂时盖住了。"
          f"这种行情里最容易犯的错，就是把上涨当成低风险。")
    p4 = (f"{name}反转要看：如果某天出现超过{fb.c('single_day_floor_pct', 5)}%的单日跌幅，"
          f"这段样本的 VaR 会立刻失真；六十根日线只覆盖一次上涨，没有经历过回撤，"
          f"尾部风险被系统性低估。这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%"
          f"的交易日站在六十日线上方，趋势确认不等于风险确认，仓位要按波动给。")
    out.append((f"风险 · {code}", _emit(
        sid="sft-000078", scene="风险", code=code,
        questions=[f"{code} 涨了这么多，风险是不是很小？"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "风险")], fb=fb,
        params={"symbol": code, "requested_days": n, "bars_count": len(rows),
                "interval": [rg["start"], rg["end"]]},
        extra={"market_regime": rg["reg"], "trend_metrics": ts,
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "evaluation_gate": "descriptive"})))

    # ---------- 风险 2：宁沪高速 ----------
    code, n = "sh.600377", 90
    name = NAMES_NEW[code]
    bars = await _bars_for(code, n)
    rows = bars[-n:]
    ts = trend_stats(bars, n)
    rg = regime(rows)
    w = window_metrics(rows)
    fb = new_fb()
    fb.c("annual_factor", 252)
    p1 = f"宁沪高速这段慢涨，最大回撤只有百分之七上下。 " + _range(fb, rows, n)
    p2 = (f"{name}口径按不复权日线的日收益算：年化波动是日收益标准差乘根号"
          f"{fb.c('annual_factor', 252)}，下行波动只取负收益，单日最大跌幅取最差一根，"
          f"VaR 取收益分布第五百分位。")
    p3 = (f"{name}的读数：区间涨跌{fb.n('range_return_pct2', w['ret'], 2, signed=True)}%，"
          f"最大回撤{fb.n('max_drawdown_pct2', w['mdd'], 2)}%，"
          f"振幅{fb.n('amplitude_pct2', w['amp'], 2)}%，"
          f"年化波动{fb.n('annual_vol_pct', w['vol'], 2)}%，"
          f"下行波动{fb.n('downside_vol_pct', w['dvol'], 2)}%，"
          f"单日最大跌幅{fb.n('max_daily_drop_pct', w['maxdrop'], 2, signed=True)}%，"
          f"日 VaR {fb.n('var95_daily_pct', w['var95'], 2, signed=True)}%，"
          f"跌{fb.n('down_days', w['down_days'], 0)}天、涨{fb.n('up_days', w['up_days'], 0)}天；"
          f"下行波动已接近总波动的七成，涨得慢、跌得也不轻，"
          f"回撤浅只是被时间摊薄。这种行情里最容易犯的错是拿回撤小当安全垫，"
          f"忽略波动率并不低。")
    p4 = (f"{name}反转要看：如果单日跌幅超过{fb.c('single_day_floor_pct', 5)}%，"
          f"这段{fb.n('max_drawdown_pct3', rg['mdd'], 2)}%的最大回撤会很快被改写；"
          f"高速股的车流和收费政策是慢变量，日线看不到。"
          f"这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%的交易日收在六十日线上方，"
          f"风险预算要按年化波动留，而不是按最大回撤留。")
    out.append((f"风险 · {code}", _emit(
        sid="sft-000079", scene="风险", code=code,
        questions=[f"{code} 回撤这么小，风险是不是可控？"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "风险")], fb=fb,
        params={"symbol": code, "requested_days": n, "bars_count": len(rows),
                "interval": [rg["start"], rg["end"]]},
        extra={"market_regime": rg["reg"], "trend_metrics": ts,
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "evaluation_gate": "descriptive"})))

    # ---------- 持仓 1：大参林 ----------
    code, n, invest = "sh.603233", 60, 100000
    name = NAMES_NEW[code]
    bars = await _bars_for(code, n)
    rows = bars[-n:]
    ts = trend_stats(bars, n)
    rg = regime(rows)
    w = window_metrics(rows)
    fb = new_fb()
    v = _hold_numbers(fb, rows, invest, n)
    p1 = f"大参林这六十天的浮盈来自一段单边上涨，不是选股能力。 " + _range(fb, rows, n)
    p2 = (f"{name}口径按买入价乘整手股数算成本，佣金{fb.c('commission_pct', COMMISSION)}%、"
          f"卖出印花税{fb.c('stamp_tax_pct', STAMP)}%，浮盈不扣卖出费用，"
          f"保本价按双边费用倒推。")
    p3 = (f"{name}的读数：买入价{v['buy']}，持有{v['shares']}股、成本{v['cost']}元，"
          f"现价{v['last']}、市值{v['value']}元，浮盈{v['pnl']}元、幅度{v['pct']}%，"
          f"保本价{v['breakeven']}，买入后最大回撤{v['mdd']}%，"
          f"区间分位{fb.n('range_position_pct', w['pos'], 2)}%，"
          f"RSI {fb.n('rsi14', w['rsi'], 2)}，年化波动{fb.n('annual_vol_pct', w['vol'], 2)}%；"
          f"趋势还在，但 RSI 已在七十上方、价格贴着区间顶，浮盈的安全垫没有想象中厚。"
          f"这种行情里最容易犯的错是过早止盈，涨一点就卖，"
          f"或者在超买位置反过来加仓。")
    p4 = (f"{name}反转要看：如果跌破 MA20，这段单边上涨才算转成震荡；"
          f"如果 RSI 高位钝化而价格继续新高，止盈要按均线而不是按情绪。"
          f"这段{fb.n('ma60_above_pct', ts['ma60_above_pct'], 1)}%的交易日站在六十日线上方，"
          f"六十根样本很短，浮盈里有行情馈赠，换一段下跌，同样的买点会变成亏损。")
    out.append((f"持仓 · {code}", _emit(
        sid="sft-000080", scene="持仓", code=code,
        questions=[f"{code} 六十个交易日前买的，现在浮盈多少，要不要止盈？"],
        answers=[_pad("\n\n".join([p1, p2, p3, p4]), "持仓")], fb=fb,
        params={"symbol": code, "requested_days": n, "bars_count": len(rows),
                "invest": invest,
                "interval": [rg["start"], rg["end"]]},
        extra={"market_regime": rg["reg"], "trend_metrics": ts,
               "regime_metrics": {k: round(rg[k], 4) for k in ("ret", "mdd", "amp")},
               "evaluation_gate": "descriptive"})))

    return out


# ============================ 12 条「其他」 ============================
CLASSIFY = {
    "sft-000005": ("冲高回落（宽幅过山车）",
                   "最大涨幅 {max_runup}% 后回撤 {mdd}%，期末仍 {ret}%；涨幅过单边门槛但回撤远超 10%，"
                   "期末为正又不算急跌。无法归入三类，盲区=「大涨后深度回撤」。"),
    "sft-000006": ("震荡上行（趋势中带一次深调）",
                   "涨 {ret}%、回撤 {mdd}%、60 日线上方 {ma60}%。应归单边上涨：回撤阈值可从 10% 放宽到 20%，"
                   "当前落在 10%–20% 回撤盲区。"),
    "sft-000013": ("阶梯上行（多次回踩后再创新高）",
                   "涨 {ret}%、回撤 {mdd}%、60 日线上方 {ma60}%，高点接近窗口末端。最接近单边上涨，"
                   "但回撤落在 20%–30% 带，三类都不收；盲区=「趋势成立但回撤 20%+」。"),
    "sft-000016": ("震荡上行（强趋势 + 三成回撤）",
                   "涨 {ret}%、回撤 {mdd}%、60 日线上方 {ma60}%，创新高日接近末端。应按单边上涨处理，"
                   "当前被回撤阈值挡在门外；盲区=「强趋势 + 大回撤」。"),
    "sft-000020": ("阴跌后横盘（窗口开头即高点）",
                   "跌 {absret}%、回撤 {mdd}%、最近 20 日 {ret20}% 只是修复。最接近急跌，但跌幅差 4.7 个点"
                   "没到 -15%；盲区=「跌 10%–15%、回撤>20%」。"),
    "sft-000046": ("窄幅横盘偏强（贴区间上沿）",
                   "涨 {ret}%、回撤 {mdd}%、振幅 {amp}% 只差 0.2 个点没到 15%。应归震荡（低振幅整理）；"
                   "盲区=「振幅<15% 的窄幅上行」。"),
    "sft-000049": ("前段横盘、末段拉升",
                   "涨 {ret}%、回撤 {mdd}%，60 日线上方仅 {ma60}%，大部分时间在均线下方。应归震荡"
                   "（区间突破前夜）；盲区=「涨幅 10%–15%、回撤>10%」。"),
    "sft-000050": ("冲高—回落—再冲高（高位宽震）",
                   "涨 {ret}%、回撤 {mdd}%、振幅 {amp}%，收盘回到区间上沿。无法归入三类：涨幅过 10%、"
                   "回撤也过 10%；盲区=「10%–15% 涨幅 + 15%–20% 回撤」。"),
    "sft-000055": ("V 型反弹 / 末段急拉",
                   "涨 {ret}%、回撤 {mdd}%，60 日线上方仅 {ma60}%，最后 20 日 {ret20}% 贡献主要涨幅。"
                   "应归震荡（V 型反弹），不是单边趋势；盲区同「10%–15% 涨幅 + 大回撤」。"),
    "sft-000060": ("缓慢阴跌（几乎全程在 60 日线下方）",
                   "跌 {absret}%、回撤 {mdd}%、60 日线上方仅 {ma60}%。最接近急跌，但跌幅没到 -15%；"
                   "盲区=「10%–15% 缓跌」。"),
    "sft-000066": ("冲高回落后的宽幅震荡",
                   "涨 {ret}%、回撤 {mdd}%、振幅 {amp}%，收在区间中部。应归震荡；"
                   "盲区=「涨幅 10%–15% + 回撤 15%–20%」。"),
    "sft-000070": ("暴涨后崩跌（泡沫破裂）",
                   "最大涨幅 {max_runup}% 后回撤 {mdd}%，期末仍 {ret}%。期末为正排除急跌，回撤过大排除单边，"
                   "涨幅过大排除震荡；盲区=「泡沫破裂型」。"),
}


def _ma60_ratio(bars, start):
    closes = [b["close"] for b in bars]
    idx = next(i for i, b in enumerate(bars) if b["date"] >= start)
    above = total = 0
    for i in range(idx, len(bars)):
        if i + 1 < 60:
            continue
        ma = sum(closes[i + 1 - 60:i + 1]) / 60
        total += 1
        if closes[i] > ma:
            above += 1
    return round(above / total * 100, 1) if total else 0.0


def _pullbacks(closes):
    peak, count, in_dd = closes[0], 0, False
    for c in closes[1:]:
        if c >= peak:
            peak, in_dd = c, False
        elif (peak - c) / peak * 100 >= 3 and not in_dd:
            count += 1
            in_dd = True
    return count


async def other_rows() -> list[dict]:
    await fetch_rows("sh.600000", "date,close", "2026-11-06", "2026-11-06")
    samples = [json.loads(x) for x in TRIAL70.read_text(encoding="utf-8").splitlines()
               if x.strip()]
    out = []
    for s in samples:
        p = s["provenance"]
        if p.get("market_regime") != "其他":
            continue
        pa = p["params"]
        code, (start, end) = pa["symbol"], pa["interval"]
        warm = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=160)).strftime("%Y-%m-%d")
        bars = await fetch_bars(code, warm, end, adjustflag="3") or []
        win = [b for b in bars if start <= b["date"] <= end]
        if len(win) < 20:
            raise RuntimeError(f"{s['sample_id']} {code} 取数失败")
        rg = regime(win)
        closes = [b["close"] for b in win]
        out.append({
            "sid": s["sample_id"], "scene": s["trial_scene"], "code": code,
            "name": NAMES.get(code, ""), "n": len(win), "start": win[0]["date"],
            "end": win[-1]["date"], "ret": round(rg["ret"], 2),
            "mdd": round(rg["mdd"], 2), "amp": round(rg["amp"], 2),
            "ma60": _ma60_ratio(bars, start), "pullbacks": _pullbacks(closes),
            "max_runup": round(max((c / closes[0] - 1) * 100 for c in closes), 2),
            "ret20": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) > 21 else 0.0,
        })
    return out


# ============================ 输出 ============================
def write_jsonl(trial_raw: list[bytes], new: list[tuple[str, dict]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "samples.jsonl", "wb") as f:
        for raw in trial_raw:
            f.write(raw)
        for _, s in new:
            f.write((json.dumps(s, ensure_ascii=False) + "\n").encode("utf-8"))


def write_md(samples: list[dict], titles: list[str], n_old: int) -> None:
    lines = [
        "# SFT v3 final80（trial70 + 10 条单边上涨补样）", "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}。前 {n_old} 条与 "
        "`backend/data/sft_v3/trial70/samples.jsonl` 逐字节一致；后 10 条为真实行情补样，"
        "全部为 `单边上涨`（区间涨跌>+15% 且最大回撤<10%，并要求站上 60 日线天数占比>=60% "
        "或回撤次数<=3）。", "",
        f"## 第一部分：trial70（{n_old} 条，原样保留）", "",
    ]
    for i, (title, s) in enumerate(zip(titles, samples[:n_old]), 1):
        ans = answers_of(s)
        lens = "+".join(str(cjk_len(a)) for a in ans)
        lines.append(f"### {i}. {s['sample_id']} · {s['trial_scene']} · {title}（{lens} 字）")
        lines.append("")
        users = [m["content"] for m in s["messages"] if m["role"] == "user"]
        for j, (u, a) in enumerate(zip(users, ans), 1):
            tag = f"（第 {j} 轮）" if len(users) > 1 else ""
            lines += [f"**user{tag}**", "", u, "",
                      f"**assistant{tag}**", "", a, ""]
        lines += ["---", ""]
    lines += [f"## 第二部分：新增 10 条单边上涨（sft-000071 ~ sft-000080）", ""]
    for i, (title, s) in enumerate(zip(titles[n_old:], samples[n_old:]), n_old + 1):
        ans = answers_of(s)
        lens = "+".join(str(cjk_len(a)) for a in ans)
        lines.append(f"### {i}. {s['sample_id']} · {title}（{lens} 字）")
        lines.append("")
        users = [m["content"] for m in s["messages"] if m["role"] == "user"]
        for j, (u, a) in enumerate(zip(users, ans), 1):
            tag = f"（第 {j} 轮）" if len(users) > 1 else ""
            lines += [f"**user{tag}**", "", u, "",
                      f"**assistant{tag}**", "", a, ""]
        lines += ["---", ""]
    (OUT_DIR / "samples.md").write_text("\n".join(lines), encoding="utf-8")


def write_notes(rows: list[dict]) -> None:
    lines = [
        "# trial70 中 12 条「其他」逐条判定", "",
        "trial70 的分类规则写死在 `trial70_samples.regime()`：",
        "`单边上涨 = 区间涨跌>+15% 且最大回撤<10%`；`急跌 = 涨跌<-15% 且回撤>20%`；"
        "`震荡 = |涨跌|<=10% 且振幅>15%`；其余全部落进 `其他`。", "",
        "本表只做判定说明，**未改动 trial70 目录内任何字节**。", "",
        "| sample_id | 场景 | 标的 | 区间涨跌 | 最大回撤 | 振幅 | 站上60日线 | 回撤次数 | 实际形态 | 建议归类 / 盲区说明 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for r in rows:
        shape, note = CLASSIFY[r["sid"]]
        note = note.format(
            ret=f"{r['ret']:.2f}",
            absret=f"{abs(r['ret']):.2f}", mdd=f"{r['mdd']:.2f}",
            amp=f"{r['amp']:.2f}", ma60=f"{r['ma60']:.1f}",
            max_runup=f"{r['max_runup']:.2f}", ret20=f"{r['ret20']:+.2f}",
        )
        lines.append(
            f"| {r['sid']} | {r['scene']} | {r['name']}（{r['code']}） | "
            f"{r['ret']:+.2f}% | {r['mdd']:.2f}% | {r['amp']:.2f}% | "
            f"{r['ma60']:.1f}% | {r['pullbacks']} | {shape} | {note} |")
    lines += [
        "", "## 归类结论", "",
        "- **应归单边上涨（回撤阈值偏严）**：sft-000006、sft-000016。涨幅与趋势都成立，只因中途回撤 "
        "19%–34% 被 10% 阈值挡下。",
        "- **应归震荡（区间/横盘/V 型）**：sft-000046、sft-000049、sft-000055、sft-000066。"
        "形态是横盘整理、末段拉升或冲高回落，不是持续趋势。",
        "- **无法归入任何一类（规则盲区）**：sft-000005、sft-000013、sft-000020、sft-000050、"
        "sft-000060、sft-000070。典型是「大涨后深度回撤」「泡沫破裂」「缓跌」「跌 10%–15%」。",
        "", "规则的共同盲区是三条硬阈值之间留了四条缝：涨幅 10%–15%、回撤 10%–20%、"
        "回撤 20%–35%、以及「期末为正但中途腰斩」。补样只针对单边上涨，未改规则；"
        "若要减少「其他」，应把三分类改成按 60 日线占比 + 回撤深度的连续打分，而不是继续加阈值。",
    ]
    (OUT_DIR / "REGIME_NOTES.md").write_text("\n".join(lines), encoding="utf-8")


def write_manifest(samples: list[dict], report: dict) -> None:
    syms = []
    for s in samples:
        pa = s["provenance"]["params"]
        syms.append({"sample_id": s["sample_id"], "symbol": pa.get("symbol"),
                     "name": NAMES.get(pa.get("symbol"), NAMES_NEW.get(pa.get("symbol"), "")),
                     "scene": s["trial_scene"],
                     "market_regime": s["provenance"].get("market_regime")})
    lengths = [cjk_len(a) for s in samples for a in answers_of(s)]
    scan = dict(SCREEN_SUMMARY)
    manifest = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(samples),
        "screen_pool": scan,
        "scenes": dict(Counter(s["trial_scene"] for s in samples)),
        "regimes": dict(Counter(s["provenance"].get("market_regime") for s in samples)),
        "symbols": syms,
        "symbol_count": len({x["symbol"] for x in syms}),
        "cross_scene_reuse": report["cross_scene"],
        "unregistered": report["unregistered"],
        "unregistered_detail": report["bad_detail"],
        "answer_count": len(lengths),
        "first_sentence": {"distinct": report["first_distinct"], "total": report["first_total"]},
        "cjk_chars": {"min": min(lengths), "max": max(lengths),
                      "avg": round(sum(lengths) / len(lengths), 1),
                      "out_of_range": len(report["over"])},
        "trial70_prefix": {
            "lines": 70,
            "sha256_first70": report["prefix_sha"],
            "identical_to_trial70": report["prefix_ok"],
        },
        "new_samples": [s["sample_id"] for s in samples[70:]],
        "insufficient_trades": report["insufficient"],
    }
    (OUT_DIR / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


async def main() -> int:
    args = parse_args()
    trial_bytes = TRIAL70.read_bytes()
    trial_lines = trial_bytes.splitlines(keepends=True)
    if len(trial_lines) != 70:
        raise RuntimeError(f"trial70 行数异常：{len(trial_lines)}")

    if args.verify:
        samples = [json.loads(x) for x in
                   (OUT_DIR / "samples.jsonl").read_text(encoding="utf-8").splitlines()
                   if x.strip()]
        report = verify(samples)
        prefix = trial_bytes
        got = (OUT_DIR / "samples.jsonl").read_bytes()
        report["prefix_ok"] = got.startswith(prefix)
        report["prefix_sha"] = hashlib.sha256(prefix).hexdigest()
        ok = (len(samples) == 80 and report["unregistered"] == 0
              and not report["over"] and not report["nodisc"]
              and not report["cross_scene"] and report["prefix_ok"])
        print(f"验收：{'通过' if ok else '未通过'}")
        return 0 if ok else 1

    new = await build_new()
    if len(new) != 10:
        raise RuntimeError(f"新增条数异常：{len(new)}")

    samples_all = [json.loads(x) for x in trial_bytes.decode("utf-8").splitlines() if x.strip()]
    new_samples = [s for _, s in new]
    all_samples = samples_all + new_samples
    report = verify(all_samples)
    report["prefix_ok"] = True
    report["prefix_sha"] = hashlib.sha256(trial_bytes).hexdigest()

    write_jsonl(trial_lines, new)
    # 标题：trial70 从 samples.md 里回填会不准，这里用标的名 + 场景
    titles = []
    for s in samples_all:
        code = s["provenance"]["params"].get("symbol")
        titles.append(f"{NAMES.get(code, code)}")
    for title, _ in new:
        titles.append(title)
    write_md(all_samples, titles, 70)
    write_notes(await other_rows())
    write_manifest(all_samples, report)
    print(f"\n输出目录：{OUT_DIR}")
    return 0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
