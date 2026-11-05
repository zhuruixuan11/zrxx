"""投资组合 API。"""
from fastapi import APIRouter, Depends, HTTPException
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
