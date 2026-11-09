"""合规审查薄适配（衡策 → Legal-Agent）。

衡策负责投研分析，Legal-Agent 负责合规批注。两侧独立部署，只通过 HTTP 契约对接：

    POST {LEGAL_AGENT_URL}/api/v1/report/review

职责：把分析回答整理成结构化报告、送审、把批注拼回回答。
Legal-Agent 超时/不可用时不影响分析结果，只标注「未审」，绝不静默当作已审。

配置见 app.config.Settings：
    COMPLIANCE_ENABLED / LEGAL_AGENT_URL / LEGAL_AGENT_TIMEOUT_SECONDS
"""

import asyncio
import json
import re
import time
import urllib.request
from typing import Any

from loguru import logger

from ..config import get_settings

_REVIEWABLE_INTENTS = {"investment_analysis", "risk_assessment"}

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n+")
_NUM_RE = re.compile(
    r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:%|％|亿元|亿|万元|万|元|倍)?(?![\d])"
)
_YEAR_OR_CODE_RE = re.compile(r"^(?:(?:19|20)\d{2}|[0368]\d{5})$")
_COUNT_SUFFIXES = ("个月", "年", "天", "周", "名", "人", "位")
_SOURCE_RE = re.compile(r"(?:数据来源|资料来源|来源)[：:]\s*([^\s，。；;]{2,40})")
_ACTION_HINTS = ("建议", "操作", "配置", "买入", "卖出", "增持", "减持", "目标价")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENT_SPLIT.split(text or "") if part.strip()]


def _paragraphs(text: str, limit: int = 12) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text or "") if p.strip()]
    return paragraphs[:limit]


def _overlaps(answer: str, source: str) -> bool:
    answer_norm = re.sub(r"\s+", "", answer)
    source_norm = re.sub(r"\s+", "", source)
    if len(source_norm) < 10:
        return bool(source_norm) and source_norm in answer_norm
    for start in range(0, len(source_norm) - 10, 6):
        if source_norm[start:start + 10] in answer_norm:
            return True
    return False


def _is_list_marker(text: str, start: int, end: int) -> bool:
    if start == 0 and text[end:end + 1] in (".", "、", ")", "）"):
        return True
    return start > 0 and text[start - 1] in ("、", "(", "（")


def _build_numbers(answer: str) -> list[dict[str, str]]:
    numbers: list[dict[str, str]] = []
    seen: set[str] = set()
    for sentence in _sentences(answer):
        source_match = _SOURCE_RE.search(sentence)
        source = source_match.group(1) if source_match else ""
        for match in _NUM_RE.finditer(sentence):
            value = match.group(0)
            if _YEAR_OR_CODE_RE.match(value.replace(",", "")) or value in seen:
                continue
            if _is_list_marker(sentence, match.start(), match.end()):
                continue
            suffix = sentence[match.end():match.end() + 4]
            if any(suffix.startswith(item) for item in _COUNT_SUFFIXES):
                continue
            seen.add(value)
            numbers.append({
                "value": value,
                "context": sentence[:150],
                "source": source,
                "evidence": "",
            })
            if len(numbers) >= 80:
                return numbers
    return numbers


