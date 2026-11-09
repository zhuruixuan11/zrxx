"""衡策 · 微调命令行工具（与 API 共用 services/finetune 逻辑）。

在 backend 目录下运行：
    python -m scripts.finetune export   --dataset-id ID [--out PATH]
    python -m scripts.finetune validate --dataset-id ID [--max-length 8192]
    python -m scripts.finetune upload   --file PATH
    python -m scripts.finetune train    --file-id FILE_ID [--epochs 3] --yes
    python -m scripts.finetune status   --job-id JOB_ID
    python -m scripts.finetune logs     --job-id JOB_ID
    python -m scripts.finetune list
    python -m scripts.finetune activate --model-name NAME [--job-id ID]
"""
import argparse
import asyncio
import json
import sys

from sqlalchemy import select

from app.database import async_session, init_tables
from app.models.dataset import Dataset, DatasetSample
from app.models.finetune import FineTuneJob
from app.services import finetune as ft


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


async def _load_dataset(dataset_id: str):
    async with async_session() as db:
        ds = (await db.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise SystemExit(f"数据集不存在: {dataset_id}")
        samples = (
            await db.execute(select(DatasetSample).where(DatasetSample.dataset_id == dataset_id))
        ).scalars().all()
    return ds, list(samples)


async def cmd_export(args) -> None:
    ds, samples = await _load_dataset(args.dataset_id)
    rows = ft.build_rows(ds.type, samples)
    report = ft.validate_rows(rows, args.max_length)
    if not report["valid"]:
        _dump(report)
        raise SystemExit("没有可用样本")
    out = args.out or ft.default_export_path(ds.id)
    path = ft.export_jsonl(report.pop("rows"), out)
    report["file"] = path
    _dump(report)


async def cmd_validate(args) -> None:
    ds, samples = await _load_dataset(args.dataset_id)
    rows = ft.build_rows(ds.type, samples)
    report = ft.validate_rows(rows, args.max_length)
    report.pop("rows", None)
    _dump(report)


async def cmd_upload(args) -> None:
    file_id = await ft.upload_file(args.file)
    _dump({"file": args.file, "file_id": file_id})


async def cmd_train(args) -> None:
    if not args.yes:
        raise SystemExit("训练按 token 计费，确认后请加 --yes 再提交")
    hyper = {
        "n_epochs": args.epochs,
        "batch_size": args.batch_size,
        "max_length": args.max_length,
    }
    data = await ft.create_remote_job(
        base_model=args.base_model,
        file_id=args.file_id,
        training_type=args.training_type,
        hyper_parameters=hyper,
        job_name=args.job_name,
        model_name=args.model_name,
    )
    _dump(data)


async def cmd_status(args) -> None:
    _dump(await ft.get_remote_job(args.job_id))


async def cmd_logs(args) -> None:
    _dump(await ft.get_remote_logs(args.job_id))


async def cmd_list(args) -> None:
    _dump(await ft.list_remote_jobs())


async def cmd_activate(args) -> None:
    from app.llm.client import set_model_override

    set_model_override(args.model_name)
    if args.job_id:
        async with async_session() as db:
            job = (
                await db.execute(select(FineTuneJob).where(FineTuneJob.id == args.job_id))
            ).scalar_one_or_none()
            if not job:
                raise SystemExit(f"任务不存在: {args.job_id}")
            job.served_model_name = args.model_name
            job.status = "exported"
            await db.commit()
        print(f"已写入任务 {args.job_id} 并设为导出状态（重启服务后自动恢复）")
    else:
        print("仅对当前进程生效；若要让服务端长期使用，请传 --job-id 或调用 POST /api/finetune/jobs/{id}/activate")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scripts.finetune", description="衡策微调工具")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("export", help="导出 jsonl 并给出校验统计")
    e.add_argument("--dataset-id", required=True)
    e.add_argument("--out", default=None)
    e.add_argument("--max-length", type=int, default=8192)
    e.set_defaults(func=cmd_export)

    v = sub.add_parser("validate", help="只校验不写盘")
    v.add_argument("--dataset-id", required=True)
    v.add_argument("--max-length", type=int, default=8192)
    v.set_defaults(func=cmd_validate)

    u = sub.add_parser("upload", help="上传 jsonl 到百炼")
    u.add_argument("--file", required=True)
    u.set_defaults(func=cmd_upload)

    t = sub.add_parser("train", help="创建训练任务（需 --yes）")
    t.add_argument("--file-id", required=True)
    t.add_argument("--base-model", default="qwen3-8b")
    t.add_argument("--training-type", default="efficient_sft")
    t.add_argument("--epochs", type=int, default=3)
    t.add_argument("--batch-size", type=int, default=16)
    t.add_argument("--max-length", type=int, default=8192)
    t.add_argument("--job-name", default=None)
    t.add_argument("--model-name", default=None)
    t.add_argument("--yes", action="store_true")
    t.set_defaults(func=cmd_train)

    s = sub.add_parser("status", help="查询远端任务状态")
    s.add_argument("--job-id", required=True)
    s.set_defaults(func=cmd_status)

    l = sub.add_parser("logs", help="查询训练日志")
    l.add_argument("--job-id", required=True)
    l.set_defaults(func=cmd_logs)

    ls = sub.add_parser("list", help="列出远端任务")
    ls.set_defaults(func=cmd_list)

    a = sub.add_parser("activate", help="切换运行时模型")
    a.add_argument("--model-name", required=True)
    a.add_argument("--job-id", default=None)
    a.set_defaults(func=cmd_activate)
    return p


async def _main() -> None:
    args = build_parser().parse_args()
    await init_tables()
    try:
        await args.func(args)
    except ft.FineTuneError as e:
        print(f"错误: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
