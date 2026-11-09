"""策略回测 API —— 23 套内置策略 + baostock 真实历史数据 + 回测历史。"""
import json
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..backtest_engine import list_strategies, run_strategy_backtest
from ..backtest_engine.runner import STRATEGY_META
from ..database import async_session
from ..models.backtest_run import BacktestRun
from ..models.user import User
from ..security import get_current_user

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class SaveRunPayload(BaseModel):
    """保存一条回测记录（前端直接回传本次结果快照，无需重跑）。"""
    symbol: str = Field(..., min_length=6, max_length=10)
    strategy: str
    initial_capital: float = Field(default=200_000, ge=1000)
    days: int = Field(default=120, ge=1, le=5000)
    params: Dict[str, Any] = {}
    metrics: Dict[str, Any]
    equity_curve: List[Dict[str, Any]] = []


def _serialize(run: BacktestRun, with_curve: bool = False) -> Dict[str, Any]:
    data = {
        "id": run.id,
        "symbol": run.symbol,
        "strategy": run.strategy,
        "strategy_name": run.strategy_name,
        "params": json.loads(run.params_json or "{}"),
        "initial_capital": run.initial_capital,
        "days": run.days,
        "metrics": json.loads(run.metrics_json or "{}"),
        "created_at": run.created_at.strftime("%Y-%m-%d %H:%M:%S") if run.created_at else None,
    }
    if with_curve:
        data["equity_curve"] = json.loads(run.equity_curve_json or "[]")
    return data


@router.get("/strategies")
def strategies():
    """列出所有可用策略 + 参数说明。"""
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "strategies": list_strategies(),
            "groups": sorted({m.get("group", "") for m in STRATEGY_META.values() if m.get("group")}),
            "note": (
                "数据源：baostock 前复权日线（adjustflag=2）。"
                "成本：佣金万三（单笔最低 5 元）+ 卖出印花税千一 + 过户费 0.001%（双边）+ 滑点万五。"
                "成交：信号基于上一根已收盘 K 线，次日开盘价±滑点成交。"
            ),
        },
    }


@router.get("/run")
async def run(
    symbol: str = Query(..., description="股票代码，如 600519 / 000001"),
    strategy: str = Query(default="ma_cross", description="策略 id，见 /strategies"),
    initial_capital: float = Query(default=200_000, ge=1000, description="初始资金（默认 20 万，避免高价股买不起一手）"),
    days: int = Query(default=120, ge=60, le=500, description="回测天数"),
    params: str = Query(default="{}", description="策略参数 JSON，如 {\"short_window\":5,\"long_window\":20}"),
):
    """运行一次策略回测。"""
    try:
        p = json.loads(params) if params else {}
        if not isinstance(p, dict):
            raise ValueError("params 必须是 JSON 对象")
    except json.JSONDecodeError:
        raise HTTPException(400, "params 不是合法 JSON")

    try:
        result = await run_strategy_backtest(
            symbol=symbol,
            strategy=strategy,
            params=p,
            initial_capital=initial_capital,
            days=days,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.warning(f"[backtest] run failed: {e}")
        raise HTTPException(502, "回测执行失败，请稍后重试或调整参数")

    return {"code": 0, "msg": "ok", "data": result}


@router.post("/save")
async def save_run(payload: SaveRunPayload, user: User = Depends(get_current_user)):
    """把本次回测结果存入历史（指标 + 权益曲线）。"""
    meta = STRATEGY_META.get(payload.strategy, {})
    run = BacktestRun(
        user_id=user.id,
        symbol=payload.symbol.strip().upper(),
        strategy=payload.strategy,
        strategy_name=meta.get("name", payload.strategy),
        params_json=json.dumps(payload.params, ensure_ascii=False),
        initial_capital=payload.initial_capital,
        days=payload.days,
        metrics_json=json.dumps(payload.metrics, ensure_ascii=False),
        equity_curve_json=json.dumps(payload.equity_curve, ensure_ascii=False),
    )
    async with async_session() as db:
        db.add(run)
        await db.commit()
        await db.refresh(run)
    return {"code": 0, "msg": "ok", "data": _serialize(run)}


@router.get("/history")
async def history(
    limit: int = Query(default=30, ge=1, le=100, description="返回条数"),
    user: User = Depends(get_current_user),
):
    """我的回测历史（按时间倒序，不含权益曲线以减小体积）。"""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(BacktestRun)
                .where(BacktestRun.user_id == user.id)
                .order_by(BacktestRun.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return {"code": 0, "msg": "ok", "data": {"items": [_serialize(r) for r in rows]}}


@router.delete("/history/{run_id}")
async def delete_history(run_id: str, user: User = Depends(get_current_user)):
    async with async_session() as db:
        run = await db.get(BacktestRun, run_id)
        if not run or run.user_id != user.id:
            raise HTTPException(404, "回测记录不存在")
        await db.delete(run)
        await db.commit()
    return {"code": 0, "msg": "ok", "data": {"deleted": run_id}}


@router.get("/compare")
async def compare(
    ids: str = Query(..., description="逗号分隔的回测记录 id（2-3 条）"),
    user: User = Depends(get_current_user),
):
    """多策略对比：返回选中记录（含权益曲线）。"""
    parts = [i.strip() for i in ids.split(",") if i.strip()]
    if len(parts) < 2:
        raise HTTPException(400, "至少选择 2 条回测记录")
    if len(parts) > 3:
        raise HTTPException(400, "最多对比 3 条回测记录")

    async with async_session() as db:
        rows = (
            await db.execute(
                select(BacktestRun).where(
                    BacktestRun.id.in_(parts), BacktestRun.user_id == user.id
                )
            )
        ).scalars().all()

    if len(rows) < 2:
        raise HTTPException(404, "回测记录不足，请重新选择")

    order = {rid: i for i, rid in enumerate(parts)}
    rows.sort(key=lambda r: order.get(r.id, len(parts)))
    return {
        "code": 0, "msg": "ok",
        "data": {"items": [_serialize(r, with_curve=True) for r in rows]},
    }
