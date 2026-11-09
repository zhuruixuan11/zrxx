#!/usr/bin/env bash
# 租到 GPU 后第一步：先探测镜像自带环境，再只补装缺的包。
# 核心原则：镜像已有可用 torch 时**绝不重装 torch**，避免覆盖镜像的 CUDA 版本。
#
#   bash backend/training/setup_gpu_env.sh
#   PY=python3.11 bash backend/training/setup_gpu_env.sh      # 指定解释器
#   CUDA_TAG=cu124 bash backend/training/setup_gpu_env.sh     # 仅在镜像无可用 torch 时生效
#   FORCE_TORCH=1 bash ...                                    # 强制按 CUDA_TAG 重装 torch（一般不要用）
#
# 装完的虚拟环境在 backend/training/.venv：
#   镜像自带 torch → 用 --system-site-packages 建，继承镜像的 torch/CUDA
#   无可用 torch   → 普通 venv，从 PyTorch 官方索引装
set -euo pipefail
cd "$(dirname "$0")/../.."                       # 仓库根

PY="${PY:-python3}"
VENV="${VENV:-backend/training/.venv}"
REQ="backend/training/requirements-training.txt"
CUDA_TAG="${CUDA_TAG:-cu126}"
FORCE_TORCH="${FORCE_TORCH:-0}"

echo "== 0/5 检查 Python"
"$PY" - <<'PY'
import sys
v = sys.version_info
print(f"python {v.major}.{v.minor}.{v.micro}")
if v < (3, 11):
    sys.exit("需要 Python >= 3.11（trl 1.16/1.17 的硬要求），请用 PY=/path/to/python3.11 重跑本脚本")
PY

echo "== 1/5 探测镜像自带 torch（能否直接复用）"
HAS_CUDA_TORCH=0
if [ "$FORCE_TORCH" = "1" ]; then
  echo "FORCE_TORCH=1，跳过复用"
elif "$PY" - <<'PY'
import sys
try:
    import torch
except Exception as exc:                      # 没装 / 装坏
    print(f"未检测到可用 torch：{exc}")
    sys.exit(1)
print(f"torch {torch.__version__}  cuda_build={torch.version.cuda}  "
      f"available={torch.cuda.is_available()}  device={torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-'}")
sys.exit(0 if torch.cuda.is_available() else 1)
PY
then
  HAS_CUDA_TORCH=1
fi

if [ "$HAS_CUDA_TORCH" = "1" ]; then
  echo "== 2/5 复用镜像 torch：venv 加 --system-site-packages，不重装 torch"
  if [ ! -d "$VENV" ]; then
    "$PY" -m venv --system-site-packages "$VENV"
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  python -m pip install -q --upgrade pip
  echo "== 3/5 只补装 transformers/peft/trl/datasets/accelerate/bitsandbytes（requirements 不含 torch）"
  pip install -q -r "$REQ"
else
  echo "== 2/5 镜像无可用 torch：从 ${CUDA_TAG} 索引安装（PyPI 默认源是 CPU 版）"
  if [ ! -d "$VENV" ]; then
    "$PY" -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  python -m pip install -q --upgrade pip
  pip install -q "torch==2.14.1" --index-url "https://download.pytorch.org/whl/${CUDA_TAG}"
  echo "== 3/5 安装其余依赖"
  pip install -q -r "$REQ"
fi

echo "== 4/5 版本与 CUDA 兼容检查（不一致就非 0 退出，不静默通过）"
python - <<'PY'
import importlib, sys
import torch

EXPECT = {"transformers": "5.17.0", "peft": "0.21.2", "trl": "1.16.0",
          "bitsandbytes": "0.50.2", "datasets": "5.1.0", "accelerate": "1.15.0"}
bad = []
for name, want in EXPECT.items():
    try:
        got = importlib.import_module(name).__version__
    except Exception as exc:
        print(f"{name:14s} 导入失败：{exc}")
        bad.append(f"{name}=={want}")
        continue
    if got == want:
        print(f"{name:14s} {got}  ok")
    else:
        print(f"{name:14s} {got}  ✗ 期望 {want}")
        bad.append(f"{name}=={want}")

print(f"torch          {torch.__version__}  cuda_build={torch.version.cuda}  "
      f"cuda_available={torch.cuda.is_available()}")
if torch.version.cuda is None:
    print("✗ 当前是 CPU 版 torch：镜像无可用 torch 时才会这样，说明装到了 PyPI 默认源")
    bad.append("torch(cu126)")
if not torch.cuda.is_available():
    print("✗ torch.cuda.is_available() 为 False：驱动与 CUDA build 不匹配，或没挂到 GPU")
    bad.append("cuda-unavailable")

if bad:
    sys.exit("自检失败：" + "; ".join(bad)
             + "\n修：pip install --force-reinstall " + " ".join(bad)
             + "\n若 torch 是 CPU 版，见 README「镜像自带 torch 时怎么装」")
PY

echo "== 5/5 4bit 量化自检（NF4 双重量化 + 一次真实前向）"
python backend/training/check_gpu_env.py