def _build_citations(answer: str, state: dict[str, Any]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    for index, doc in enumerate(state.get("rag_sources") or [], 1):
        content = str(doc.get("content") or "").strip()
        if not content or not _overlaps(answer, content):
            continue
        citations.append({
            "ref": f"C{index}",
            "quote": content[:140],
            "source_text": content,
            "source_type": "公开",
        })
        if len(citations) >= 5:
            break
    return citations


def build_payload(answer: str, state: dict[str, Any], title: str = "") -> dict[str, Any]:
    actions = [
        sentence for sentence in _sentences(answer)
        if any(hint in sentence for hint in _ACTION_HINTS)
    ][:8]
    disclaimer = "本报告不构成投资建议。" if "不构成投资建议" in answer else ""
    return {
        "title": title or "衡策投研分析",
        "conclusions": _paragraphs(answer),
        "numbers": _build_numbers(answer),
        "citations": _build_citations(answer, state),
        "disclaimer": disclaimer,
        "actions": actions,
        "full_text": answer,
    }


def _render_section(meta: dict[str, Any]) -> str:
    status = meta.get("status")
    elapsed = meta.get("elapsed_ms", 0)
    if status == "unreviewed":
        return (
            "\n\n---\n【合规审查】未审：Legal-Agent 不可用"
            f"（{meta.get('message', '连接失败或超时')}）。以上内容未经合规护栏校验，"
            "请勿直接对外使用。"
        )
    if status == "skipped":
        return "\n\n---\n【合规审查】未开启（COMPLIANCE_ENABLED=false），以上内容未经合规护栏校验。"
    if status == "no_hit":
        return f"\n\n---\n【合规审查】无命中：未发现数字无来源、承诺收益、超资质表述或引用不合规问题（{elapsed:.0f}ms）。"

    annotations = meta.get("annotations") or []
    summary = meta.get("summary") or {}
    lines = [
        "\n\n---\n",
        f"【合规审查】共 {len(annotations)} 条批注"
        f"（高 {summary.get('high', 0)} / 中 {summary.get('medium', 0)} / 低 {summary.get('low', 0)}），"
        f"耗时 {elapsed:.0f}ms：\n",
    ]
    for item in annotations[:12]:
        lines.append(
            f"- [{item.get('rule_id')}｜{item.get('risk_level')}] {item.get('location')}："
            f"{item.get('evidence', '')[:80]}\n"
            f"  依据：{item.get('basis', '')}\n"
            f"  建议：{item.get('suggestion', '')}\n"
        )
    if len(annotations) > 12:
        lines.append(f"- 另有 {len(annotations) - 12} 条批注未展示。\n")
    return "".join(lines)


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


async def review_answer(answer: str, state: dict[str, Any]) -> dict[str, Any]:
    """送审一份分析回答。任何异常都降级为 unreviewed，不向上抛。"""
    settings = get_settings()
    base_url = settings.LEGAL_AGENT_URL.rstrip("/")
    timeout = float(settings.LEGAL_AGENT_TIMEOUT_SECONDS)
    url = base_url + "/api/v1/report/review"
    payload = build_payload(answer, state)
    started = time.perf_counter()
    try:
        data = await asyncio.to_thread(_post_json, url, payload, timeout)
        return {
            "status": data.get("status", "reviewed"),
            "annotations": data.get("annotations", []),
            "summary": data.get("summary", {}),
            "hit_types": data.get("hit_types", {}),
            "message": data.get("message", ""),
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "degraded": False,
        }
    except Exception as error:  # noqa: BLE001
        logger.warning("合规审查不可用（{}: {}）", type(error).__name__, error)
        return {
            "status": "unreviewed",
            "annotations": [],
            "summary": {},
            "hit_types": {},
            "message": type(error).__name__,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "degraded": True,
        }


async def enrich_result(result: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """对投资分析/风险评估结果送审，并把批注拼回 response。

    返回 (合规元数据, 追加的展示文本)。
    """
    answer = result.get("response") or ""
    intent = result.get("intent") or "chitchat"
    if intent not in _REVIEWABLE_INTENTS or not answer.strip():
        meta = {"status": "skipped", "annotations": [], "summary": {}, "elapsed_ms": 0, "degraded": False}
        result["compliance"] = meta
        return meta, ""
    if not get_settings().COMPLIANCE_ENABLED:
        meta = {"status": "skipped", "annotations": [], "summary": {}, "elapsed_ms": 0, "degraded": False}
        result["compliance"] = meta
        return meta, _render_section(meta)

    meta = await review_answer(answer, result)
    section = _render_section(meta)
    result["response"] = answer + section
    result["compliance"] = meta
    logger.info(
        "合规审查：intent={} status={} annotations={} elapsed_ms={:.0f}",
        intent, meta["status"], len(meta.get("annotations") or []), meta.get("elapsed_ms", 0),
    )
    return meta, section
