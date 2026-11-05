"""衡策回测引擎（内置策略库，数据源为 baostock）。"""
from . import strategies  # noqa: F401  # 触发策略注册
from .registry import get_strategy_class, list_registered_strategies, register_strategy  # noqa: F401
from .runner import list_strategies, run_strategy_backtest  # noqa: F401

__all__ = [
    "get_strategy_class",
    "list_registered_strategies",
    "register_strategy",
    "list_strategies",
    "run_strategy_backtest",
]
