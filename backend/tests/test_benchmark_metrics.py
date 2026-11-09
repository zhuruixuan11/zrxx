"""基准跟随指标单测：口径（几何年化、ddof=1、超额=两个几何累计之差）不能悄悄漂移。"""
import asyncio
from datetime import date, timedelta

import numpy as np
import pytest

from app.services import benchmark as bm
from app.services.benchmark import (
    BenchmarkError,
    _align_series,
    _bare_symbol,
    _industry_section,
    _max_drawdown,
    _metrics,
)


def _bars(closes, start=date(2026, 1, 1)):
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "close": float(c)}
        for i, c in enumerate(closes)
    ]


def test_metrics_identical_series_has_zero_excess():
    nav = np.array([1.0, 1.05, 0.98, 1.12, 1.2])
    m = _metrics(nav, nav.copy())
    assert m["excess"]["cumulative_pct"] == 0.0
    assert m["excess"]["annualized_pct"] == 0.0
    assert m["excess"]["tracking_error_pct"] == 0.0
    assert m["excess"]["beta"] == 1.0
    assert m["excess"]["alpha_pct"] == 0.0


def test_metrics_are_geometric_not_arithmetic():
    # +10% 再 -10%：算术均值 0%，几何累计 = -1%
    m = _metrics(np.array([1.0, 1.10, 0.99]), np.array([1.0, 1.0, 1.0]))
    assert m["strategy"]["cumulative_return_pct"] == -1.0
    assert m["strategy"]["annualized_return_pct"] < 0
    assert m["trading_days"] == 2


def test_metrics_alpha_is_annualized_excess_minus_beta_term():
    nav_p = np.array([1.0, 1.05, 1.02, 1.10, 1.08, 1.15])
    nav_b = np.array([1.0, 1.02, 1.01, 1.05, 1.03, 1.07])
    m = _metrics(nav_p, nav_b)
    r_p = np.diff(nav_p) / nav_p[:-1]
    r_b = np.diff(nav_b) / nav_b[:-1]
    beta = float(np.cov(r_p, r_b, ddof=1)[0][1] / np.var(r_b, ddof=1))
    assert m["excess"]["beta"] == pytest.approx(beta, abs=1e-4)

    cum_p = nav_p[-1] / nav_p[0] - 1
    cum_b = nav_b[-1] / nav_b[0] - 1
    assert m["excess"]["cumulative_pct"] == pytest.approx((cum_p - cum_b) * 100, abs=0.01)
    # alpha = 策略年化 - beta × 基准年化（rf=0）
    expected_alpha = (m["strategy"]["annualized_return_pct"]
                      - beta * m["benchmark"]["annualized_return_pct"])
    assert m["excess"]["alpha_pct"] == pytest.approx(expected_alpha, abs=0.02)


def test_metrics_tracking_error_uses_sample_std():
    nav_p = np.array([1.0, 1.05, 1.02, 1.10, 1.08, 1.15])
    nav_b = np.array([1.0, 1.02, 1.01, 1.05, 1.03, 1.07])
    r_p = np.diff(nav_p) / nav_p[:-1]
    r_b = np.diff(nav_b) / nav_b[:-1]
    expected = float(np.std(r_p - r_b, ddof=1) * np.sqrt(252) * 100)
    assert _metrics(nav_p, nav_b)["excess"]["tracking_error_pct"] == pytest.approx(expected, abs=0.01)


def test_metrics_rejects_too_short_series():
    with pytest.raises(BenchmarkError):
        _metrics(np.array([1.0]), np.array([1.0]))


def test_max_drawdown_uses_running_peak():
    assert _max_drawdown(np.array([1.0, 1.2, 0.9])) == pytest.approx(25.0)
    assert _max_drawdown(np.array([])) == 0.0


