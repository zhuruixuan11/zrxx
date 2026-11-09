#!/usr/bin/env python
"""Qwen3.5 QLoRA SFT（不依赖 TRL，hand-rolled Trainer）。

Qwen3.5 是 GatedDeltaNet（线性注意力）+ 全注意力混合结构，target_modules **不能照抄 Qwen3**：
  全注意力层：q_proj / k_proj / v_proj / o_proj
  线性注意力层（DeltaNet）：in_proj_qkv / in_proj_z / in_proj_b / in_proj_a / out_proj
  MLP：gate_proj / up_proj / down_proj
  视觉塔（qkv / proj / linear_fc1 / linear_fc2）不训，conv1d 默认不加 LoRA。

超参与数据路径默认读 backend/training/train_config.yaml，命令行参数优先。
配置缺失、PyYAML 未安装或 YAML 解析失败时回退到内置默认值并打印警告，不会因此崩溃。

用法（在租到的 GPU 机器上）：
  python backend/training/prepare_data.py --mix
  python backend/training/train_sft.py                    # 全部取配置默认值
  python backend/training/train_sft.py --epochs 2 --lr 1e-4 --output backend/outputs/xxx   # 覆盖
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",
    "in_proj_qkv", "in_proj_z", "in_proj_b", "in_proj_a", "out_proj",
    "gate_proj", "up_proj", "down_proj",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(__file__).resolve().parent / "train_config.yaml"

# 配置读不到时的内置默认值（与 train_config.yaml 保持一致）
CONFIG_DEFAULTS: dict = {
    "seed": 42,
    "model": {"name": "Qwen/Qwen3.5-4B"},
    "paths": {
        "mixed_train": "backend/data/training/mixed/train.jsonl",
        "core_val": "backend/data/training/core/val.jsonl",
        "output_dir": "backend/outputs/qwen3_5_4b_sft_mixed",
    },
    "train": {
        "epochs": 8,
        "per_device_train_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "learning_rate": 0.0001,
        "lr_scheduler_type": "cosine",
        "warmup_ratio": 0.05,
        "lora_rank": 16,
        "lora_alpha": 32,
        "lora_dropout": 0.05,
        "max_len": 1024,
        "max_grad_norm": 1.0,
        "eval_strategy": "epoch",
        "save_strategy": "epoch",
        "logging_steps": 10,
        "load_best_model_at_end": True,
        "metric_for_best_model": "eval_loss",
        "greater_is_better": False,
        "early_stopping_patience": 3,
        "early_stopping_threshold": 0.0,
        "save_total_limit": 8,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(CONFIG_DEFAULTS))
    if not path.exists():
        print(f"[警告] 找不到配置文件 {path}，使用内置默认值")
        return cfg
    try:
        import yaml
    except ImportError:
        print("[警告] 未安装 PyYAML，使用内置默认值（pip install pyyaml 可读配置）")
        return cfg
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # YAML 语法错误不应该让训练脚本直接崩溃
        print(f"[警告] 配置解析失败（{exc}），使用内置默认值")
        return cfg
    return _deep_merge(cfg, loaded)


def resolve(path_str: str) -> Path:
    path = Path(path_str)
    return path if path.is_absolute() else REPO_ROOT / path


def build_dataset(tokenizer, train_file: Path, val_file: Path, max_len: int):
    from datasets import load_dataset

    ds = load_dataset("json", data_files={"train": str(train_file),
                                          "validation": str(val_file)})

    def _ids(enc):
        """transformers 5 的 apply_chat_template(tokenize=True) 可能返回 Encoding，
        必须显式取成 list[int]，否则 datasets/Arrow 会拿 Encoding 当下标写盘并溢出。"""
        if hasattr(enc, "keys"):
            return [int(x) for x in enc["input_ids"]]
        if hasattr(enc, "ids"):
            return [int(x) for x in enc.ids]
        return [int(x) for x in enc]

    def encode(ex):
        msgs = ex["messages"]
        ids = _ids(tokenizer.apply_chat_template(msgs, tokenize=True, add_generation_prompt=False))
        prefix = _ids(tokenizer.apply_chat_template(msgs[:-1], tokenize=True, add_generation_prompt=True))
        labels = list(ids)
        if ids[: len(prefix)] == prefix:
            labels[: len(prefix)] = [-100] * len(prefix)
        ids, labels = ids[:max_len], labels[:max_len]
        return {"input_ids": ids, "labels": labels, "attention_mask": [1] * len(ids)}

    return ds.map(encode, remove_columns=ds["train"].column_names,
                  desc="tokenize")


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen3.5 QLoRA SFT（默认读 train_config.yaml）")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="YAML 配置文件")
    ap.add_argument("--model", default=None, help="基座模型（默认取配置）")
    ap.add_argument("--data", default=None,
                    help="兼容旧用法：包含 train.jsonl / val.jsonl 的目录，给了会覆盖配置里的两个文件")
    ap.add_argument("--train-file", default=None, help="训练集 jsonl（默认 mixed/train.jsonl）")
    ap.add_argument("--val-file", default=None, help="验证集 jsonl（默认 core/val.jsonl）")
    ap.add_argument("--output", default=None, help="输出目录（默认取配置 paths.output_dir）")
    ap.add_argument("--max-len", type=int, default=None)
    ap.add_argument("--epochs", type=float, default=None)
    ap.add_argument("--lr", type=float, default=None)
    ap.add_argument("--batch", type=int, default=None)
    ap.add_argument("--grad-accum", type=int, default=None)
    ap.add_argument("--rank", type=int, default=None)
    ap.add_argument("--alpha", type=int, default=None)
    ap.add_argument("--dropout", type=float, default=None)
    ap.add_argument("--max-steps", type=int, default=0,
                    help="冒烟测试用：>0 时只跑这么多步，并自动关掉中途评估与存档")
    ap.add_argument("--resume-from-checkpoint", default=None,
                    help="断点续训：传 checkpoint-* 目录（run_rented_gpu.sh 的 RESUME=auto 会自动找最大的）")
    ap.add_argument("--no-4bit", action="store_true",
                    help="不用 4bit 量化，直接 bf16 LoRA（bitsandbytes 与 CUDA 不匹配时的兜底）")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    train_cfg = cfg.get("train", {})
    paths = cfg.get("paths", {})

    def pick(cli_value, key, default):
        return cli_value if cli_value is not None else train_cfg.get(key, default)

    epochs = pick(args.epochs, "epochs", 8)
    lr = pick(args.lr, "learning_rate", 1e-4)
    batch = pick(args.batch, "per_device_train_batch_size", 2)
    grad_accum = pick(args.grad_accum, "gradient_accumulation_steps", 8)
    rank = pick(args.rank, "lora_rank", 16)
    alpha = pick(args.alpha, "lora_alpha", 32)
    dropout = pick(args.dropout, "lora_dropout", 0.05)
    max_len = pick(args.max_len, "max_len", 1024)
    warmup_ratio = train_cfg.get("warmup_ratio", 0.05)
    scheduler = train_cfg.get("lr_scheduler_type", "cosine")
    eval_strategy = train_cfg.get("eval_strategy", "epoch")
    save_strategy = train_cfg.get("save_strategy", "epoch")
    logging_steps = train_cfg.get("logging_steps", 10)
    max_grad_norm = float(train_cfg.get("max_grad_norm", 1.0))
    # early stopping / 选 best checkpoint（无 GPU 时只做代码层验证）
    load_best = bool(train_cfg.get("load_best_model_at_end", True))
    metric_for_best = train_cfg.get("metric_for_best_model", "eval_loss")
    greater_is_better = bool(train_cfg.get("greater_is_better", False))
    patience = int(train_cfg.get("early_stopping_patience", 3))
    es_threshold = float(train_cfg.get("early_stopping_threshold", 0.0))
    save_total_limit = int(train_cfg.get("save_total_limit", 8))
    seed = int(cfg.get("seed", 42))
    model_name = args.model or (cfg.get("model") or {}).get("name") or "Qwen/Qwen3.5-4B"

    if args.data:
        train_file = Path(args.data) / "train.jsonl"
        val_file = Path(args.data) / "val.jsonl"
    else:
        train_file = resolve(args.train_file or paths["mixed_train"])
        val_file = resolve(args.val_file or paths["core_val"])
    output = args.output or paths["output_dir"]

    resume_from = args.resume_from_checkpoint or None
    if resume_from is not None and not Path(resume_from).is_dir():
        print(f"[错误] --resume-from-checkpoint 指向的目录不存在：{resume_from}")
        return 2

    print(json.dumps({
        "config": str(args.config), "model": model_name,
        "train_file": str(train_file), "val_file": str(val_file), "output": output,
        "epochs": epochs, "batch": batch, "grad_accum": grad_accum,
        "learning_rate": lr, "rank": rank, "alpha": alpha, "dropout": dropout,
        "max_len": max_len, "warmup_ratio": warmup_ratio, "seed": seed,
        "max_grad_norm": max_grad_norm, "early_stopping_patience": patience,
        "load_best_model_at_end": load_best,
        "resume_from_checkpoint": resume_from,
    }, ensure_ascii=False, indent=2))

    # 重依赖放到参数解析之后，保证本机没装 datasets/peft 时 `--help` 和 import 仍然可用
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
                              DataCollatorForSeq2Seq, EarlyStoppingCallback, Trainer,
                              TrainingArguments)

    torch.manual_seed(seed)
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if args.no_4bit:
        print("[model] --no-4bit：跳过 4bit 量化，走 bf16 LoRA（显存约为 QLoRA 的 2–3 倍）")
        model = AutoModelForCausalLM.from_pretrained(
            model_name, dtype=torch.bfloat16, device_map={"": 0})
        model.enable_input_require_grads()
    else:
        bnb = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_name, quantization_config=bnb, dtype=torch.bfloat16, device_map={"": 0})
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(
        r=rank, lora_alpha=alpha, lora_dropout=dropout,
        bias="none", task_type="CAUSAL_LM", target_modules=TARGET_MODULES))
    model.print_trainable_parameters()

    ds = build_dataset(tokenizer, train_file, val_file, max_len)
    collator = DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100)

    effective_batch = batch * grad_accum
    steps_per_epoch = math.ceil(len(ds["train"]) / effective_batch)
    total_steps = math.ceil(steps_per_epoch * float(epochs))
    # transformers 5.x 的 TrainingArguments 已移除 warmup_ratio，改由步数表达；
    # 仍从 yaml 读比例，这里换算成步数（240 × 5% ≈ 12 步）。
    warmup_steps = max(1, round(total_steps * float(warmup_ratio)))
    print(f"[steps] 训练行 {len(ds['train'])}，有效批 {batch}×{grad_accum}={effective_batch}，"
          f"steps/epoch = ceil({len(ds['train'])}/{effective_batch}) = {steps_per_epoch}；"
          f"{epochs} epoch = {total_steps} 步（单卡口径）")
    print(f"[steps] 注意：混合集 471 行里唯一内容只有 199 条（精写 68 + 辅助 131），"
          f"训练行数是 exposure，不是样本数")
    print(f"[early-stop] eval/save 每 epoch，patience={patience}，"
          f"metric={metric_for_best}（greater_is_better={greater_is_better}），"
          f"load_best_model_at_end={load_best}，save_total_limit={save_total_limit}；"
          f"warmup={warmup_steps} 步（{warmup_ratio}×{total_steps}）")

    smoke = bool(args.max_steps and args.max_steps > 0)
    if smoke:
        eval_strategy, save_strategy, load_best = "no", "no", False
        print(f"[smoke] max_steps={args.max_steps}：关闭中途 eval/save 与 best 加载")

    targs = TrainingArguments(
        output_dir=output,
        num_train_epochs=epochs,
        max_steps=(args.max_steps if args.max_steps and args.max_steps > 0 else -1),
        per_device_train_batch_size=batch,
        per_device_eval_batch_size=batch,
        gradient_accumulation_steps=grad_accum,
        learning_rate=lr,
        lr_scheduler_type=scheduler,
        warmup_steps=warmup_steps,
        logging_steps=logging_steps,
        save_strategy=save_strategy,
        eval_strategy=eval_strategy,
        load_best_model_at_end=load_best,
        metric_for_best_model=metric_for_best,
        greater_is_better=greater_is_better,
        save_total_limit=save_total_limit,
        bf16=True,
        optim="adamw_torch" if args.no_4bit else "paged_adamw_8bit",
        gradient_checkpointing=True,
        max_grad_norm=max_grad_norm,
        report_to="none",
        seed=seed,
        remove_unused_columns=False,
    )
    callbacks = []
    if patience > 0 and load_best:
        callbacks.append(EarlyStoppingCallback(
            early_stopping_patience=patience,
            early_stopping_threshold=es_threshold))
    elif patience > 0:
        print("[警告] load_best_model_at_end=False，EarlyStoppingCallback 需要它，已跳过早停")
    trainer = Trainer(model=model, args=targs, train_dataset=ds["train"],
                      eval_dataset=ds["validation"], data_collator=collator,
                      callbacks=callbacks)

    peak_before = torch.cuda.max_memory_allocated() / 1024**3 if torch.cuda.is_available() else 0
    if resume_from:
        print(f"[resume] 从 {resume_from} 续训（checkpoint 只在每个 epoch 边界保存，最多回退一个 epoch）")
    trainer.train(resume_from_checkpoint=resume_from)
    state = trainer.state
    stopped_early = bool(state.global_step < state.max_steps)
    best_checkpoint = getattr(state, "best_model_checkpoint", None)
    best_eval_loss = getattr(state, "best_metric", None)
    if best_eval_loss is not None:
        best_eval_loss = float(best_eval_loss)
    print(f"[early-stop] stopped_early={stopped_early}（global_step {state.global_step}/{state.max_steps}），"
          f"best_eval_loss={best_eval_loss}，best_checkpoint={best_checkpoint}")
    peak = torch.cuda.max_memory_allocated() / 1024**3 if torch.cuda.is_available() else 0
    trainer.save_model(output)  # load_best_model_at_end=True 时这里写的是 best 权重
    tokenizer.save_pretrained(output)

    summary = {
        "config": str(args.config),
        "model": model_name,
        "output": output,
        "train_file": str(train_file),
        "val_file": str(val_file),
        "train_rows": len(ds["train"]),
        "val_rows": len(ds["validation"]),
        "rank": rank, "alpha": alpha, "lr": lr,
        "epochs": epochs, "batch": batch, "grad_accum": grad_accum,
        "effective_batch": effective_batch,
        "steps_per_epoch": steps_per_epoch,
        "total_steps": total_steps,
        "warmup_ratio": warmup_ratio,
        "warmup_steps": warmup_steps,
        "max_len": max_len,
        "max_grad_norm": max_grad_norm,
        "seed": seed,
        "early_stopping_patience": patience,
        "early_stopping_threshold": es_threshold,
        "metric_for_best_model": metric_for_best,
        "greater_is_better": greater_is_better,
        "best_checkpoint": best_checkpoint,
        "best_eval_loss": best_eval_loss,
        "stopped_early": stopped_early,
        "global_step": int(state.global_step),
        "peak_vram_gb": round(peak, 2),
        "peak_vram_before_train_gb": round(peak_before, 2),
        "resume_from_checkpoint": resume_from,
    }
    Path(output).mkdir(parents=True, exist_ok=True)
    (Path(output) / "train_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
