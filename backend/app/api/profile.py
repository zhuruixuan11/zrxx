"""用户投资画像 API。"""
from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..agent.profile import (
    HORIZON_LABELS,
    build_profile_summary,
    normalize_risk_level,
    profile_to_dict,
)
from ..database import async_session
from ..models.user import User
from ..security import get_current_user

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    risk_level: Optional[str] = None
    investment_horizon: Optional[str] = None
    preferred_sectors: Optional[List[str]] = None
    avoided_sectors: Optional[List[str]] = None
    investment_style: Optional[str] = Field(default=None, max_length=500)
    bio: Optional[str] = Field(default=None, max_length=500)


def _wrap(data: dict) -> dict:
    return {"code": 0, "msg": "ok", "data": data}


@router.get("")
async def get_profile(user: User = Depends(get_current_user)):
    profile = profile_to_dict(user)
    return _wrap({
        "profile": profile,
        "summary": build_profile_summary(profile),
        "options": {
            "risk_levels": [
                {"value": "conservative", "label": "保守"},
                {"value": "moderate", "label": "稳健"},
                {"value": "balanced", "label": "平衡"},
                {"value": "aggressive", "label": "积极"},
            ],
            "horizons": [{"value": k, "label": v.split("（")[0]} for k, v in HORIZON_LABELS.items()],
        },
    })


@router.put("")
async def update_profile(payload: ProfileUpdate, user: User = Depends(get_current_user)):
    async with async_session() as db:
        row = await db.get(User, user.id)
        if row is None:
            raise HTTPException(404, "用户不存在")

        if payload.risk_level is not None:
            risk = normalize_risk_level(payload.risk_level)
            if risk is None:
                raise HTTPException(400, "风险偏好取值非法")
            row.risk_level = risk

        if payload.investment_horizon is not None:
            h = payload.investment_horizon.strip().lower()
            if h and h not in HORIZON_LABELS:
                raise HTTPException(400, "投资期限取值非法")
            row.investment_horizon = h or None

        if payload.preferred_sectors is not None:
            row.preferred_sectors = json.dumps(
                [s.strip() for s in payload.preferred_sectors if s.strip()], ensure_ascii=False
            )
        if payload.avoided_sectors is not None:
            row.avoided_sectors = json.dumps(
                [s.strip() for s in payload.avoided_sectors if s.strip()], ensure_ascii=False
            )
        if payload.investment_style is not None:
            row.investment_style = payload.investment_style.strip() or None
        if payload.bio is not None:
            row.bio = payload.bio.strip() or None

        await db.commit()
        await db.refresh(row)

    profile = profile_to_dict(row)
    return _wrap({"profile": profile, "summary": build_profile_summary(profile)})
