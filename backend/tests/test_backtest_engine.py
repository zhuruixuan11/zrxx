"""回测引擎回归：成交时点、成本、可交易性、指标口径。

全部用构造 K 线 + 打桩策略，不触网、不依赖 baostock。
信号策略只按「行日期」出信号，因此可以精确推出：哪一根出信号、哪一根成交、成交价是多少。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.backtest_engine.base import BacktestStrategy
from app.backtest_engine.runner import _limit_pct, _simulate


class _Scripted(BacktestStrategy):
    """按日期脚本出信号的测试策略：buy_dates 里的行 → 买；sell_dates 里的行 → 卖。"""

    name = "scripted"

    def __init__(self, buy_dates=(), sell_dates=()):
        self.buy_dates = set(buy_dates)
        self.sell_dates = set(sell_dates)
        self.seen: list[tuple[str, float]] = []  # (信号行日期, 当时持仓)

    def enrich(self, df, params):
        return df

    def signal(self, row, prev_row, params, context=None):
        self.seen.append((row["trade_date"], float((context or {}).get("position") or 0)))
        if row["trade_date"] in self.buy_dates and not context.get("position"):
            return 1
        if row["trade_date"] in self.sell_dates and context.get("position"):
            return -1
        return 0


def _bar(date, open_, close, preclose=None, tradestatus="1", is_st=0):
    return {
        "date": date,
        "open": open_,
        "high": max(open_, close),
        "low": min(open_, close),
        "close": close,
        "volume": 1_000_000,
        "preclose": close if preclose is None else preclose,
        "tradestatus": tradestatus,
        "isST": is_st,
    }


def _run(bars, buy=(), sell=(), capital=100_000.0, **kw):
    inst = _Scripted(buy_dates=buy, sell_dates=sell)
    args = dict(
        commission=0.0003, stamp_tax=0.001, slippage=0.0005,
        min_commission=5.0, transfer_fee=0.00001, risk_free_rate=0.0,
    )
    args.update(kw)
    result = _simulate(
        "600519", bars, inst, {}, 100, float(capital),
        args["commission"], args["stamp_tax"], args["slippage"],
        args["min_commission"], args["transfer_fee"], args["risk_free_rate"],
    )
    return result, inst


# ---------------- 成交时点：无未来函数 ----------------

def test_signal_on_previous_bar_fills_at_next_bar_open():
    bars = [
        _bar("2026-01-05", 10.0, 10.0),
        _bar("2026-01-06", 10.0, 10.5),   # 信号行
        _bar("2026-01-07", 11.0, 11.0),   # 应在这里成交
        _bar("2026-01-08", 12.0, 12.0),   # 卖出信号行
        _bar("2026-01-09", 12.5, 12.5),   # 应在这里卖出
    ]
    result, inst = _run(bars, buy=["2026-01-06"], sell=["2026-01-08"])
    trades = result["trades"]

    assert [t["action"] for t in trades] == ["BUY", "SELL"]
    # 信号行是 01-06，成交落在下一根 01-07 —— 不能当天成交
    assert trades[0]["date"] == "2026-01-07"
    assert trades[1]["date"] == "2026-01-09"
    # 成交价 = 当日开盘 ± 滑点，不是收盘价，也不是信号日价格
    assert trades[0]["price"] == pytest.approx(round(11.0 * 1.0005, 2))
    assert trades[1]["price"] == pytest.approx(round(12.5 * 0.9995, 2))

    # 策略在每根 bar 只看到「上一根」的数据：卖出信号行必须早于卖出成交日
    signal_dates = [d for d, _ in inst.seen]
    assert signal_dates == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    assert max(signal_dates) < trades[-1]["date"]


def test_first_bar_only_serves_as_prev_never_fills_itself():
    # 首根没有「上一根已收盘 K 线」，它只能作为 prev 被读取；
    # 用它出的信号必须等到第二根开盘才成交，不会在首根当天成交。
    bars = [_bar("2026-01-05", 10.0, 10.0), _bar("2026-01-06", 10.0, 10.0)]
    result, inst = _run(bars, buy=["2026-01-05"])
    assert [t["date"] for t in result["trades"]] == ["2026-01-06"]
    assert inst.seen[0][0] == "2026-01-05"


# ---------------- 成本：佣金/印花税/过户费/滑点 ----------------

def test_cost_model_numbers_are_exact():
    bars = [
        _bar("2026-02-02", 10.0, 10.0),
        _bar("2026-02-03", 10.0, 10.0),   # 买信号
        _bar("2026-02-04", 11.0, 11.0),  # 买入成交
        _bar("2026-02-05", 11.0, 11.0),  # 卖信号
        _bar("2026-02-06", 12.5, 12.5),  # 卖出成交
    ]
    result, _ = _run(bars, buy=["2026-02-03"], sell=["2026-02-05"])
    buy, sell = result["trades"]

    buy_px = 11.0 * 1.0005
    shares = buy["shares"]
    cost = shares * buy_px
    # 买入：佣金（万三，最低 5）+ 过户费（双边）
    assert buy["fee"] == pytest.approx(max(cost * 0.0003, 5.0) + cost * 0.00001, abs=0.01)
    # 卖出：佣金（万三，最低 5）+ 印花税（千一，仅卖出）+ 过户费
    revenue = shares * 12.5 * 0.9995
    assert sell["fee"] == pytest.approx(
        max(revenue * 0.0003, 5.0) + revenue * (0.001 + 0.00001), abs=0.01
    )
    # 印花税只在卖出出现：卖出费率明显高于买入（同一笔规模下）
    assert sell["fee"] > buy["fee"] * 3

    # 整手：必须是 100 的整数倍
    assert shares % 100 == 0
    assert shares == 9000
    # 现金不能为负（最低佣金不会把账户打穿）
    assert result["metrics"]["final_equity"] > 0


def test_min_commission_is_five_yuan_when_turnover_is_small():
    # 本金 1 万、股价 5 元：turnover 9500 元 × 万三 = 2.85 元 < 5 元，佣金必须抬到 5 元
    bars = [
        _bar("2026-03-02", 5.0, 5.0),
        _bar("2026-03-03", 5.0, 5.0),
        _bar("2026-03-04", 5.0, 5.0),
    ]
    result, _ = _run(bars, buy=["2026-03-02"], capital=10_000.0)
    buy = result["trades"][0]
    cost = buy["shares"] * 5.0 * 1.0005
    assert buy["shares"] == 1900
    assert cost * 0.0003 < 5.0                      # 按比例算不足 5 元
    assert buy["fee"] == pytest.approx(5.0 + cost * 0.00001, abs=0.01)
    assert buy["fee"] > 5.0                          # 5 元底价 + 过户费


def test_unaffordable_lot_reports_warning_instead_of_trading():
    bars = [_bar("2026-03-02", 5.0, 5.0), _bar("2026-03-03", 5.0, 5.0)]
    result, _ = _run(bars, buy=["2026-03-02"], capital=100.0)
    assert result["trades"] == []
    assert result["warning"] and "不足以买入一手" in result["warning"]


# ---------------- 可交易性：涨停/跌停/停牌/零价 ----------------

def test_limit_up_blocks_buy_on_main_board():
    bars = [
        _bar("2026-04-01", 10.0, 10.0, preclose=9.9),
        _bar("2026-04-02", 11.0, 11.0, preclose=10.0),  # +10% 一字涨停
    ]
    result, _ = _run(bars, buy=["2026-04-01"])
    assert result["trades"] == []
    assert result["rejections"]["limit_up"] == 1


def test_same_ten_percent_move_is_tradable_on_chinext():
    # 300 开头涨跌幅 20%，+10% 不算涨停，必须允许成交
    bars = [
        _bar("2026-04-01", 10.0, 10.0, preclose=9.9),
        _bar("2026-04-02", 11.0, 11.0, preclose=10.0),
    ]
    inst = _Scripted(buy_dates=["2026-04-01"])
    result = _simulate(
        "300750", bars, inst, {}, 100, 100_000.0,
        0.0003, 0.001, 0.0005, 5.0, 0.00001, 0.0,
    )
    assert result["rejections"]["limit_up"] == 0
    assert len(result["trades"]) == 1


def test_main_board_st_uses_five_percent_limit():
    bars = [
        _bar("2026-04-01", 10.0, 10.0, preclose=9.9),
        _bar("2026-04-02", 10.5, 10.5, preclose=10.0, is_st=1),  # +5% 即 ST 涨停
    ]
    result, _ = _run(bars, buy=["2026-04-01"])
    assert result["trades"] == []
    assert result["rejections"]["limit_up"] == 1


def test_limit_down_blocks_sell():
    bars = [
        _bar("2026-05-04", 10.0, 10.0, preclose=9.9),
        _bar("2026-05-05", 10.0, 10.0, preclose=10.0),   # 买入
        _bar("2026-05-06", 10.0, 10.0, preclose=10.0),   # 卖信号
        _bar("2026-05-07", 9.0, 9.0, preclose=10.0),     # -10% 跌停，不能卖
        _bar("2026-05-08", 9.5, 9.5, preclose=9.0),
    ]
    result, _ = _run(bars, buy=["2026-05-04"], sell=["2026-05-06"])
    assert [t["action"] for t in result["trades"]] == ["BUY"]
    assert result["rejections"]["limit_down"] == 1


def test_suspended_bar_is_skipped_and_equity_is_flat():
    bars = [
        _bar("2026-06-01", 10.0, 10.0),
        _bar("2026-06-02", 10.0, 10.0, tradestatus="0"),  # 停牌
        _bar("2026-06-03", 10.0, 10.0),
    ]
    result, _ = _run(bars, buy=["2026-06-01"])
    assert result["trades"] == []
    assert result["rejections"]["suspended"] == 1
    assert result["equity_curve"][0]["equity"] == 100_000.0
    # 停牌当日净值沿用上一根有效价，不能跳水
    assert result["equity_curve"][1]["equity"] == 100_000.0


def test_all_zero_bar_does_not_crash_or_double_count_suspension():
    bars = [
        _bar("2026-06-01", 10.0, 10.0),
        _bar("2026-06-02", 0.0, 0.0, preclose=10.0),  # 全零行情但未标停牌
        _bar("2026-06-03", 10.2, 10.2),
    ]
    result, _ = _run(bars, buy=["2026-06-01"])
    assert result["rejections"]["suspended"] == 0
    assert result["equity_curve"][1]["equity"] == 100_000.0
    assert result["metrics"]["final_equity"] == 100_000.0


def test_limit_pct_by_board_and_st():
    assert _limit_pct("sh.600519", False) == 0.10
    assert _limit_pct("sz.300750", False) == 0.20
    assert _limit_pct("sh.688981", False) == 0.20
    assert _limit_pct("bj.430047", False) == 0.30
    assert _limit_pct("sh.600519", True) == 0.05
    # ST 只压主板，创业板不降到 5%
    assert _limit_pct("sz.300750", True) == 0.20


# ---------------- 指标口径 ----------------

def _curve_metrics(result, initial_capital=100_000.0):
    """从返回的净值曲线独立复算指标，用于核对引擎口径（不复用引擎代码）。

    注意：曲线首点是「第一根 bar 成交后」的净值，已含买入费用，所以累计收益要
    以 initial_capital 为基准，而不是以曲线首点为基准。
    日收益序列只包含曲线相邻点之差（引擎就是这么算的），首日费用不计入日收益。
    """
    eq = np.array([e["equity"] for e in result["equity_curve"]], dtype=float)
    ret = np.diff(eq) / eq[:-1]
    rf_d = 0.0
    excess = ret - rf_d
    vol = ret.std(ddof=1)
    peak = np.maximum.accumulate(eq)
    max_dd = ((peak - eq) / peak).max() * 100
    total = (eq[-1] - initial_capital) / initial_capital
    annual = ((1 + total) ** (252 / max(len(ret), 1)) - 1) * 100
    downside = excess[excess < 0]
    return {
        "total": total * 100,
        "annual": annual,
        "vol": vol * np.sqrt(252) * 100,
        "sharpe": excess.mean() / vol * np.sqrt(252),
        "sortino": excess.mean() / downside.std(ddof=1) * np.sqrt(252),
        "max_dd": max_dd,
        "calmar": annual / max_dd,
        "n": len(ret),
    }


def _choppy_bars():
    closes = [10.0, 10.4, 10.1, 10.8, 10.2, 11.0, 10.5, 11.4, 10.9, 11.8, 11.3, 12.0]
    bars = []
    for i, c in enumerate(closes):
        day = f"2026-07-{i + 1:02d}"
        bars.append(_bar(day, closes[i - 1] if i else c, c, preclose=closes[i - 1] if i else c))
    return bars


def test_metrics_match_independent_recomputation():
    bars = _choppy_bars()
    # 第二根出买信号（在第一根上），第四根出卖信号
    result, _ = _run(bars, buy=["2026-07-01"], sell=["2026-07-04"], capital=100_000.0)
    m = result["metrics"]
    exp = _curve_metrics(result)

    assert m["total_return_pct"] == pytest.approx(exp["total"], abs=0.01)
    assert m["annual_return_pct"] == pytest.approx(exp["annual"], abs=0.01)   # 几何年化
    assert m["annual_volatility_pct"] == pytest.approx(exp["vol"], abs=0.01)
    assert m["sharpe_ratio"] == pytest.approx(exp["sharpe"], abs=0.01)
    assert m["sortino_ratio"] == pytest.approx(exp["sortino"], abs=0.01)
    assert m["max_drawdown_pct"] == pytest.approx(exp["max_dd"], abs=0.01)
    assert m["calmar_ratio"] == pytest.approx(exp["calmar"], abs=0.01)
    # 年化收益用几何、不是算术平均
    arithmetic = exp["total"] / exp["n"] * 252
    assert m["annual_return_pct"] != pytest.approx(arithmetic, abs=0.01)


def test_std_uses_sample_ddof_one():
    bars = _choppy_bars()
    result, _ = _run(bars, buy=["2026-07-01"], sell=["2026-07-04"])
    eq = np.array([e["equity"] for e in result["equity_curve"]], dtype=float)
    ret = np.diff(eq) / eq[:-1]
    ddof1 = ret.std(ddof=1) * np.sqrt(252) * 100
    ddof0 = ret.std(ddof=0) * np.sqrt(252) * 100
    assert ddof1 != pytest.approx(ddof0, abs=1e-6)  # 确保这个样本能区分
    assert result["metrics"]["annual_volatility_pct"] == pytest.approx(ddof1, abs=0.01)


def test_sharpe_subtracts_risk_free_rate():
    bars = _choppy_bars()
    low, _ = _run(bars, buy=["2026-07-01"], sell=["2026-07-04"], risk_free_rate=0.0)
    high, _ = _run(bars, buy=["2026-07-01"], sell=["2026-07-04"], risk_free_rate=0.10)
    assert high["metrics"]["sharpe_ratio"] < low["metrics"]["sharpe_ratio"]
    # 差额应等于 rf/252 带来的均值下调: (mean - rf/252)/mean 比例
    eq = np.array([e["equity"] for e in low["equity_curve"]], dtype=float)
    ret = np.diff(eq) / eq[:-1]
    vol = ret.std(ddof=1)
    expected = (ret.mean() - 0.10 / 252) / vol * np.sqrt(252)
    assert high["metrics"]["sharpe_ratio"] == pytest.approx(expected, abs=0.01)


def test_profit_factor_only_counts_sells():
    # 两次往返：第一次盈利、第二次亏损；盈亏比 = 盈利合计 / |亏损合计|
    # 显式给开盘价，成交价 = 次日开盘 × (1 ± 滑点)，涨跌方向不含糊
    opens = [10.0, 10.0, 10.5, 11.0, 11.2, 10.8, 10.6, 10.2, 9.4, 9.6]
    bars = [
        _bar(f"2026-08-{i + 1:02d}", opens[i], opens[i], preclose=opens[i - 1] if i else opens[0])
        for i in range(len(opens))
    ]
    result, _ = _run(
        bars,
        buy=["2026-08-01", "2026-08-06"],   # 成交在 08-02 / 08-07
        sell=["2026-08-03", "2026-08-08"],  # 成交在 08-04 / 08-09
    )
    sells = [t for t in result["trades"] if t["action"] == "SELL"]
    assert len(sells) == 2
    assert sells[0]["pnl"] > 0   # 买 10.5 卖 11.0
    assert sells[1]["pnl"] < 0   # 买 10.6 卖 9.4
    gains = sum(t["pnl"] for t in sells if t["pnl"] > 0)
    losses = sum(t["pnl"] for t in sells if t["pnl"] < 0)
    assert result["metrics"]["profit_factor"] == pytest.approx(gains / abs(losses), abs=0.02)
    assert result["metrics"]["win_rate_pct"] == pytest.approx(50.0)


def test_flat_curve_has_zero_risk_metrics_without_dividing_by_zero():
    bars = [_bar(f"2026-09-{i + 1:02d}", 10.0, 10.0, preclose=10.0) for i in range(5)]
    result, _ = _run(bars)
    m = result["metrics"]
    assert m["total_return_pct"] == 0.0
    assert m["annual_volatility_pct"] == 0.0
    assert m["sharpe_ratio"] == 0.0
    assert m["sortino_ratio"] == 0.0
    assert m["calmar_ratio"] == 0.0
