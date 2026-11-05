"""A 股代码标准化单测（不触网）。"""
import pytest

from app.agent.tools import normalize_a_share


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("600519", "sh.600519"),
        ("688981", "sh.688981"),
        ("000001", "sz.000001"),
        ("300750", "sz.300750"),
        ("920819", "bj.920819"),
        ("830799", "bj.830799"),
        ("600519.SH", "sh.600519"),
        ("sh600519", "sh.600519"),
    ],
)
def test_normalize_a_share(raw, expected):
    assert normalize_a_share(raw) == expected


def test_us_ticker_is_not_a_share():
    assert normalize_a_share("AAPL") is None
