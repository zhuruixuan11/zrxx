"""衡策回测引擎适配层：内置策略库 + baostock 数据源。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

from loguru import logger

from ..agent.backtest import fetch_history_bars
from .registry import get_strategy_class, list_registered_strategies


# ========== 交易成本默认值（A 股现行规则）==========
ANN_FACTOR = 252
DEFAULT_COMMISSION = 0.0003        # 佣金万三（券商可调）
DEFAULT_MIN_COMMISSION = 5.0       # 单笔最低佣金 5 元
DEFAULT_STAMP_TAX = 0.001          # 印花税千一，仅卖出
DEFAULT_TRANSFER_FEE = 0.00001     # 过户费 0.001%，买卖双边
DEFAULT_SLIPPAGE = 0.0005          # 滑点万五，买入上浮/卖出下浮


# ========== 策略元信息（前端下拉用）==========
# 说明：id 与策略注册名一致，params 为各策略支持的参数及默认值。
STRATEGY_META: Dict[str, Dict[str, Any]] = {
    "ma_cross": {
        "name": "均线金叉死叉", "group": "趋势",
        "desc": "短期均线上穿长期均线买入，下穿卖出",
        "params": [("short_window", 5, "短期均线"), ("long_window", 20, "长期均线")],
    },
    "ema_sma_bias": {
        "name": "EMA/SMA 偏离", "group": "趋势",
        "desc": "EMA 相对 SMA 的偏离度做多空判断",
        "params": [("ema_span", 12, "EMA 周期"), ("sma_window", 50, "SMA 周期"),
                   ("bias_threshold", 0.0, "偏离阈值")],
    },
    "triple_ma": {
        "name": "三均线系统", "group": "趋势",
        "desc": "短中长三均线多头排列才持有",
        "params": [("short_window", 5, "短期"), ("mid_window", 20, "中期"), ("long_window", 60, "长期")],
    },
    "macd": {
        "name": "MACD 金叉死叉", "group": "趋势",
        "desc": "DIF 上穿 DEA 买入，下穿卖出",
        "params": [("fast_period", 12, "快线"), ("slow_period", 26, "慢线"), ("signal_period", 9, "信号线")],
    },
    "adx_macd": {
        "name": "ADX 过滤 + MACD", "group": "趋势",
        "desc": "ADX 强趋势时才接受 MACD 信号，过滤震荡假突破",
        "params": [("adx_period", 14, "ADX 周期"), ("min_adx", 22.0, "ADX 阈值"),
                   ("fast_period", 12, "快线"), ("slow_period", 26, "慢线"), ("signal_period", 9, "信号线")],
    },
    "donchian_breakout": {
        "name": "唐奇安通道突破", "group": "突破",
        "desc": "突破 N 日最高价买入，跌破 N 日最低价卖出",
        "params": [("channel_period", 20, "通道周期")],
    },
    "turtle": {
        "name": "海龟交易法则", "group": "突破",
        "desc": "20 日突破入场，10 日跌破出场，ATR 止损",
        "params": [("entry_period", 20, "入场周期"), ("exit_period", 10, "出场周期"),
                   ("atr_period", 14, "ATR 周期"), ("atr_stop_mult", 0.0, "ATR 止损倍数(0=不用)")],
    },
    "volume_breakout": {
        "name": "量价突破", "group": "突破",
        "desc": "放量突破 N 日高点买入，跌破出场周期低点卖出",
        "params": [("breakout_period", 20, "突破周期"), ("vol_ma_period", 20, "量均线"),
                   ("volume_mult", 1.5, "放量倍数"), ("exit_period", 20, "出场周期")],
    },
    "boll_breakout": {
        "name": "布林带突破", "group": "突破",
        "desc": "收盘突破布林上轨买入，跌破中轨卖出",
        "params": [("period", 20, "布林周期"), ("std_dev", 2.0, "标准差倍数")],
    },
    "boll_reversion": {
        "name": "布林带回归", "group": "均值回归",
        "desc": "跌破布林下轨买入（超卖），回到中轨卖出",
        "params": [("period", 20, "布林周期"), ("std_dev", 2.0, "标准差倍数")],
    },
    "rsi": {
        "name": "RSI 超卖超买", "group": "均值回归",
        "desc": "RSI 低于下阈值买入，高于上阈值卖出",
        "params": [("window", 14, "RSI 周期"), ("lower", 30, "超卖线"), ("upper", 70, "超买线")],
    },
    "rsi_ma200": {
        "name": "RSI + MA200 趋势过滤", "group": "均值回归",
        "desc": "价格在 MA200 之上且 RSI 超卖时买入",
        "params": [("ma_window", 200, "MA 周期"), ("window", 14, "RSI 周期"),
                   ("lower", 30, "超卖线"), ("upper", 70, "超买线")],
    },
    "kdj": {
        "name": "KDJ 金叉死叉", "group": "震荡",
        "desc": "K 上穿 D 且未超买时买入，死叉或超买卖出",
        "params": [("k_period", 9, "K 周期"), ("d_period", 3, "D 周期"), ("j_period", 3, "J 周期")],
    },
    "stochastic": {
        "name": "随机指标 STOCH", "group": "震荡",
        "desc": "K 上穿 D 在超卖区买入，超买区卖出",
        "params": [("k_period", 14, "K 周期"), ("d_period", 3, "D 周期"),
                   ("oversold_level", 30.0, "超卖"), ("overbought_level", 70.0, "超买")],
    },
    "cci": {
        "name": "CCI 顺势指标", "group": "震荡",
        "desc": "CCI 由下向上穿越 -100 买入，上穿 +100 卖出",
        "params": [("period", 20, "CCI 周期"), ("oversold", -100.0, "超卖"), ("overbought", 100.0, "超买")],
    },
    "williams_r": {
        "name": "威廉指标 W%R", "group": "震荡",
        "desc": "W%R 超卖区买入，超买区卖出",
        "params": [("period", 14, "周期"), ("oversold", -80.0, "超卖"), ("overbought", -20.0, "超买")],
    },
    "momentum_roc": {
        "name": "动量 ROC", "group": "动量",
        "desc": "N 日变动率由负转正买入，由正转负卖出",
        "params": [("roc_window", 10, "ROC 周期")],
    },
    "obv_cross": {
        "name": "OBV 能量潮", "group": "量价",
        "desc": "OBV 上穿其均线买入，下穿卖出",
        "params": [("obv_ma_period", 30, "OBV 均线周期")],
    },
    "vwap_deviation": {
        "name": "VWAP 偏离", "group": "量价",
        "desc": "价格低于 VWAP 一定偏离买入，回归卖出",
        "params": [("period", 20, "VWAP 周期"), ("deviation", 0.008, "偏离比例")],
    },
    "ma_cross_atr_stop": {
        "name": "均线 + ATR 止损", "group": "风控",
        "desc": "均线金叉买入，ATR 倍数移动止损",
        "params": [("short_window", 5, "短期"), ("long_window", 20, "长期"),
                   ("atr_period", 14, "ATR 周期"), ("atr_stop_mult", 2.0, "ATR 止损倍数")],
    },
    "vol_target_ma_cross": {
        "name": "波动率目标仓位", "group": "风控",
        "desc": "按 ATR 波动率动态调整仓位（风险预算）",
        "params": [("short_window", 5, "短期"), ("long_window", 20, "长期"),
                   ("atr_period", 14, "ATR 周期"), ("risk_budget_pct", 2.0, "风险预算%"),
                   ("max_fraction", 1.0, "单次最大仓位")],
    },
    "kelly_ma_cross": {
        "name": "凯利公式仓位", "group": "风控",
        "desc": "均线信号 + 固定比例凯利仓位",
        "params": [("short_window", 5, "短期"), ("long_window", 20, "长期"),
                   ("equity_fraction", 0.35, "仓位比例")],
    },
    "cross_section_momentum": {
        "name": "截面动量（多标的）", "group": "多标的",
        "desc": "需多标的截面回测，单标的模式下不可用",
        "params": [],
        "disabled": True,
    },
}

# 兼容旧参数名
_PARAM_ALIASES = {
    "short_period": "short_window",
    "long_period": "long_window",
}


def list_strategies() -> List[Dict[str, Any]]:
    """返回全部策略元信息（按分组）。"""
    out = []
    registered = set(list_registered_strategies())
    for sid, meta in STRATEGY_META.items():
        out.append({
            "id": sid,
            "name": meta["name"],
            "group": meta.get("group", ""),
            "desc": meta["desc"],
            "disabled": meta.get("disabled", False) or sid not in registered,
            "params": [
                {"name": p[0], "default": p[1], "desc": p[2]} for p in meta["params"]
            ],
        })
    return out


def _normalize_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    p = dict(params or {})
    for old, new in _PARAM_ALIASES.items():
        if old in p and new not in p:
            p[new] = p.pop(old)
    return p


def _bars_to_df(bars: List[Dict[str, Any]]) -> pd.DataFrame:
    """baostock bars → DataFrame（含策略需要的列 + 停牌/涨跌停辅助列）。"""
    rows = []
    for b in bars:
        d = str(b.get("date", ""))
        close = float(b.get("close", 0) or 0)
        vol = float(b.get("volume", 0) or 0)
        rows.append({
            "trade_date": d,
            "open": float(b.get("open", close) or close),
            "high": float(b.get("high", close) or close),
            "low": float(b.get("low", close) or close),
            "close": close,
            "vol": vol,
            "amount": close * vol,
            # 以下三列仅 baostock 主源有值；新浪兜底缺省为「正常交易、无昨收」
            "preclose": float(b.get("preclose", 0) or 0),
            "tradestatus": str(b.get("tradestatus", "1") or "1"),
            "isST": int(float(b.get("isST", 0) or 0)),
        })
    df = pd.DataFrame(rows)
    df = df.sort_values("trade_date", ascending=True).reset_index(drop=True)
    return df


def _limit_pct(symbol: str, is_st: bool) -> float:
    """按板块返回涨跌幅限制比例（前复权下用 open/preclose 比值判断仍然有效）。"""
    digits = "".join(ch for ch in str(symbol) if ch.isdigit())
    code = digits[-6:] if len(digits) >= 6 else digits
    if code.startswith(("688", "689", "300", "301")):
        pct = 0.20  # 科创板 / 创业板
    elif code.startswith(("4", "8", "920")):
        pct = 0.30  # 北交所
    else:
        pct = 0.10  # 沪深主板
    if is_st and pct == 0.10:
        pct = 0.05  # 主板 ST/*ST
    return pct


def _simulate(
    symbol: str,
    bars: List[Dict[str, Any]],
    inst: Any,
    params: Dict[str, Any],
    days: int,
    initial_capital: float,
    commission: float,
    stamp_tax: float,
    slippage: float,
    min_commission: float,
    transfer_fee: float,
    risk_free_rate: float,
) -> Dict[str, Any]:
    """纯 CPU 段：K 线整理 + 指标预热 + 逐 bar 撮合 + 绩效统计。由 async 入口丢线程池执行。"""
    df = _bars_to_df(bars)
    df = inst.enrich(df, params)

    # 只回测用户指定区间
    cutoff = df["trade_date"].iloc[-days] if len(df) > days else df["trade_date"].iloc[0]
    start_idx = int(df.index[df["trade_date"] >= cutoff][0])
    start_idx = max(1, start_idx)

    cash = float(initial_capital)
    shares = 0.0
    entry_price = 0.0
    entry_fee = 0.0  # 买入端已付费用，卖出时计入净盈亏
    trades: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []
    min_lot_cost = 0.0  # 一手（100 股）所需资金，用于资金不足时的提示
    lot_blocked = False

    def _equity(price: float) -> float:
        return cash + shares * price

    reject_limit_up = 0
    reject_limit_down = 0
    reject_suspended = 0
    last_price = float(df["close"].iloc[max(start_idx - 1, 0)])

    prev_row = None
    for i in range(start_idx, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1] if i > 0 else None

        # 信号基于“上一根已收盘 K 线”，本根开盘成交（避免未来函数）
        context = {"position": shares, "entry_price": entry_price or None, "cash": cash}
        sig = 0
        if prev is not None:
            if shares > 0:
                sig = inst.risk_exit(prev, df.iloc[i - 2] if i >= 2 else None, params, context)
            if sig == 0:
                sig = inst.signal(prev, df.iloc[i - 2] if i >= 2 else None, params, context)

        date = str(row["trade_date"])
        open_px = float(row.get("open") or 0.0)
        close_px = float(row.get("close") or 0.0)
        suspended = str(row.get("tradestatus", "1")) == "0"

        # 停牌或全零行情：不允许成交，净值沿用上一根有效价（避免零价除零/净值跳水）
        if suspended or (open_px <= 0 and close_px <= 0):
            if suspended:
                reject_suspended += 1
            equity_curve.append({"date": date, "equity": round(_equity(last_price), 2)})
            prev_row = row
            continue

        fill = open_px if open_px > 0 else close_px
        # 涨跌停：开盘即封板视为无法成交（前复权下 open 与 preclose 同比例缩放，比值有效）
        preclose = float(row.get("preclose") or 0.0)
        limit_up_open = limit_down_open = False
        if preclose > 0:
            pct = _limit_pct(symbol, bool(row.get("isST")))
            limit_up_open = fill >= preclose * (1 + pct) * (1 - 1e-4)
            limit_down_open = fill <= preclose * (1 - pct) * (1 + 1e-4)

        if sig == 1 and shares == 0 and cash > 0:
            if limit_up_open:
                reject_limit_up += 1
            else:
                frac = float(inst.position_fraction(params, {**context, "signal_row": prev}))
                frac = max(0.0, min(1.0, frac))
                budget = cash * frac
                buy_px = fill * (1 + slippage)  # 买入滑点：成交价上浮
                # 按含费成本反推可买手数，再逐手回退，保证现金不会被最低佣金打穿
                max_shares = int(budget / (buy_px * (1 + commission + transfer_fee)) / 100) * 100
                while max_shares > 0:
                    cost = max_shares * buy_px
                    fee = max(cost * commission, min_commission) + cost * transfer_fee
                    if cost + fee <= cash:
                        break
                    max_shares -= 100
                if max_shares > 0:
                    cost = max_shares * buy_px
                    fee = max(cost * commission, min_commission) + cost * transfer_fee
                    cash -= cost + fee
                    shares = float(max_shares)
                    entry_price = buy_px
                    entry_fee = fee
                    trades.append({"date": date, "action": "BUY", "price": round(buy_px, 2),
                                   "shares": max_shares, "amount": round(cost, 2),
                                   "fee": round(fee, 2), "pnl": 0.0})
                else:
                    lot_blocked = True
                    min_lot_cost = fill * 100 * (1 + commission + transfer_fee) + min_commission

        elif sig == -1 and shares > 0:
            if limit_down_open:
                reject_limit_down += 1
            else:
                sell_px = fill * (1 - slippage)  # 卖出滑点：成交价下浮
                revenue = shares * sell_px
                fee = max(revenue * commission, min_commission) + revenue * (stamp_tax + transfer_fee)
                # 净盈亏 = 卖出净收入 - 买入总成本（含买入费用）
                pnl = revenue - fee - (entry_price * shares + entry_fee)
                cash += revenue - fee
                trades.append({"date": date, "action": "SELL", "price": round(sell_px, 2),
                               "shares": int(shares), "amount": round(revenue, 2),
                               "fee": round(fee, 2), "pnl": round(pnl, 2)})
                shares = 0.0
                entry_price = 0.0
                entry_fee = 0.0

        last_price = close_px if close_px > 0 else last_price
        equity_curve.append({"date": date, "equity": round(_equity(last_price), 2)})
        prev_row = row

    final_equity = _equity(last_price)
    total_return = (final_equity - initial_capital) / initial_capital * 100

    equity_arr = np.array([e["equity"] for e in equity_curve], dtype=float)
    daily_ret = np.diff(equity_arr) / equity_arr[:-1] if len(equity_arr) > 1 else np.array([])
    ann_factor = ANN_FACTOR
    annual = ((1 + total_return / 100) ** (ann_factor / max(len(daily_ret), 1)) - 1) * 100 if len(daily_ret) > 0 else 0.0

    rf_daily = risk_free_rate / ann_factor
    excess = daily_ret - rf_daily
    vol = float(daily_ret.std(ddof=1)) if len(daily_ret) > 1 else 0.0
    sharpe = float(excess.mean() / vol * np.sqrt(ann_factor)) if vol > 0 else 0.0
    downside = excess[excess < 0]
    downside_vol = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = float(excess.mean() / downside_vol * np.sqrt(ann_factor)) if downside_vol > 0 else 0.0
    annual_vol = vol * np.sqrt(ann_factor) * 100

    peak = np.maximum.accumulate(equity_arr) if len(equity_arr) else np.array([initial_capital])
    max_dd = float(((peak - equity_arr) / peak).max() * 100) if len(equity_arr) else 0.0
    calmar = float(annual / max_dd) if max_dd > 0 else 0.0

    sells = [t for t in trades if t["action"] == "SELL"]
    win_rate = sum(1 for t in sells if t["pnl"] > 0) / len(sells) * 100 if sells else 0.0
    gains = sum(t["pnl"] for t in sells if t["pnl"] > 0)
    losses = sum(t["pnl"] for t in sells if t["pnl"] < 0)
    profit_factor = float(gains / abs(losses)) if losses < 0 else 0.0

    warning = None
    if lot_blocked and min_lot_cost > 0:
        warning = (
            f"初始资金 {initial_capital:,.0f} 元不足以买入一手（100 股约需 "
            f"{min_lot_cost:,.0f} 元），本次未产生交易。请提高初始资金或换低价标的。"
        )

    return {
        "bars_count": len(equity_curve),
        "warning": warning,
        "cost_model": {
            "commission": commission,
            "min_commission": min_commission,
            "stamp_tax": stamp_tax,
            "transfer_fee": transfer_fee,
            "slippage": slippage,
            "fill_rule": "信号基于上一根已收盘K线，本根开盘价±滑点成交",
            "tradability": "涨停不买、跌停不卖、停牌跳过（按板块/ST 涨跌幅限制）",
            "adjustflag": "2=前复权",
            "lot_size": 100,
        },
        "rejections": {
            "limit_up": reject_limit_up,
            "limit_down": reject_limit_down,
            "suspended": reject_suspended,
        },
        "metrics": {
            "total_return_pct": round(total_return, 2),
            "annual_return_pct": round(float(annual), 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "calmar_ratio": round(calmar, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "annual_volatility_pct": round(annual_vol, 2),
            "win_rate_pct": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "risk_free_rate": risk_free_rate,
            "initial_capital": round(initial_capital, 2),
            "final_equity": round(final_equity, 2),
            "trades_count": len(trades),
        },
        "metrics_meta": {
            "sharpe_ratio": "mean(r - rf/252) / std(r, ddof=1) × √252",
            "sortino_ratio": "mean(r - rf/252) / std(下行超额收益, ddof=1) × √252",
            "calmar_ratio": "年化收益 / 最大回撤",
            "annual_volatility_pct": "std(日收益, ddof=1) × √252 × 100",
            "profit_factor": "卖出盈利合计 / |卖出亏损合计|（单笔净盈亏含双边费用）",
            "win_rate_pct": "盈利卖出笔数 / 卖出笔数",
            "risk_free_rate": "年化无风险利率，与基准页口径一致（当前取 0）",
        },
        "trades": trades,
        "equity_curve": equity_curve,
    }


async def run_strategy_backtest(
    symbol: str,
    strategy: str = "ma_cross",
    params: Optional[Mapping[str, Any]] = None,
    initial_capital: float = 100_000.0,
    days: int = 120,
    frequency: str = "d",
    commission: float = DEFAULT_COMMISSION,
    stamp_tax: float = DEFAULT_STAMP_TAX,
    slippage: float = DEFAULT_SLIPPAGE,
    min_commission: float = DEFAULT_MIN_COMMISSION,
    transfer_fee: float = DEFAULT_TRANSFER_FEE,
    risk_free_rate: float = 0.0,
) -> Dict[str, Any]:
    """用内置策略跑一次单标的回测（baostock 前复权日线，次日开盘成交）。

    取数是真异步（网关内部走线程池），撮合与指标是纯 CPU；
    后者统一丢到线程池，避免 days 较大时占住事件循环。
    """
    params = _normalize_params(params or {})
    cls = get_strategy_class(strategy)
    if getattr(cls, "requires_multi_asset", False):
        raise ValueError(f"策略 {strategy} 需要多标的截面回测，当前仅支持单标的")
    inst = cls()

    # 多取 90 天做指标预热
    bars = await fetch_history_bars(symbol, days=days + 90, frequency=frequency)
    if len(bars) < 60:
        raise ValueError(f"数据点太少 ({len(bars)})，无法回测")

    result = await asyncio.to_thread(
        _simulate,
        symbol, bars, inst, params, days, float(initial_capital),
        commission, stamp_tax, slippage, min_commission, transfer_fee, risk_free_rate,
    )

    return {
        "symbol": symbol,
        "strategy": strategy,
        "strategy_name": STRATEGY_META.get(strategy, {}).get("name", strategy),
        "params": params,
        "data_source": "baostock(前复权)" if bars[0].get("source") == "baostock" else "sina(不复权兜底)",
        **result,
    }


def run_strategy_backtest_sync(*args, **kwargs):
    """同步包装，供非 async 环境使用。"""
    return asyncio.run(run_strategy_backtest(*args, **kwargs))
