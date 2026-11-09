"""基准跟随 / 跟踪误差分析 API。"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from ..models.user import User
from ..security import get_current_user
from ..services.benchmark import BenchmarkError, analyze_benchmark, benchmark_options

router = APIRouter(prefix="/api/benchmark", tags=["benchmark"])


class AnalyzePayload(BaseModel):
    """分析对象（策略回测 / 已保存回测 / 我的组合）对比一条基准。"""
    source: Literal["strategy", "run", "portfolio"] = Field(
        default="strategy", description="分析对象：strategy 现跑回测 / run 已保存回测 / portfolio 我的组合"
    )
    benchmark: Literal["hs300", "zz500", "custom"] = Field(
        default="hs300", description="基准：hs300 沪深300 / zz500 中证500 / custom 自选组合"
    )
    # source=strategy
    symbol: Optional[str] = Field(default=None, description="股票代码，如 600519")
    strategy: str = Field(default="ma_cross", description="策略 id，见 /api/backtest/strategies")
    params: Dict[str, Any] = Field(default_factory=dict)
    initial_capital: float = Field(default=200_000, ge=1000)
    days: int = Field(default=120, ge=20, le=500)
    # source=run
    run_id: Optional[str] = Field(default=None, description="已保存回测记录 id")


@router.get("/options")
async def options(user: User = Depends(get_current_user)):
    """基准列表 + 口径说明（前端下拉与页脚共用）。"""
    return {"code": 0, "msg": "ok", "data": await benchmark_options(user.id)}


@router.post("/analyze")
async def analyze(payload: AnalyzePayload, user: User = Depends(get_current_user)):
    """执行一次基准跟随分析并返回净值序列、指标与行业暴露。"""
    try:
        result = await analyze_benchmark(user.id, payload.model_dump())
    except BenchmarkError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.warning(f"[benchmark] analyze failed: {e}")
        raise HTTPException(502, "基准分析执行失败，请稍后重试或调整参数")
    return {"code": 0, "msg": "ok", "data": result}
