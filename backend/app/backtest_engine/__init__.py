"""衡策回测引擎（内置策略库，数据源为 baostock）。

本包（BacktestStrategy 基类、注册表、回测运行器及全部 23 个内置策略）
移植自开源项目 fin-agent（https://github.com/YUHAI0/fin-agent），
遵循其 MIT License 原始许可声明，版权归原作者所有。
"""
# The MIT License (MIT)
# Copyright (c) 2024 YUHAI0 (fin-agent backtest engine)
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
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
