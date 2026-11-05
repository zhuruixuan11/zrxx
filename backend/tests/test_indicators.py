"""技术指标计算单测（纯函数，不触网）。"""
import pytest

from app.agent.indicators import compute_indicators


def _bars(closes, volumes=None):
    volumes = volumes or [100000] * len(closes)
    return [
        {"date": f"d{i}", "close": c, "volume": v}
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def test_insufficient_bars_returns_empty():
    """少于 20 根日线时无法计算，返回空 dict 而不是抛异常。"""
    assert compute_indicators(_bars([10.0] * 19)) == {}


def test_ma_and_returns_on_steady_uptrend():
    closes = [10.0 + i * 0.1 for i in range(60)]
    out = compute_indicators(_bars(closes))

    assert out["bars"] == 60
    assert out["last_close"] == pytest.approx(15.9)
    assert out["ma5"] == pytest.approx(15.7)
    assert out["ma20"] == pytest.approx(14.95)  # 最后 20 根 14.0~15.9 的均值
    assert out["ma60"] == pytest.approx(12.95)
    # 单调上涨 → 无下跌区间 → RSI 顶格 100
    assert out["rsi14"] == 100.0
    assert out["ret5"] == pytest.approx((15.9 / 15.4 - 1) * 100, abs=0.01)
    assert out["pos60"] == 100.0


def test_only_short_history_omits_ma60():
    closes = [10.0 + i * 0.1 for i in range(30)]
    out = compute_indicators(_bars(closes))
    # 数据不足时字段直接省略（不是 None），避免下游误用
    assert "ma60" not in out
    assert out["ma20"] is not None
    assert out["high60"] == pytest.approx(12.9)