def test_align_series_intersects_dates_and_normalizes_both_sides():
    strat = _bars([10 + i for i in range(30)])
    bench = _bars([100 + 2 * i for i in range(30)])[5:]  # 少前 5 天
    dates, p, b = _align_series(strat, bench)
    assert len(dates) == 25
    assert dates == sorted(dates)
    assert p[0] == pytest.approx(1.0) and b[0] == pytest.approx(1.0)
    # 交集从第 6 天开始：策略 15→16，基准 110→112，各自只保留相对涨跌
    assert p[1] / p[0] == pytest.approx(16 / 15)
    assert b[1] / b[0] == pytest.approx(112 / 110)


def test_align_series_rejects_short_overlap():
    with pytest.raises(BenchmarkError):
        _align_series(_bars([1, 2, 3]), _bars([1, 2, 3]))


def test_bare_symbol_accepts_all_common_formats():
    assert _bare_symbol("sh.600519") == "600519"
    assert _bare_symbol("600519.SH") == "600519"
    assert _bare_symbol("300750") == "300750"


# ---------------- 行业暴露 / 偏离 ----------------

def _holdings(*pairs):
    return [{"symbol": sym, "weight": w} for sym, w in pairs]


def test_industry_deviation_sums_to_zero(monkeypatch):
    mapping = {"600519": "食品饮料", "601398": "银行", "000725": "电子"}
    monkeypatch.setattr(bm, "_industry_of", _fake_industry(mapping))

    strat = _holdings(("600519", 0.5), ("601398", 0.3), ("000725", 0.2))
    bench = _holdings(("600519", 0.4), ("601398", 0.4), ("000725", 0.2))
    section = asyncio.run(_industry_section(strat, bench, "hs300"))

    assert section["available"] is True
    assert section["benchmark_available"] is True
    items = {i["industry"]: i for i in section["items"]}

    assert items["食品饮料"]["weight_pct"] == pytest.approx(50.0)
    assert items["食品饮料"]["benchmark_weight_pct"] == pytest.approx(40.0)
    assert items["食品饮料"]["deviation_pct"] == pytest.approx(10.0)
    assert items["银行"]["deviation_pct"] == pytest.approx(-10.0)
    assert items["电子"]["deviation_pct"] == pytest.approx(0.0)

    # 组合权重合计 100%，行业偏离合计 0%（偏离按 2 位小数取整，留 0.02 容差）
    assert sum(i["weight_pct"] for i in section["items"]) == pytest.approx(100.0, abs=0.02)
    assert sum(i["benchmark_weight_pct"] for i in section["items"]) == pytest.approx(100.0, abs=0.02)
    assert sum(i["deviation_pct"] for i in section["items"]) == pytest.approx(0.0, abs=0.02)


def test_missing_industry_falls_into_unknown_bucket_and_still_sums_to_hundred(monkeypatch):
    mapping = {"600519": "食品饮料", "601398": ""}  # 第二只取不到行业
    monkeypatch.setattr(bm, "_industry_of", _fake_industry(mapping))

    strat = _holdings(("600519", 0.75), ("601398", 0.25))
    section = asyncio.run(_industry_section(strat, None, "hs300"))

    items = {i["industry"]: i for i in section["items"]}
    assert items["未知"]["weight_pct"] == pytest.approx(25.0)
    assert sum(i["weight_pct"] for i in section["items"]) == pytest.approx(100.0, abs=0.02)
    assert "1 只持仓行业分类缺失" in section["note"]


def test_index_benchmark_has_exposure_but_no_deviation(monkeypatch):
    monkeypatch.setattr(bm, "_industry_of", _fake_industry({"600519": "食品饮料"}))

    section = asyncio.run(_industry_section(_holdings(("600519", 1.0)), None, "hs300"))

    assert section["available"] is True
    assert section["benchmark_available"] is False
    assert section["items"][0]["deviation_pct"] is None
    assert section["items"][0]["benchmark_weight_pct"] is None
    assert "不做估算" in section["note"]


def test_no_holdings_returns_unavailable_section():
    section = asyncio.run(_industry_section(None, None, "hs300"))
    assert section["available"] is False
    assert section["items"] == []


def _fake_industry(mapping):
    """把 _industry_of（会联网查 baostock/东财）替换成查字典。"""
    async def _of(symbol):
        return mapping.get(symbol, "")
    return _of

