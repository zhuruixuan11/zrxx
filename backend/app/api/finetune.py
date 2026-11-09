"""微调 API：数据集导出、训练任务管理、模型激活。"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import get_settings
from ..database import async_session
from ..llm.client import get_model_override
from ..models.dataset import Dataset
from ..models.finetune import FineTuneJob
from ..models.user import User
from ..security import get_current_user
from ..services import finetune as ft

router = APIRouter(prefix="/api/finetune", tags=["finetune"])


# ============ Schema ============
class ExportReq(BaseModel):
    dataset_id: str
    max_length: int = 8192


class JobCreate(BaseModel):
    dataset_id: str
    base_model: str = "qwen3-8b"
    training_type: str = "efficient_sft"
    job_name: Optional[str] = None
    model_name: Optional[str] = None
    hyper_parameters: dict = Field(default_factory=dict)
    max_length: int = 8192
    upload: bool = False  # False 时只落本地 pending 记录，便于无 Key 跑通


class ActivateReq(BaseModel):
    served_model_name: str


def _ok(data=None):
    return {"code": 0, "msg": "ok", "data": data}


# ============ 数据集 ============
@router.get("/datasets")
async def list_candidates(user: User = Depends(get_current_user)):
    """列出可用于微调的数据集（本人创建的 sft / dpo）。"""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Dataset)
                .where(Dataset.created_by == user.id, Dataset.type.in_(["sft", "dpo"]))
                .order_by(Dataset.created_at.desc())
            )
        ).scalars().all()
    return _ok([
        {
            "id": d.id,
            "name": d.name,
            "type": d.type,
            "sample_count": d.sample_count,
            "is_published": d.is_published,
        }
        for d in rows
    ])


@router.post("/export")
async def export_dataset(req: ExportReq, user: User = Depends(get_current_user)):
    """导出 jsonl 并返回校验统计，不上传、不创建远端任务。"""
    try:
        async with async_session() as db:
            ds, samples = await ft.load_dataset(db, req.dataset_id, user.id)
        rows = ft.build_rows(ds.type, samples)
        report = ft.validate_rows(rows, req.max_length)
        if not report["valid"]:
            raise ft.FineTuneError(f"没有可用样本：{report['errors'][:3]}")
        path = ft.export_jsonl(report.pop("rows"), ft.default_export_path(ds.id))
    except ft.FineTuneError as e:
        raise HTTPException(status_code=400, detail=str(e))
    report.update({"file": path, "dataset_id": ds.id, "dataset_type": ds.type})
    return _ok(report)


# ============ 任务 ============
@router.post("/jobs")
async def create_job(req: JobCreate, user: User = Depends(get_current_user)):
    """导出 →（可选）上传并创建远端训练任务。"""
    try:
        async with async_session() as db:
            ds, samples = await ft.load_dataset(db, req.dataset_id, user.id)
            rows = ft.build_rows(ds.type, samples)
            report = ft.validate_rows(rows, req.max_length)
            if not report["valid"]:
                raise ft.FineTuneError(f"没有可用样本：{report['errors'][:3]}")
            path = ft.export_jsonl(report.pop("rows"), ft.default_export_path(ds.id))

            job = FineTuneJob(
                user_id=user.id,
                dataset_id=ds.id,
                base_model=req.base_model,
                training_type=req.training_type,
                job_name=req.job_name,
                status="pending",
                hyper_parameters=json.dumps(req.hyper_parameters, ensure_ascii=False),
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)

            if req.upload:
                try:
                    job.status = "uploading"
                    await db.commit()
                    job.training_file_id = await ft.upload_file(path, f"hengce {ds.name}")
                    remote = await ft.create_remote_job(
                        base_model=req.base_model,
                        file_id=job.training_file_id,
                        training_type=req.training_type,
                        hyper_parameters=req.hyper_parameters,
                        job_name=req.job_name,
                        model_name=req.model_name,
                    )
                    job.remote_job_id = remote.get("job_id")
                    job.status = ft.normalize_status(remote.get("status")) or "running"
                    await db.commit()
                except ft.FineTuneError as e:
                    job.status = "failed"
                    job.error = str(e)
                    await db.commit()
                    raise
            await db.refresh(job)
            data = ft.job_to_dict(job)
    except ft.FineTuneError as e:
        raise HTTPException(status_code=400, detail=str(e))

    data["export"] = report
    data["file"] = path
    return _ok(data)


@router.get("/jobs")
async def list_jobs(user: User = Depends(get_current_user)):
    async with async_session() as db:
        jobs = (
            await db.execute(
                select(FineTuneJob).where(FineTuneJob.user_id == user.id).order_by(FineTuneJob.created_at.desc())
            )
        ).scalars().all()
    return _ok([ft.job_to_dict(j) for j in jobs])


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, sync: bool = True, user: User = Depends(get_current_user)):
    async with async_session() as db:
        job = (
            await db.execute(
                select(FineTuneJob).where(FineTuneJob.id == job_id, FineTuneJob.user_id == user.id)
            )
        ).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        if sync and job.remote_job_id:
            try:
                job = await ft.sync_job(db, job)
            except ft.FineTuneError as e:
                job.error = str(e)
    return _ok(ft.job_to_dict(job))


@router.get("/jobs/{job_id}/logs")
async def get_job_logs(job_id: str, user: User = Depends(get_current_user)):
    async with async_session() as db:
        job = (
            await db.execute(
                select(FineTuneJob).where(FineTuneJob.id == job_id, FineTuneJob.user_id == user.id)
            )
        ).scalar_one_or_none()
    if not job or not job.remote_job_id:
        raise HTTPException(status_code=404, detail="任务不存在或尚未提交到远端")
    try:
        logs = await ft.get_remote_logs(job.remote_job_id)
    except ft.FineTuneError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _ok(logs)


@router.post("/jobs/{job_id}/activate")
async def activate(job_id: str, req: ActivateReq, user: User = Depends(get_current_user)):
    async with async_session() as db:
        job = (
            await db.execute(
                select(FineTuneJob).where(FineTuneJob.id == job_id, FineTuneJob.user_id == user.id)
            )
        ).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        try:
            ft.activate_model(req.served_model_name)
        except ft.FineTuneError as e:
            raise HTTPException(status_code=400, detail=str(e))
        job.served_model_name = req.served_model_name
        job.status = "exported"
        await db.commit()
        await db.refresh(job)
    return _ok(ft.job_to_dict(job))


@router.get("/active")
async def active_model():
    """当前实际生效的模型（微调模型会覆盖主 Provider 路由）。"""
    s = get_settings()
    override = get_model_override()
    if override:
        return _ok({"provider": "qwen(微调)", "model": override, "source": "runtime_override"})
    if s.QWEN_FINETUNED_MODEL:
        return _ok({"provider": "qwen(微调)", "model": s.QWEN_FINETUNED_MODEL, "source": "config"})
    default_model = {
        "deepseek": s.DEEPSEEK_MODEL,
        "openai": s.OPENAI_MODEL,
        "qwen": s.QWEN_MODEL,
    }.get(s.LLM_PROVIDER, "mock")
    return _ok({"provider": s.LLM_PROVIDER, "model": default_model, "source": "default"})
