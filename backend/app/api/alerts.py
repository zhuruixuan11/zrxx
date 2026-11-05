"""到价预警 API。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import get_settings
from ..database import async_session
from ..models.alert import PriceAlert
from ..models.user import User
from ..security import get_current_user
from ..services.alerts import _send_email, check_all_alerts, condition_text, is_trading_now

router = APIRouter(prefix="/api/alerts", tags=["alerts"])
settings = get_settings()

AlertType = Literal["price", "change", "cost"]


class AlertCreate(BaseModel):
    symbol: str = Field(..., min_length=6, max_length=10)
    name: Optional[str] = None
    alert_type: AlertType = "price"
    operator: Literal[">=", "<=", ">", "<"] = ">="
    threshold: Optional[float] = Field(default=None, gt=0)
    # cost 类型：持仓成本价
    reference_price: Optional[float] = Field(default=None, gt=0)
    # change / cost 类型：涨跌幅阈值（带符号，%）
    change_pct: Optional[float] = Field(default=None, ge=-100, le=100)
    note: Optional[str] = None


class AlertUpdate(BaseModel):
    enabled: Optional[bool] = None
    threshold: Optional[float] = Field(default=None, gt=0)
    change_pct: Optional[float] = Field(default=None, ge=-100, le=100)


def _serialize(a: PriceAlert) -> dict:
    return {
        "id": a.id,
        "symbol": a.symbol,
        "name": a.name or a.symbol,
        "alert_type": a.alert_type or "price",
        "operator": a.operator,
        "threshold": a.threshold,
        "reference_price": a.reference_price,
        "change_pct": a.change_pct,
        "condition": condition_text(a),
        "note": a.note,
        "enabled": a.enabled,
        "last_price": a.last_price,
        "last_checked_at": a.last_checked_at.strftime("%Y-%m-%d %H:%M:%S") if a.last_checked_at else None,
        "triggered_at": a.triggered_at.strftime("%Y-%m-%d %H:%M:%S") if a.triggered_at else None,
        "triggered_price": a.triggered_price,
        "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else None,
    }


@router.get("")
async def list_alerts(user: User = Depends(get_current_user)):
    """我的全部预警（监控中 + 已触发），按创建时间倒序。"""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(PriceAlert)
                .where(PriceAlert.user_id == user.id)
                .order_by(PriceAlert.created_at.desc())
            )
        ).scalars().all()

    items = [_serialize(a) for a in rows]
    return {
        "code": 0, "msg": "ok",
        "data": {
            "items": items,
            "monitor": {
                "enabled": settings.ALERT_ENABLED,
                "poll_interval_seconds": settings.ALERT_POLL_INTERVAL_SECONDS,
                "trading_hours_only": settings.ALERT_TRADING_HOURS_ONLY,
                "is_trading_now": is_trading_now(),
                "email_configured": bool(settings.SMTP_HOST and settings.SMTP_USER),
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        },
    }


@router.post("")
async def create_alert(payload: AlertCreate, user: User = Depends(get_current_user)):
    symbol = payload.symbol.strip().upper()
    # 按预警类型做条件校验，避免存进无法触发的空条件
    if payload.alert_type == "price" and payload.threshold is None:
        raise HTTPException(400, "价格预警需要填写价格阈值")
    if payload.alert_type == "change" and payload.change_pct is None:
        raise HTTPException(400, "涨跌幅预警需要填写涨跌幅阈值")
    if payload.alert_type == "cost" and (
        payload.change_pct is None or payload.reference_price is None
    ):
        raise HTTPException(400, "止盈止损预警需要填写成本价与涨跌幅阈值")

    async with async_session() as db:
        alert = PriceAlert(
            user_id=user.id,
            symbol=symbol,
            name=(payload.name or "").strip() or None,
            alert_type=payload.alert_type,
            operator=payload.operator,
            threshold=payload.threshold or 0.0,
            reference_price=payload.reference_price,
            change_pct=payload.change_pct,
            note=payload.note,
        )
        db.add(alert)
        await db.commit()
        await db.refresh(alert)
        return {"code": 0, "msg": "ok", "data": _serialize(alert)}


@router.put("/{alert_id}")
async def update_alert(
    alert_id: str, payload: AlertUpdate, user: User = Depends(get_current_user)
):
    """启停预警。重新启用会清空上一次的触发记录。"""
    async with async_session() as db:
        alert = await db.get(PriceAlert, alert_id)
        if not alert or alert.user_id != user.id:
            raise HTTPException(404, "预警不存在")
        if payload.threshold is not None:
            alert.threshold = payload.threshold
        if payload.change_pct is not None:
            alert.change_pct = payload.change_pct
        if payload.enabled is not None:
            alert.enabled = payload.enabled
            if payload.enabled:
                alert.triggered_at = None
                alert.triggered_price = None
        await db.commit()
        await db.refresh(alert)
        return {"code": 0, "msg": "ok", "data": _serialize(alert)}


@router.delete("/{alert_id}")
async def delete_alert(alert_id: str, user: User = Depends(get_current_user)):
    async with async_session() as db:
        alert = await db.get(PriceAlert, alert_id)
        if not alert or alert.user_id != user.id:
            raise HTTPException(404, "预警不存在")
        await db.delete(alert)
        await db.commit()
    return {"code": 0, "msg": "ok", "data": {"deleted": alert_id}}


@router.post("/check")
async def check_now(user: User = Depends(get_current_user)):
    """立即检查一轮（非交易时段也能验证条件配置是否生效）。"""
    result = await check_all_alerts()
    return {
        "code": 0, "msg": "ok",
        "data": {
            "checked": result["checked"],
            "triggered": len(result["triggered"]),
            "items": result["triggered"],
        },
    }


@router.post("/test-email")
async def test_email(user: User = Depends(get_current_user)):
    """给自己发一封测试邮件，验证 SMTP 配置是否真的可用。"""
    if not (settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD):
        raise HTTPException(
            400,
            "未配置 SMTP，请在 backend/.env 填写 SMTP_HOST / SMTP_USER / SMTP_PASSWORD 后重启后端",
        )
    if not user.email:
        raise HTTPException(400, "当前账号没有邮箱，请先在个人中心补充")
    ok = await asyncio.to_thread(
        _send_email,
        "[衡策] 预警邮件测试",
        "这是一封测试邮件，收到说明衡策的到价预警推送已配置成功。",
        user.email,
    )
    if not ok:
        raise HTTPException(502, "邮件发送失败，请检查 SMTP 端口、授权码与收发件人")
    return {"code": 0, "msg": "ok", "data": {"sent_to": user.email}}
