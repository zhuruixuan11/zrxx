#!/usr/bin/env python
"""训练环境自检：装完依赖后跑一遍，任何一项硬性不满足都直接非 0 退出并说清原因。

    python backend/training/check_gpu_env.py
"""
from __future__ import annotations

import sys

HARD_FAIL = 0
WARN = 0


def ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def warn(msg: str) -> None:
    global WARN
    WARN += 1
    print(f"[WARN] {msg}")


def fail(msg: str, fix: str) -> None:
    global HARD_FAIL
    HARD_FAIL += 1
    print(f"[FAIL] {msg}")
    print(f"       修法：{fix}")


def main() -> int:
    print("=" * 62)
    print(" 衡策训练环境自检")
    print("=" * 62)

    # 1) 版本
    pkgs = ["torch", "transformers", "peft", "trl", "bitsandbytes", "datasets", "accelerate"]
    for name in pkgs:
        try:
            mod = __import__(name)
            ver = getattr(mod, "__version__", "?")
            print(f"  {name:<14} {ver}")
        except Exception as exc:
            fail(f"{name} 导入失败：{type(exc).__name__}: {exc}",
                 f"pip install -r backend/training/requirements-training.txt")
    print(f"  python         {sys.version.split()[0]}")

    if sys.version_info < (3, 11):
        fail(f"Python {sys.version.split()[0]} 过低（trl 要求 >=3.11）",
             "用 3.11 重建虚拟环境：python3.11 -m venv .venv")
    elif sys.version_info >= (3, 13):
        warn(f"Python {sys.version.split()[0]} 偏新，个别 wheel 可能缺失；"
             "目标机器建议 3.11 或 3.12（两个版本均已验证依赖可解析）")

    if HARD_FAIL:
        return 1

    import torch

    # 2) GPU 与 CUDA
    if not torch.cuda.is_available():
        fail("torch.cuda.is_available() = False",
             "先确认镜像自带 torch 是否本可用（nvidia-smi 能否出卡、torch.version.cuda 是否非 None）；"
             "确认装成了 CPU 版才重装：pip uninstall -y torch && "
             "pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cu126")
        return 1

    prop = torch.cuda.get_device_properties(0)
    vram = prop.total_memory / 1024**3
    print(f"  GPU            {prop.name}  {vram:.1f}GB  sm_{prop.major}{prop.minor}")
    print(f"  torch CUDA     {torch.version.cuda}")

    if vram < 7.5:
        fail(f"显存只有 {vram:.1f}GB，跑不了 Qwen3.5-4B（QLoRA 峰值 10-13GB）",
             "换 >=16GB 的卡，或改 MODEL=Qwen/Qwen3.5-0.8B")
    elif vram < 15:
        warn(f"显存 {vram:.1f}GB 偏小，Qwen3.5-4B 需 BATCH=1 MAXLEN=768，仍可能 OOM")
    else:
        ok("显存足够 Qwen3.5-4B QLoRA")

    if prop.major < 8:
        warn(f"sm_{prop.major}{prop.minor} 无 bf16 tensor core，训练会慢，必要时改 fp16")
    if not torch.cuda.is_bf16_supported():
        warn("不支持 bf16，训练脚本里的 bf16=True 需改为 fp16")

    # 3) Qwen3.5 是否被当前 transformers 认识
    try:
        from transformers.models.qwen3_5 import modeling_qwen3_5  # noqa: F401
        ok("transformers 内置 Qwen3.5（models/qwen3_5）")
    except Exception as exc:
        fail(f"transformers 不认识 Qwen3.5：{type(exc).__name__}: {exc}",
             "换 transformers==5.17.0（4.x 里没有 qwen3_5）")

    # 4) TRL / PEFT 关键接口
    try:
        from trl import DPOConfig, DPOTrainer  # noqa: F401
        ok("trl.DPOTrainer 可用")
    except Exception as exc:
        fail(f"trl 接口不可用：{type(exc).__name__}: {exc}",
             "pip install trl==1.16.0（1.17.x 要求 torch>=2.6，镜像 torch 2.5.1 装不上）")
    try:
        from peft import LoraConfig, prepare_model_for_kbit_training  # noqa: F401
        ok("peft LoRA / kbit 接口可用")
    except Exception as exc:
        fail(f"peft 接口不可用：{type(exc).__name__}: {exc}", "pip install peft==0.21.2")

    # 5) 真的跑一次 4bit 量化算子（比只看版本号可靠）
    try:
        import bitsandbytes as bnb
        from transformers import BitsAndBytesConfig

        BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                           bnb_4bit_use_double_quant=True,
                           bnb_4bit_compute_dtype=torch.bfloat16)
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        layer = bnb.nn.Linear4bit(64, 64, quant_type="nf4", compute_dtype=dtype).cuda()
        out = layer(torch.randn(2, 64, device="cuda", dtype=dtype))
        if out.shape != (2, 64):
            raise RuntimeError(f"4bit 前向输出形状异常：{tuple(out.shape)}")
        ok("NF4 4bit 量化 + 前向计算跑通")
    except Exception as exc:
        fail(f"4bit 量化算子失败：{type(exc).__name__}: {exc}",
             "多为 bitsandbytes 与 CUDA 不匹配：pip install --force-reinstall "
             "bitsandbytes==0.50.2；仍失败则改用 bf16 LoRA（去掉 load_in_4bit）")

    print("-" * 62)
    if HARD_FAIL:
        print(f"自检未通过：{HARD_FAIL} 项硬性失败，别急着开训。")
        return 1
    print(f"自检通过（{WARN} 项警告）。可以跑 run_rented_gpu.sh。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
