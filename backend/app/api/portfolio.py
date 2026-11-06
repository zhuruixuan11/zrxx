"""投资组合 API。"""
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..database import async_session
from ..models.portfolio import Holding, Portfolio, Trade, TradeAction
from ..models.user import User
from ..security import get_current_user
from ..agent.tools import get_historical, get_portfolio_summary, get_quote

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("/summary")
async def summary(user: User = Depends(get_current_user)):
    async with async_session() as session:
        data = await get_portfolio_summary(user.id, session)
    return {"code": 0, "msg": "ok", "data": data}


@router.get("/holdings")
async def holdings(user: User = Depends(get_current_user)):
    async with async_session() as session:
        data = await get_portfolio_summary(user.id, session)
    return {"code": 0, "msg": "ok", "data": {"list": data["holdings"], "total": len(data["holdings"])}}


@router.post("/holdings/add")
async def add_holding(symbol: str, quantity: float, price: float, asset_type: str = "stock",
                      user: User = Depends(get_current_user)):
    """添加/更新持仓（简化：同一 symbol 用加权平均成本合并）。"""
    async with async_session() as session:
        p = (await session.execute(select(Portfolio).where(Portfolio.user_id == user.id))).scalar_one_or_none()
        if not p:
            raise HTTPException(404, "未找到投资组合")

        h = (await session.execute(
            select(Holding).where(Holding.portfolio_id == p.id, Holding.symbol == symbol.upper())
        )).scalar_one_or_none()

        if h:
            # 加权平均
            new_qty = float(h.quantity) + quantity
            new_cost = (float(h.avg_cost) * float(h.quantity) + price * quantity) / max(new_qty, 0.0001)
            h.quantity = new_qty
            h.avg_cost = new_cost
        else:
            h = Holding(portfolio_id=p.id, symbol=symbol.upper(),
                        asset_type=asset_type, quantity=quantity, avg_cost=price)
            session.add(h)

        # 扣现金
        if p.cash_balance < quantity * price:
            raise HTTPException(400, "可用现金不足")
        p.cash_balance = float(p.cash_balance) - quantity * price
        await session.commit()

        # 同时记一笔 Trade
        trade = Trade(portfolio_id=p.id, symbol=symbol.upper(), action=TradeAction.BUY,
                      quantity=quantity, price=price, total_amount=quantity * price, status="executed")
        session.add(trade)
        await session.commit()

    return {"code": 0, "msg": "持仓已更新"}


@router.get("/trades")
async def trades(user: User = Depends(get_current_user), limit: int = 20):
    async with async_session() as session:
        p = (await session.execute(select(Portfolio).where(Portfolio.user_id == user.id))).scalar_one_or_none()
        if not p:
            return {"code": 0, "data": {"list": [], "total": 0}}
        rows = (await session.execute(
            select(Trade).where(Trade.portfolio_id == p.id).order_by(Trade.trade_time.desc()).limit(limit)
        )).scalars().all()
    out = []
    for t in rows:
        out.append({
            "id": t.id, "symbol": t.symbol, "action": t.action.value,
            "quantity": float(t.quantity), "price": float(t.price),
            "total_amount": float(t.total_amount), "trade_time": t.trade_time.strftime("%Y-%m-%d %H:%M"),
        })
    return {"code": 0, "data": {"list": out, "total": len(out)}}


class FromBacktestPayload(BaseModel):
    """从回测结果一键开模拟仓（取首笔 BUY 作为当前持仓）。"""
    symbol: str = Field(..., description="股票代码")
    strategy: str = Field(..., description="策略 id")
    trades: List[dict] = Field(..., description="回测 trades 列表（含 date/action/price/shares）")
    initial_capital: float = Field(default=100_000, ge=1000)
    notes: Optional[str] = None


@router.post("/from-backtest")
async def from_backtest(payload: FromBacktestPayload, user: User = Depends(get_current_user)):
    """用回测结果里的首笔 BUY 建一笔模拟持仓。

    逻辑：
    1) 取回测 trades 中**第一笔 BUY**（不是最后一笔）作为开仓记录
    2) 买入价 = 回测成交价（避免用"现在的价格"干扰回测验证）
    3) 如果用户已有同 symbol 持仓 → 追加（加权平均成本）
    4) 自动调整现金余额
    """
    # 找到第一笔 BUY
    first_buy = None
    for t in payload.trades:
        if t.get("action", "").upper() == "BUY":
            first_buy = t
            break

    if first_buy is None:
        raise HTTPException(400, "回测结果里没有 BUY 记录，无法开仓")

    try:
        buy_price = float(first_buy["price"])
        buy_shares = float(first_buy["shares"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "BUY 记录缺少 price/shares 字段")

    if buy_price <= 0 or buy_shares <= 0:
        raise HTTPException(400, "BUY 价格或数量不合法")

    symbol = payload.symbol.upper()
    total_amount = buy_price * buy_shares
    commission = round(total_amount * 0.0003, 2)

    async with async_session() as session:
        p = (await session.execute(
            select(Portfolio).where(Portfolio.user_id == user.id)
        )).scalar_one_or_none()

        if not p:
            # 用户还没有 portfolio → 用回测初始资金新建一个
            p = Portfolio(
                user_id=user.id,
                name=f"从 {payload.strategy} 回测创建",
                initial_capital=payload.initial_capital,
                cash_balance=payload.initial_capital - total_amount - commission,
            )
            session.add(p)
            await session.flush()
        else:
            # 已有 portfolio → 检查现金是否够（不够就先"充值"到能覆盖本次买入）
            if p.cash_balance < total_amount + commission:
                needed = total_amount + commission - float(p.cash_balance)
                p.initial_capital = float(p.initial_capital) + needed
                p.cash_balance = float(p.cash_balance) + needed
            p.cash_balance = float(p.cash_balance) - total_amount - commission

        # 同一 symbol 追加（加权平均成本）
        h = (await session.execute(
            select(Holding).where(Holding.portfolio_id == p.id, Holding.symbol == symbol)
        )).scalar_one_or_none()

        if h:
            new_qty = float(h.quantity) + buy_shares
            new_cost = (float(h.avg_cost) * float(h.quantity) + buy_price * buy_shares) / max(new_qty, 0.0001)
            h.quantity = new_qty
            h.avg_cost = new_cost
        else:
            h = Holding(
                portfolio_id=p.id,
                symbol=symbol,
                asset_type="stock",
                quantity=buy_shares,
                avg_cost=buy_price,
            )
            session.add(h)

        trade_notes = payload.notes or f"从 {payload.strategy} 回测一键开仓（参考成交价 {buy_price}）"
        trade = Trade(
            portfolio_id=p.id,
            symbol=symbol,
            action=TradeAction.BUY,
            quantity=buy_shares,
            price=buy_price,
            commission=commission,
            total_amount=total_amount,
            notes=trade_notes,
            trade_time=datetime.utcnow(),
        )
        session.add(trade)
        await session.commit()

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "symbol": symbol,
            "shares": buy_shares,
            "price": buy_price,
            "total_amount": round(total_amount, 2),
            "portfolio_cash": round(float(p.cash_balance), 2),
            "notes": trade_notes,
        },
    }
