"""安全工具：密码哈希、JWT、限流。"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from loguru import logger
from passlib.context import CryptContext

from .config import get_settings
from .database import async_session
from .models.user import User

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    settings = get_settings()
    to_encode = dict(data)
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.JWT_EXPIRATION_MINUTES)
    )
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


# ========== FastAPI 依赖：获取当前用户 ==========
async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
    except JWTError as e:
        logger.warning("JWT 解析失败：%s", e)
        raise HTTPException(status_code=401, detail="登录已过期或凭据无效")

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


# ========== 密码防暴破（内存 fallback；有 Redis 可升级）========
_fail_counts: dict[str, tuple[int, float]] = {}  # key -> (count, lock_until_ts)


def check_login_lock(username: str) -> Optional[str]:
    """返回 None 表示可以尝试；返回锁定消息表示已被锁定。"""
    global _fail_counts
    now = datetime.now().timestamp()
    item = _fail_counts.get(username)
    if item and item[1] > now:
        minutes_left = int((item[1] - now) / 60) + 1
        return f"账号已锁定，请 {minutes_left} 分钟后再试"
    if item and item[1] <= now:
        _fail_counts.pop(username, None)
    return None


def record_login_fail(username: str, lock_minutes: int = 15, max_fails: int = 5):
    global _fail_counts
    now = datetime.now().timestamp()
    item = _fail_counts.get(username)
    count = (item[0] if item else 0) + 1
    if count >= max_fails:
        _fail_counts[username] = (count, now + lock_minutes * 60)
        logger.warning("用户 %s 连续 %d 次登录失败，锁定 %d 分钟", username, count, lock_minutes)
    else:
        _fail_counts[username] = (count, item[1] if item and item[1] > now else 0)


def clear_login_fail(username: str):
    _fail_counts.pop(username, None)
