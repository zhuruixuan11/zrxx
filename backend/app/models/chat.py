"""Agent 对话历史。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def _new_id() -> str:
    return uuid.uuid4().hex


class ChatHistory(Base):
    __tablename__ = "chat_histories"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Agent 端额外信息
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sources: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 字符串

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", back_populates="chat_histories")
