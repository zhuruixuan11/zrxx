#!/usr/bin/env python
"""在 SFT 适配器上做 DPO（TRL DPOTrainer）。

输入：backend/data/training/dpo/{train,val}.jsonl（prompt/chosen/rejected）
起点：SFT 产出的 LoRA 适配器目录（--sft-adapter），基座仍是同一个 Qwen3.5。

用法：
  python backend/training/train_dpo.py \
    --model Qwen/Qwen3.5-4B \
    --sft-adapter backend/outputs/qwen3_5_4b_sft \
    --data backend/data/training/dpo \
    --output backend/outputs/qwen3_5_4b_dpo
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

import torch
from datasets import load_dataset
from peft import PeftModel, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import DPOConfig, DPOTrainer


def _build(cls, **kwargs):
    """按当前 TRL 版本的实际签名过滤参数，避免版本升级直接崩。"""
    sig = inspect.signature(cls.__init__).parameters
    return cls(**{k: v for k, v in kwargs.items() if k in sig})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3.5-4B")
    ap.add_argument("--sft-adapter", required=True)
    ap.add_argument("--data", default="backend/data/training/dpo")
    ap.add_argument("--output", default="backend/outputs/qwen3_5_4b_dpo")
    ap.add_argument("--beta", type=float, default=0.1)
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--max-prompt-len", type=int, default=768)
    ap.add_argument("--no-4bit", action="store_true",
                    help="不用 4bit 量化，直接 bf16 LoRA（bitsandbytes 与 CUDA 不匹配时的兜底）")
    args = ap.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if args.no_4bit:
        print("[model] --no-4bit：跳过 4bit 量化，走 bf16 LoRA（显存约为 QLoRA 的 2–3 倍）")
        base = AutoModelForCausalLM.from_pretrained(
            args.model, dtype=torch.bfloat16, device_map={"": 0})
        base.enable_input_require_grads()
    else:
        bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                 bnb_4bit_use_double_quant=True,
                                 bnb_4bit_compute_dtype=torch.bfloat16)
        base = AutoModelForCausalLM.from_pretrained(
            args.model, quantization_config=bnb, dtype=torch.bfloat16, device_map={"": 0})
        base = prepare_model_for_kbit_training(base, use_gradient_checkpointing=True)
    model = PeftModel.from_pretrained(base, args.sft_adapter, is_trainable=True)

    ds = load_dataset("json", data_files={
        "train": f"{args.data}/train.jsonl",
        "validation": f"{args.data}/val.jsonl",
    })

    cfg = _build(
        DPOConfig,
        output_dir=args.output,
        beta=args.beta,
        learning_rate=args.lr,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.grad_accum,
        per_device_eval_batch_size=args.batch,
        max_length=args.max_len,
        max_prompt_length=args.max_prompt_len,
        bf16=True,
        gradient_checkpointing=True,
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy="epoch",
        report_to="none",
        remove_unused_columns=False,
    )
    trainer = DPOTrainer(
        model=model, ref_model=None, args=cfg,
        train_dataset=ds["train"], eval_dataset=ds["validation"],
        processing_class=tokenizer,
    )
    trainer.train()
    trainer.save_model(args.output)
    tokenizer.save_pretrained(args.output)

    peak = torch.cuda.max_memory_allocated() / 1024**3 if torch.cuda.is_available() else 0
    summary = {
        "model": args.model, "sft_adapter": args.sft_adapter, "output": args.output,
        "train_pairs": len(ds["train"]), "val_pairs": len(ds["validation"]),
        "beta": args.beta, "lr": args.lr, "epochs": args.epochs,
        "batch": args.batch, "grad_accum": args.grad_accum,
        "max_len": args.max_len, "peak_vram_gb": round(peak, 2),
    }
    (Path(args.output) / "train_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
