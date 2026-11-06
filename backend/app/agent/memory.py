"""对话历史读写 + 注入到 AgentState。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from loguru import logger
from sqlalchemy import select

from ..models.chat import ChatHistory


async def save_message(session, user_id: str, role: str, content: str,
                       intent: str | None = None, sources: str | None = None,
                       payload: str | None = None) -> None:
    msg = ChatHistory(user_id=user_id, role=role, content=content,
                      intent=intent, sources=sources, payload=payload)
    session.add(msg)
    await session.flush()


async def get_recent_history(session, user_id: str, limit: int = 8) -> List[ChatHistory]:
    cutoff = datetime.utcnow() - timedelta(hours=24)
    result = await session.execute(
        select(ChatHistory)
        .where(ChatHistory.user_id == user_id, ChatHistory.created_at >= cutoff)
        .order_by(ChatHistory.created_at.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


def format_for_prompt(history: List[ChatHistory]) -> str:
    """把 DB 记录格式化成 LLM 可读的上下文串。"""
    if not history:
        return ""
    lines = ["以下是过去 24 小时的对话历史（按时间顺序）："]
    for h in history:
        role = "用户" if h.role == "user" else "AI"
        lines.append(f"  [{role}] {h.content[:300]}")
    return "\n".join(lines)
