"""认证 API：注册/登录/当前用户。"""
from datetime import datetime, timedelta
from typing import Optional

import fastapi
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from slowapi import Limiter
from slowapi.util import get_ipaddr
from sqlalchemy import select

from ..config import get_settings
from ..database import async_session
from ..models.portfolio import Portfolio
from ..models.user import User, RiskLevel
from ..security import (
    check_login_lock,
    clear_login_fail,
    create_access_token,
    get_current_user,
    hash_password,
    record_login_fail,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()
limiter = Limiter(key_func=get_ipaddr)


class RegisterRequest(BaseModel):
    """注册请求体。"""
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)
    risk_level: Optional[str] = Field(default="moderate")


@router.post("/register")
@limiter.limit(lambda: get_settings().LOGIN_RATE_LIMIT)
async def register(payload: RegisterRequest, request: Request):
    username = payload.username
    email = payload.email
    password = payload.password
    risk_level = payload.risk_level
    async with async_session() as session:
        # 检查用户名/邮箱唯一
        existing = await session.execute(select(User).where(
            (User.username == username) | (User.email == email)))
        if existing.scalar_one_or_none():
            raise HTTPException(400, "用户名或邮箱已被注册")

        if len(password) < 6:
            raise HTTPException(400, "密码至少 6 位")

        # 解析 risk_level，非法值回退 MODERATE
        try:
            rl = RiskLevel(risk_level.lower())
        except ValueError:
            rl = RiskLevel.MODERATE

        user = User(username=username, email=email, password_hash=hash_password(password),
                    risk_level=rl)
        session.add(user)
        await session.flush()

        # 创建默认投资组合（10 万初始资金）
        portfolio = Portfolio(user_id=user.id, name="我的投资组合",
                              initial_capital=100_000, cash_balance=100_000)
        session.add(portfolio)
        await session.commit()
        await session.refresh(user)

    # 直接签发 token 让用户登录
    token = create_access_token({"sub": user.id, "username": user.username})
    return {
        "code": 0, "msg": "注册成功",
        "data": {"token": token, "token_type": "bearer",
                 "user": {"id": user.id, "username": user.username, "email": user.email}},
    }


@router.post("/login")
@limiter.limit(settings.LOGIN_RATE_LIMIT)
async def login(form: OAuth2PasswordRequestForm = Depends(), request: Request = None):
    # 1) 检查锁定状态
    lock_msg = check_login_lock(form.username)
    if lock_msg:
        raise HTTPException(429, lock_msg)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.username == form.username))
        user = result.scalar_one_or_none()

        if not user or not verify_password(form.password, user.password_hash):
            # 记录失败
            record_login_fail(form.username)
            raise HTTPException(401, "用户名或密码错误")

        # 登录成功 → 清失败计数 + 更新 last_login
        clear_login_fail(form.username)
        user.last_login_at = datetime.utcnow()
        await session.commit()

    token = create_access_token({"sub": user.id, "username": user.username})
    return {
        "code": 0, "msg": "登录成功",
        "data": {"token": token, "token_type": "bearer",
                 "user": {"id": user.id, "username": user.username,
                          "email": user.email, "risk_level": user.risk_level.value}},
    }


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "code": 0, "msg": "ok",
        "data": {"id": user.id, "username": user.username, "email": user.email,
                 "risk_level": user.risk_level.value, "bio": user.bio},
    }
