"""阿里百炼 Qwen 微调：数据集导出、格式校验、文件上传、任务管理与模型激活。

设计要点：
- 导出与校验纯本地，不依赖网络和 API Key；
- 上传/训练/查询走 DashScope REST（httpx 已在依赖中，不新增包）；
- 微调接口仅华北2（北京）地域可用，Key 必须是该地域的。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from typing import Any, Optional

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import async_session
from ..llm.client import set_model_override
from ..models.dataset import Dataset, DatasetSample
from ..models.finetune import FineTuneJob

_REMOTE_TIMEOUT = 60.0
_UPLOAD_TIMEOUT = 300.0


class FineTuneError(Exception):
    """微调流程中的可预期错误（缺 Key、远端拒绝、数据不合格等）。"""


# ============ 本地：导出与校验 ============

def estimate_tokens(text: str) -> int:
    """粗略估算 token：中文约 1.5 字符/token，取保守值用于 max_length 预检。"""
    return max(1, math.ceil(len(text or "") / 1.5))


def _sft_row(s: DatasetSample) -> Optional[dict]:
    """SFT → DashScope ChatML：{"messages":[...]}。"""
    if s.messages_json:
        try:
            msgs = json.loads(s.messages_json)
        except Exception:
            msgs = None
        if isinstance(msgs, list) and msgs:
            return {"messages": msgs}

    if not (s.instruction and s.output):
        return None
    user = s.instruction if not s.input_text else f"{s.instruction}\n\n{s.input_text}"
    return {
        "messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": s.output},
        ]
    }


def _dpo_row(s: DatasetSample) -> Optional[dict]:
    """DPO → prompt/chosen/rejected 结构（字段格式待官方 DPO 规范二次校验）。"""
    prompt = s.prompt or s.instruction
    if not (prompt and s.chosen and s.rejected):
        return None
    return {
        "messages": [{"role": "user", "content": prompt}],
        "chosen": {"role": "assistant", "content": s.chosen},
        "rejected": {"role": "assistant", "content": s.rejected},
    }


_ADAPTERS = {"sft": _sft_row, "dpo": _dpo_row}


def _row_text(row: dict) -> str:
    parts = [m.get("content", "") for m in row.get("messages", []) if isinstance(m, dict)]
    for key in ("chosen", "rejected"):
        val = row.get(key)
        if isinstance(val, dict):
            parts.append(val.get("content", ""))
    return "\n".join(parts)


async def load_dataset(db: AsyncSession, dataset_id: str, user_id: str) -> tuple[Dataset, list[DatasetSample]]:
    ds = (
        await db.execute(
            select(Dataset).where(Dataset.id == dataset_id, Dataset.created_by == user_id)
        )
    ).scalar_one_or_none()
    if not ds:
        raise FineTuneError("数据集不存在或无权限")
    if ds.type not in _ADAPTERS:
        raise FineTuneError(f"数据集类型 {ds.type} 不支持微调，仅支持 sft / dpo")
    samples = (
        await db.execute(
            select(DatasetSample).where(DatasetSample.dataset_id == dataset_id).order_by(DatasetSample.created_at)
        )
    ).scalars().all()
    return ds, list(samples)


def build_rows(dataset_type: str, samples: list[DatasetSample]) -> list[dict]:
    adapter = _ADAPTERS.get(dataset_type)
    if not adapter:
        raise FineTuneError(f"不支持的数据集类型: {dataset_type}")
    rows = []
    for s in samples:
        row = adapter(s)
        if row:
            rows.append(row)
    return rows


def validate_rows(rows: list[dict], max_length: int = 8192) -> dict:
    """校验并去重，返回统计与样例错误（最多 10 条）。"""
    valid: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []
    total_tokens = 0
    duplicated = 0

    for idx, row in enumerate(rows, start=1):
        msgs = row.get("messages")
        if not isinstance(msgs, list) or not msgs:
            errors.append(f"第 {idx} 条：messages 为空")
            continue
        if not all(isinstance(m, dict) and m.get("role") and m.get("content") for m in msgs):
            errors.append(f"第 {idx} 条：messages 存在空 role/content")
            continue
        if not any(m.get("role") == "assistant" for m in msgs):
            errors.append(f"第 {idx} 条：缺少 assistant 回复")
            continue

        norm = json.dumps(row, ensure_ascii=False, sort_keys=True)
        digest = hashlib.md5(norm.encode("utf-8")).hexdigest()
        if digest in seen:
            duplicated += 1
            continue
        seen.add(digest)

        tokens = estimate_tokens(_row_text(row))
        if tokens > max_length:
            errors.append(f"第 {idx} 条：约 {tokens} token，超过 max_length={max_length}")
            continue

        valid.append(row)
        total_tokens += tokens

    return {
        "total": len(rows),
        "valid": len(valid),
        "dropped": len(rows) - len(valid),
        "duplicated": duplicated,
        "avg_tokens": round(total_tokens / len(valid), 1) if valid else 0,
        "max_tokens": max_length,
        "estimated_training_tokens": total_tokens,
        "errors": errors[:10],
        "rows": valid,
    }


def export_jsonl(rows: list[dict], out_path: str) -> str:
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info("[微调] 已导出 {} 条 → {}", len(rows), out_path)
    return out_path


def default_export_path(dataset_id: str) -> str:
    root = get_settings().FINETUNE_DIR
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(root, f"{dataset_id}_{stamp}.jsonl")


# ============ 远端：DashScope REST ============

def _api_key() -> str:
    key = get_settings().QWEN_API_KEY
    if not key:
        raise FineTuneError("未配置 QWEN_API_KEY，无法调用百炼微调接口（可在 .env 中设置）")
    return key


def _url(path: str) -> str:
    return get_settings().QWEN_FINETUNE_BASE_URL.rstrip("/") + path


def _headers() -> dict:
    return {"Authorization": f"Bearer {_api_key()}"}


def _raise_for_remote(resp: httpx.Response, action: str) -> dict:
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        msg = body.get("message") or body.get("code") or body.get("raw") or resp.text[:300]
        raise FineTuneError(f"{action}失败（HTTP {resp.status_code}）：{msg}")
    return body


async def upload_file(path: str, description: str = "hengce fine-tune dataset") -> str:
    """上传 jsonl 训练文件，返回 file_id。"""
    if not os.path.isfile(path):
        raise FineTuneError(f"文件不存在: {path}")
    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        with open(path, "rb") as f:
            resp = await client.post(
                _url("/files"),
                headers=_headers(),
                files={"files": (os.path.basename(path), f, "application/jsonl")},
                data={"purpose": "fine-tune", "descriptions": description},
            )
    body = _raise_for_remote(resp, "上传训练文件")
    uploaded = (body.get("data") or {}).get("uploaded_files") or []
    if not uploaded:
        raise FineTuneError(f"上传未返回 file_id: {body}")
    file_id = uploaded[0].get("file_id")
    logger.info("[微调] 上传完成 file_id={}", file_id)
    return file_id


async def create_remote_job(
    base_model: str,
    file_id: str,
    training_type: str = "efficient_sft",
    hyper_parameters: Optional[dict] = None,
    job_name: Optional[str] = None,
    model_name: Optional[str] = None,
) -> dict:
    hyper = dict(hyper_parameters or {})
    # 平台要求三项必填，缺省给安全默认值
    hyper.setdefault("n_epochs", 3)
    hyper.setdefault("batch_size", 16)
    hyper.setdefault("max_length", 8192)

    payload: dict[str, Any] = {
        "model": base_model,
        "training_type": training_type,
        "training_datasets": [{"data_source_type": "file_id", "file_id": file_id}],
        "hyper_parameters": hyper,
    }
    if job_name:
        payload["job_name"] = job_name
    if model_name:
        payload["model_name"] = model_name

    async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT) as client:
        resp = await client.post(_url("/fine-tunes"), headers=_headers(), json=payload)
    body = _raise_for_remote(resp, "创建微调任务")
    logger.info("[微调] 任务已创建: {}", body.get("data", {}).get("job_id"))
    return body.get("data") or body


async def get_remote_job(job_id: str) -> dict:
    async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT) as client:
        resp = await client.get(_url(f"/fine-tunes/{job_id}"), headers=_headers())
    body = _raise_for_remote(resp, "查询微调任务")
    return body.get("data") or body


async def list_remote_jobs(page: int = 1, page_size: int = 20) -> list[dict]:
    async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT) as client:
        resp = await client.get(
            _url("/fine-tunes"),
            headers=_headers(),
            params={"page_no": page, "page_size": page_size},
        )
    body = _raise_for_remote(resp, "查询微调任务列表")
    data = body.get("data") or {}
    return data.get("jobs") or data.get("list") or []


async def get_remote_logs(job_id: str) -> Any:
    async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT) as client:
        resp = await client.get(_url(f"/fine-tunes/{job_id}/logs"), headers=_headers())
    body = _raise_for_remote(resp, "查询训练日志")
    return body.get("data") or body


async def list_checkpoints(job_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT) as client:
        resp = await client.get(_url(f"/fine-tunes/{job_id}/checkpoints"), headers=_headers())
    body = _raise_for_remote(resp, "查询 Checkpoint")
    data = body.get("data") or {}
    return data.get("checkpoints") or []


async def export_checkpoint(job_id: str, checkpoint: str, model_name: str) -> dict:
    async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT) as client:
        resp = await client.get(
            _url(f"/fine-tunes/{job_id}/export/{checkpoint}"),
            headers=_headers(),
            params={"model_name": model_name},
        )
    body = _raise_for_remote(resp, "导出模型")
    return body.get("data") or body


# ============ 任务落库与激活 ============

_STATUS_MAP = {
    "PENDING": "pending",
    "QUEUED": "running",
    "RUNNING": "running",
    "SUCCEEDED": "succeeded",
    "SUCCESS": "succeeded",
    "FAILED": "failed",
    "CANCELED": "failed",
    "CANCELLED": "failed",
    "EXPORTED": "exported",
}


def normalize_status(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _STATUS_MAP.get(str(raw).upper(), str(raw).lower())


def job_to_dict(job: FineTuneJob) -> dict:
    def _load(val):
        if not val:
            return None
        try:
            return json.loads(val)
        except Exception:
            return val

    return {
        "id": job.id,
        "dataset_id": job.dataset_id,
        "base_model": job.base_model,
        "training_type": job.training_type,
        "job_name": job.job_name,
        "remote_job_id": job.remote_job_id,
        "training_file_id": job.training_file_id,
        "status": job.status,
        "model_name": job.model_name,
        "served_model_name": job.served_model_name,
        "hyper_parameters": _load(job.hyper_parameters),
        "metrics": _load(job.metrics),
        "error": job.error,
        "created_at": job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else None,
        "updated_at": job.updated_at.strftime("%Y-%m-%d %H:%M:%S") if job.updated_at else None,
    }


async def sync_job(db: AsyncSession, job: FineTuneJob) -> FineTuneJob:
    """拉取远端状态并写回；无远端 id 时原样返回。"""
    if not job.remote_job_id:
        return job
    try:
        remote = await get_remote_job(job.remote_job_id)
    except FineTuneError as e:
        job.error = str(e)
        return job

    new_status = normalize_status(remote.get("status"))
    if new_status:
        job.status = new_status
    job.metrics = json.dumps(
        {k: remote.get(k) for k in ("status", "job_id", "model", "training_type", "metrics", "output_model") if remote.get(k) is not None},
        ensure_ascii=False,
    )
    if remote.get("output_model") and not job.served_model_name:
        job.model_name = remote["output_model"]
    await db.commit()
    await db.refresh(job)
    return job


async def load_active_override() -> Optional[str]:
    """启动时恢复：最近一条已导出且带 served_model_name 的任务。"""
    async with async_session() as db:
        job = (
            await db.execute(
                select(FineTuneJob)
                .where(FineTuneJob.status == "exported", FineTuneJob.served_model_name.isnot(None))
                .order_by(FineTuneJob.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return job.served_model_name if job else None


async def restore_override_on_startup() -> Optional[str]:
    try:
        name = await load_active_override()
    except Exception as e:  # 表未建好或查询失败不应阻塞启动
        logger.debug("[微调] 启动恢复跳过: {}", e)
        return None
    if name:
        set_model_override(name)
        logger.info("[微调] 已恢复微调模型: {}", name)
    return name


def activate_model(name: str) -> None:
    if not name or not name.strip():
        raise FineTuneError("模型名不能为空")
    set_model_override(name.strip())
