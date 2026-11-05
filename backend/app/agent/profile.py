"""用户投资画像：读写 + 生成给 LLM 的画像摘要。

画像绑定在 User 表字段上（而非独立文件），随账号走、多设备一致，也能被 Agent 直接读取。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from ..database import async_session
from ..models.user import RiskLevel, User

RISK_LABELS = {
    "conservative": "保守（以保本为先，能接受回撤 ≤ 10%）",
    "moderate": "稳健（追求风险收益平衡，可接受回撤 10-20%）",
    "balanced": "平衡（愿意承担一定波动换取更高收益）",
    "aggressive": "积极（追求高收益，可接受大幅回撤）",
}

HORIZON_LABELS = {
    "short": "短线（1-3 个月）",
    "medium": "中期（3-12 个月）",
    "long": "长线（1 年以上）",
}


def _load_list(raw: str | None) -> List[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(x) for x in val] if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def profile_to_dict(user: User) -> Dict[str, Any]:
    return {
        "username": user.username,
        "risk_level": user.risk_level.value if user.risk_level else "moderate",
        "investment_horizon": user.investment_horizon or "",
        "preferred_sectors": _load_list(user.preferred_sectors),
        "avoided_sectors": _load_list(user.avoided_sectors),
        "investment_style": user.investment_style or "",
        "bio": user.bio or "",
    }


def build_profile_summary(p: Dict[str, Any]) -> str:
    """把画像转成一段中文，塞进 LLM 上下文。字段全空时返回空串，避免污染 prompt。"""
    lines: List[str] = []
    risk = RISK_LABELS.get(p.get("risk_level", ""), "")
    horizon = HORIZON_LABELS.get(p.get("investment_horizon", ""), "")
    if risk:
        lines.append(f"- 风险偏好：{risk}")
    if horizon:
        lines.append(f"- 投资期限：{horizon}")
    if p.get("preferred_sectors"):
        lines.append(f"- 偏好板块：{'、'.join(p['preferred_sectors'])}")
    if p.get("avoided_sectors"):
        lines.append(f"- 回避板块：{'、'.join(p['avoided_sectors'])}")
    if p.get("investment_style"):
        lines.append(f"- 投资风格：{p['investment_style']}")

    if not lines:
        return ""
    return (
        "【用户投资画像】\n"
        + "\n".join(lines)
        + "\n请在建议的仓位大小、标的范围、风险提示上贴合该画像；"
        "若用户明确要求超出其风险偏好，先说明风险再执行。"
    )


async def get_profile_summary(user_id: str) -> str:
    """按 user_id 取画像摘要；查不到返回空串。"""
    if not user_id:
        return ""
    async with async_session() as db:
        user = await db.get(User, user_id)
    if user is None:
        return ""
    return build_profile_summary(profile_to_dict(user))


async def get_risk_level(user_id: str) -> str:
    """单独取风险偏好（风险评估节点用来调整仓位建议）。"""
    if not user_id:
        return "moderate"
    async with async_session() as db:
        user = await db.get(User, user_id)
    return user.risk_level.value if user and user.risk_level else "moderate"


def normalize_risk_level(raw: str | None) -> RiskLevel | None:
    if not raw:
        return None
    try:
        return RiskLevel(str(raw).lower())
    except ValueError:
        return None
