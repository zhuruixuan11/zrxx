"""持仓跟随监控 API。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..agent.tools import normalize_a_share
from ..config import get_settings
from ..database import async_session
from ..models.user import User
from ..models.watch import WatchEvent, WatchItem
from ..security import get_current_user
from ..services.alerts import is_trading_now
from ..services.watch import (
    check_watch_items,
    create_watch_item,
    email_configured,
    list_watch_events,
    list_watch_items,
)

router = APIRouter(prefix="/api/watch", tags=["watch"])
settings = get_settings()


class WatchCreate(BaseModel):
    symbol: str = Field(..., min_length=6, max_length=10, description="股票代码，如 600519")
    name: Optional[str] = None
    price_threshold_pct: float = Field(default=5.0, ge=0.5, le=20, description="价格异动阈值（%）")
    watch_price: bool = True
    watch_holding: bool = True
    watch_announcement: bool = True


class WatchUpdate(BaseModel):
    enabled: Optional[bool] = None
    price_threshold_pct: Optional[float] = Field(default=None, ge=0.5, le=20)
    watch_price: Optional[bool] = None
    watch_holding: Optional[bool] = None
    watch_announcement: Optional[bool] = None


def _serialize_item(w: WatchItem) -> dict:
    return {
        "id": w.id,
        "symbol": w.symbol,
        "name": w.name or w.symbol,
        "price_threshold_pct": w.price_threshold_pct,
        "watch_price": w.watch_price,
        "watch_holding": w.watch_holding,
        "watch_announcement": w.watch_announcement,
        "baseline_quantity": w.baseline_quantity,
        "baseline_at": w.baseline_at.strftime("%Y-%m-%d %H:%M:%S") if w.baseline_at else None,
        "enabled": w.enabled,
        "last_price": w.last_price,
        "last_checked_at": w.last_checked_at.strftime("%Y-%m-%d %H:%M:%S") if w.last_checked_at else None,
        "last_announcement_at": (
            w.last_announcement_at.strftime("%Y-%m-%d %H:%M:%S") if w.last_announcement_at else None
        ),
        "created_at": w.created_at.strftime("%Y-%m-%d %H:%M:%S") if w.created_at else None,
    }


def _serialize_event(e: WatchEvent) -> dict:
    return {
        "id": e.id,
        "watch_id": e.watch_id,
        "symbol": e.symbol,
        "event_type": e.event_type,
        "title": e.title,
        "detail": e.detail,
        "notified": e.notified,
        "created_at": e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else None,
    }


@router.get("")
async def list_items(user: User = Depends(get_current_user)):
    """关注列表 + 最近事件 + 监控状态。"""
    items = await list_watch_items(user.id)
    events = await list_watch_events(user.id, limit=50)
    return {
        "code": 0, "msg": "ok",
        "data": {
            "items": [_serialize_item(w) for w in items],
            "events": [_serialize_event(e) for e in events],
            "monitor": {
                "enabled": settings.ALERT_ENABLED,
                "poll_interval_seconds": settings.ALERT_POLL_INTERVAL_SECONDS,
                "is_trading_now": is_trading_now(),
                "email_configured": email_configured(),
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        },
    }


@router.post("")
async def create_item(payload: WatchCreate, user: User = Depends(get_current_user)):
    if not normalize_a_share(payload.symbol):
        raise HTTPException(400, "股票代码不合法（应为 6 位 A 股代码）")
    try:
        item = await create_watch_item(
            user_id=user.id,
            symbol=payload.symbol,
            name=payload.name,
            price_threshold_pct=payload.price_threshold_pct,
            watch_price=payload.watch_price,
            watch_holding=payload.watch_holding,
            watch_announcement=payload.watch_announcement,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"code": 0, "msg": "ok", "data": _serialize_item(item)}


@router.put("/{item_id}")
async def update_item(item_id: str, payload: WatchUpdate, user: User = Depends(get_current_user)):
    async with async_session() as db:
        item = await db.get(WatchItem, item_id)
        if not item or item.user_id != user.id:
            raise HTTPException(404, "关注标的不存在")
        if payload.enabled is not None:
            item.enabled = payload.enabled
        if payload.price_threshold_pct is not None:
            item.price_threshold_pct = payload.price_threshold_pct
        if payload.watch_price is not None:
            item.watch_price = payload.watch_price
        if payload.watch_holding is not None:
            item.watch_holding = payload.watch_holding
        if payload.watch_announcement is not None:
            item.watch_announcement = payload.watch_announcement
        await db.commit()
        await db.refresh(item)
        return {"code": 0, "msg": "ok", "data": _serialize_item(item)}


@router.delete("/{item_id}")
async def delete_item(item_id: str, user: User = Depends(get_current_user)):
    async with async_session() as db:
        item = await db.get(WatchItem, item_id)
        if not item or item.user_id != user.id:
            raise HTTPException(404, "关注标的不存在")
        await db.delete(item)
        await db.commit()
    return {"code": 0, "msg": "ok", "data": {"deleted": item_id}}


@router.post("/check")
async def check_now(force: bool = True, user: User = Depends(get_current_user)):
    """立即跑一轮检查（只检查当前用户）。force=True 时价格规则不受交易时段限制。"""
    result = await check_watch_items(user_id=user.id, force=force)
    return {"code": 0, "msg": "ok", "data": result}


@router.get("/events")
async def events(limit: int = 50, user: User = Depends(get_current_user)):
    rows = await list_watch_events(user.id, limit=min(max(limit, 1), 200))
    return {"code": 0, "msg": "ok", "data": {"items": [_serialize_event(e) for e in rows]}}
