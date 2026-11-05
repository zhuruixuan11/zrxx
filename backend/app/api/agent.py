"""Agent 对话 API。"""
import asyncio
import time
from collections import OrderedDict
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_ipaddr

from ..agent.graph import run_agent
from ..config import get_settings
from ..database import async_session
from ..models.user import User
from ..security import get_current_user

limiter = Limiter(key_func=lambda request: get_ipaddr(request))

# 相同问题 10 分钟内直接复用上一次回答，避免重复调 LLM（LRU 上限防内存增长）
_ANSWER_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()  # (user_id, 归一化问题) -> (ts, data)
_ANSWER_TTL = 600.0
_ANSWER_MAX = 128


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    code: int
    msg: str
    data: dict


router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/chat")
@limiter.limit(lambda: get_settings().AGENT_CHAT_RATE_LIMIT)
async def chat(req: ChatRequest, request: Request, user: User = Depends(get_current_user)):
    """与 AI Agent 对话（主入口）。"""
    from ..agent.memory import save_message
    if not req.message.strip():
        return {"code": 400, "msg": "消息不能为空"}

    # 相同问题命中缓存时直接返回（不重复写聊天记录、不重复调 LLM）
    cache_key = (user.id, " ".join(req.message.split()))
    hit = _ANSWER_CACHE.get(cache_key)
    if hit is not None:
        if time.monotonic() - hit[0] <= _ANSWER_TTL:
            _ANSWER_CACHE.move_to_end(cache_key)
            return {"code": 0, "msg": "ok", "data": {**hit[1], "cached": True}}
        _ANSWER_CACHE.pop(cache_key, None)

    async with async_session() as session:
        # 保存用户消息
        await save_message(session, user.id, "user", req.message)
        await session.commit()

        # 运行 Agent 图（兜底 75s，避免前端无限等待）
        try:
            result = await asyncio.wait_for(
                run_agent(user.id, req.message, session=session), timeout=75
            )
        except asyncio.TimeoutError:
            result = {
                "response": "抱歉，这次分析耗时过长（数据源或模型响应超时）。请稍后重试，或把问题拆小一点再问。",
                "response_type": "text",
                "intent": "chitchat",
            }

        # 保存 AI 回复
        intent = result.get("intent")
        sources = result.get("rag_sources")
        await save_message(
            session, user.id, "assistant",
            result.get("response", "抱歉，我暂时无法回答。"),
            intent=intent,
            sources=str(sources) if sources else None,
        )
        await session.commit()

    data = {
        "response": result.get("response", ""),
        "response_type": result.get("response_type", "text"),
        "intent": result.get("intent"),
        "confidence": result.get("intent_confidence"),
        "needs_clarification": result.get("needs_clarification", False),
        "sources": result.get("rag_sources", []),
    }
    # 只缓存成功回答；超时兜底是暂态，不缓存
    if result.get("response_type") != "timeout":
        _ANSWER_CACHE[cache_key] = (time.monotonic(), data)
        _ANSWER_CACHE.move_to_end(cache_key)
        while len(_ANSWER_CACHE) > _ANSWER_MAX:
            _ANSWER_CACHE.popitem(last=False)

    return {"code": 0, "msg": "ok", "data": data}


@router.get("/history")
async def history(limit: int = 20, user: User = Depends(get_current_user)):
    async with async_session() as session:
        from sqlalchemy import select
        from ..models.chat import ChatHistory
        rows = (await session.execute(
            select(ChatHistory).where(ChatHistory.user_id == user.id)
            .order_by(ChatHistory.created_at.desc()).limit(limit)
        )).scalars().all()
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "role": r.role, "content": r.content[:500],
            "intent": r.intent, "created_at": r.created_at.strftime("%Y-%m-%d %H:%M"),
        })
    # 倒序查、正序返回，前端可直接渲染
    out.reverse()
    return {"code": 0, "msg": "ok", "data": {"list": out, "total": len(out)}}


@router.delete("/history")
async def clear_history(user: User = Depends(get_current_user)):
    """清空当前用户的对话历史（本地与服务端同时清）。"""
    async with async_session() as session:
        from sqlalchemy import delete
        from ..models.chat import ChatHistory
        result = await session.execute(
            delete(ChatHistory).where(ChatHistory.user_id == user.id)
        )
        await session.commit()
    return {"code": 0, "msg": "ok", "data": {"deleted": result.rowcount or 0}}
