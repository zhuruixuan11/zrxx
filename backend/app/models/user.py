"""用户模型。UUID 用 String(36) 存 hex——避免 PostgreSQL 专属的 postgresql.UUID 方言锁死。"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class RiskLevel(str, enum.Enum):
    CONSERVATIVE = "conservative"  # 保守
    MODERATE = "moderate"          # 稳健
    BALANCED = "balanced"          # 平衡
    AGGRESSIVE = "aggressive"      # 积极


def _new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    risk_level: Mapped[RiskLevel] = mapped_column(SAEnum(RiskLevel), default=RiskLevel.MODERATE)
    avatar_url: Mapped[str] = mapped_column(String(500), nullable=True)
    bio: Mapped[str] = mapped_column(String(500), nullable=True)

    # 投资画像（喂给 Agent，让建议贴合个人风格）
    investment_horizon: Mapped[str | None] = mapped_column(String(20), nullable=True)  # short/medium/long
    preferred_sectors: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 数组
    avoided_sectors: Mapped[str | None] = mapped_column(Text, nullable=True)    # JSON 数组
    investment_style: Mapped[str | None] = mapped_column(Text, nullable=True)   # 自由描述

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    portfolios = relationship("Portfolio", back_populates="user", cascade="all, delete-orphan")
    chat_histories = relationship("ChatHistory", back_populates="user", cascade="all, delete-orphan")
