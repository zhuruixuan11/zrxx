#!/usr/bin/env bash
# 租到 GPU 后一条命令跑完：preflight → 建环境 → prepare_data --mix → SFT → DPO → 88 条 OOD 三档对比
#
#   cd /root/autodl-tmp/wealthai && bash backend/training/run_rented_gpu.sh
#
# 可覆盖的环境变量：
#   MODEL=Qwen/Qwen3.5-4B   基座
#   OUT=backend/outputs     产物目录（结果文件 + 每步日志都写这里）
#   BATCH=2 ACCUM=8         有效批 16；显存不够先降 BATCH、再抬 ACCUM
#   MAXLEN=1024             序列长度；24G 卡可试 1536
#   RESUME=auto|0|<路径>    断点续训：auto=自动找 $OUT/qwen3_5_4b_sft/checkpoint-* 里 step 最大的；
#                           0=从头训；<路径>=指定 checkpoint 目录
#   DRYRUN=1                只跑 preflight + 数据准备，不训练（本机验证脚本骨架用）
#   HF_ENDPOINT=https://hf-mirror.com   国内拉模型
#
# 每步日志：$OUT/logs/{00_setup,00_gpu_check,01_prepare_data,02_sft,03_dpo,04_eval}.log
# 任一步失败会打印该日志路径与最后 30 行，并以非 0 退出。
set -euo pipefail
cd "$(dirname "$0")/../.."          # 仓库根

MODEL="${MODEL:-Qwen/Qwen3.5-4B}"
OUT="${OUT:-backend/outputs}"
BATCH="${BATCH:-2}"
ACCUM="${ACCUM:-8}"
MAXLEN="${MAXLEN:-1024}"
RESUME="${RESUME:-auto}"
DRYRUN="${DRYRUN:-0}"

SFT_OUT="$OUT/qwen3_5_4b_sft"
DPO_OUT="$OUT/qwen3_5_4b_dpo"
LOG_DIR="$OUT/logs"
SUMMARY_TSV="$LOG_DIR/summary.tsv"
TOTAL_SECS=0
CURRENT_LOG=""
LAST_SECS=0
mkdir -p "$OUT" "$LOG_DIR"
: > "$SUMMARY_TSV"

record_step() {   # 步骤名 耗时秒 峰值显存
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$SUMMARY_TSV"
  TOTAL_SECS=$((TOTAL_SECS + $2))
}

run_logged() {    # 步骤名 日志文件 命令...
  local label="$1" log="$2"; shift 2
  CURRENT_LOG="$log"
  local t0 t1 rc
  echo
  echo "=================================================================="
  echo "== ${label}"
  echo "   日志：${log}"
  echo "=================================================================="
  t0=$(date +%s)
  set +e
  "$@" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  set -e
  t1=$(date +%s)
  LAST_SECS=$((t1 - t0))
  if [ "$rc" -ne 0 ]; then
    echo
    echo "!! ${label} 失败（退出码 ${rc}）"
    echo "   完整日志：${log}"
    echo "   最后 30 行："
    tail -n 30 "$log" || true
    exit "$rc"
  fi
}

count_lines() {
  local f="$1"
  if [ ! -f "$f" ]; then echo "MISSING"; return 0; fi
  wc -l < "$f" | tr -d ' \r'
}

read_vram() {     # 从 train_summary.json 读 peak_vram_gb
  local f="$1"
  if [ ! -f "$f" ]; then echo "未记录"; return 0; fi
  "$PY" - "$f" 2>/dev/null <<'PY' || echo "未记录"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
v = d.get("peak_vram_gb")
print(f"{v} GB" if isinstance(v, (int, float)) and v > 0 else "未记录")
PY
}

find_latest_checkpoint() {   # $1=适配器目录；打印 step 最大的 checkpoint 路径
  local dir="$1" best="" d s
  [ -d "$dir" ] || return 0
  for d in "$dir"/checkpoint-*; do
    [ -d "$d" ] || continue
    s="${d##*-}"
    case "$s" in ''|*[!0-9]*) continue ;; esac
    if [ -z "$best" ] || [ "$s" -gt "$best" ]; then best="$s"; fi
  done
  if [ -n "$best" ]; then printf '%s/checkpoint-%s' "$dir" "$best"; fi
}

# =============================================================================
# 0. 环境：DRYRUN 用本机 python；真跑时优先用 setup_gpu_env.sh 建的 venv
# =============================================================================
if [ "$DRYRUN" = "1" ]; then
  PY="${PY:-python}"
  echo "[env] DRYRUN=1：跳过建环境，使用 $PY（本机验证 preflight 与数据准备）"
else
  if [ ! -x "backend/training/.venv/bin/python" ]; then
    run_logged "环境安装" "$LOG_DIR/00_setup.log" bash backend/training/setup_gpu_env.sh
    record_step "环境安装" "$LAST_SECS" "未记录"
  fi
  PY="${PY:-backend/training/.venv/bin/python}"
fi

# =============================================================================
# 1. preflight：缺数据就打印明确修法并非 0 退出
# =============================================================================
echo
echo "== preflight：数据文件"
PREFLIGHT_FAIL=0

check_rows() {    # $1=路径 $2=期望行数 $3=修法
  local f="$1" expect="$2" fix="$3" n
  n=$(count_lines "$f")
  if [ "$n" = "MISSING" ]; then
    echo "  [FAIL] $f 不存在（应为 $expect 行）"
    echo "         修法：$fix"
    PREFLIGHT_FAIL=1
  elif [ "$n" != "$expect" ]; then
    echo "  [FAIL] $f = $n 行，应为 $expect 行"
    echo "         修法：$fix"
    PREFLIGHT_FAIL=1
  else
    echo "  [ OK ] $f = $n 行"
  fi
}

check_rows "backend/data/eval_ood/ood.jsonl" 88 \
  "按 README §12.3 重新 scp ood.jsonl；必须是 88 条，旧版 80 条不能用"

if [ ! -f "backend/data/dpo_synth/train.jsonl" ]; then
  echo "  [FAIL] backend/data/dpo_synth/train.jsonl 不存在"
  echo "         修法：按 README §12.3 传 dpo_synth/{train,val,test}.jsonl"
  PREFLIGHT_FAIL=1
else
  echo "  [ OK ] backend/data/dpo_synth/train.jsonl 存在"
fi

# mixed/core 是 prepare_data --mix 的产物：已正确就直接用，不对就先现场生成再严格复核
NEED_PREPARE=0
MIXED_ROWS=$(count_lines "backend/data/training/mixed/train.jsonl")
CORE_VAL_ROWS=$(count_lines "backend/data/training/core/val.jsonl")
if [ "$MIXED_ROWS" = "471" ]; then
  echo "  [ OK ] backend/data/training/mixed/train.jsonl = 471 行"
else
  echo "  [待生成] backend/data/training/mixed/train.jsonl = ${MIXED_ROWS}（应为 471）"
  NEED_PREPARE=1
fi
if [ "$CORE_VAL_ROWS" = "12" ]; then
  echo "  [ OK ] backend/data/training/core/val.jsonl = 12 行"
else
  echo "  [待生成] backend/data/training/core/val.jsonl = ${CORE_VAL_ROWS}（应为 12）"
  NEED_PREPARE=1
fi

# --mix 的输入源：缺了就没法重建，直接给出修法
MIX_SOURCES_OK=1
for f in backend/data/sft_v3/final80/samples.jsonl \
         backend/data/sft_synth_v2/train.jsonl \
         backend/data/sft_synth_v2/val.jsonl; do
  if [ ! -f "$f" ]; then
    MIX_SOURCES_OK=0
    if [ "$NEED_PREPARE" = "1" ]; then
      echo "  [FAIL] $f 不存在，且 mixed/core 需要重建"
      echo "         修法：按 README §12 传该文件（--mix 的输入源）"
      PREFLIGHT_FAIL=1
    else
      echo "  [警告] $f 不存在；mixed/core 已正确，本次跳过重建"
    fi
  fi
done

if [ "$PREFLIGHT_FAIL" -ne 0 ]; then
  echo
  echo "!! preflight 未通过，已停止（没有开始训练）。按上面的「修法」逐条处理后重跑。"
  exit 1
fi

# =============================================================================
# 2. 环境自检：GPU 是否真的可用
# =============================================================================
echo
echo "== 环境自检：GPU"
CUDA_OK=0
set +e
"$PY" - <<'PY' 2>&1 | tee "$LOG_DIR/00_gpu_check.log"
import sys
try:
    import torch
except Exception as exc:
    sys.exit(f"[FAIL] 无法 import torch（{exc}）。修法：bash backend/training/setup_gpu_env.sh")
if not torch.cuda.is_available():
    sys.exit("[FAIL] torch.cuda.is_available() 为 False。修法：先 nvidia-smi 确认有卡；"
             "若 torch 装成 CPU 版，重跑 setup_gpu_env.sh")
name = torch.cuda.get_device_name(0)
total = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
print(f"[ OK ] GPU: {name}   显存 {total:.1f} GB")
if total < 20:
    print(f"[警告] 显存 {total:.1f}GB < 20GB，4B QLoRA 可能 OOM："
          f"先 BATCH=1 ACCUM=16，再 MAXLEN=768")
PY
CUDA_OK=${PIPESTATUS[0]}
set -e
if [ "$CUDA_OK" -ne 0 ]; then
  echo
  echo "!! GPU 自检失败（日志：$LOG_DIR/00_gpu_check.log），已停止。"
  exit "$CUDA_OK"
fi

# =============================================================================
# 3. 数据准备（幂等）：prepare_data --mix + 首次补 DPO 转换
# =============================================================================
prepare_all() {
  if [ "$MIX_SOURCES_OK" = "1" ]; then
    "$PY" backend/training/prepare_data.py --mix
  else
    echo "[prepare] 缺少 --mix 输入源，跳过重建（沿用现有 mixed/core）"
  fi
  if [ ! -f "backend/data/training/dpo/train.jsonl" ]; then
    local sft_dir="backend/data/sft_synth_v2"
    [ -d "$sft_dir" ] || sft_dir="backend/data/sft_synth"
    echo "[prepare] backend/data/training/dpo 不存在，补跑完整转换（sft-dir=$sft_dir）"
    "$PY" backend/training/prepare_data.py --sft-dir "$sft_dir"
  fi
}

run_logged "数据准备" "$LOG_DIR/01_prepare_data.log" prepare_all
record_step "数据准备" "$LAST_SECS" "未记录"

# 生成后严格复核（preflight 时允许「待生成」，这里不允许）
for pair in "backend/data/training/mixed/train.jsonl:471" \
            "backend/data/training/core/val.jsonl:12" \
            "backend/data/eval_ood/ood.jsonl:88"; do
  f="${pair%%:*}"; expect="${pair##*:}"
  n=$(count_lines "$f")
  if [ "$n" != "$expect" ]; then
    echo
    echo "!! 数据准备后复核失败：$f = $n 行，应为 $expect 行"
    echo "   日志：$LOG_DIR/01_prepare_data.log"
    echo "   修法：检查 --mix 输入源（sft_v3/final80/samples.jsonl、sft_synth_v2/{train,val}.jsonl）"
    exit 1
  fi
done
echo "[prepare] 复核通过：mixed 471 / core val 12 / ood 88"

if [ "$DRYRUN" = "1" ]; then
  echo
  echo "== DRYRUN=1：preflight 与数据准备均通过，跳过 SFT / DPO / 评测。"
fi

# =============================================================================
# 4. SFT（读 train_config.yaml：8 epoch / eval+save 每 epoch / 早停 / best ckpt）
# =============================================================================
if [ "$DRYRUN" != "1" ]; then
  RESUME_ARG=""
  case "$RESUME" in
    0)
      echo "[resume] RESUME=0，从头训练" ;;
    auto)
      RESUME_ARG=$(find_latest_checkpoint "$SFT_OUT")
      if [ -n "$RESUME_ARG" ]; then
        echo "[resume] auto → 找到 checkpoint：$RESUME_ARG"
      else
        echo "[resume] auto → 未找到 checkpoint，从头训练"
      fi ;;
    *)
      RESUME_ARG="$RESUME"
      echo "[resume] 使用指定 checkpoint：$RESUME_ARG" ;;
  esac

  run_sft() {
    set -- --model "$MODEL" --batch "$BATCH" --grad-accum "$ACCUM" \
           --max-len "$MAXLEN" --output "$SFT_OUT"
    if [ -n "$RESUME_ARG" ]; then
      set -- "$@" --resume-from-checkpoint "$RESUME_ARG"
    fi
    "$PY" backend/training/train_sft.py "$@"
  }
  run_logged "SFT（8 epoch/早停）" "$LOG_DIR/02_sft.log" run_sft
  record_step "SFT" "$LAST_SECS" "$(read_vram "$SFT_OUT/train_summary.json")"

  # DPO 与评测都接 train_summary.json 里的 best_checkpoint，不用 last
  BEST=""
  if [ -f "$SFT_OUT/train_summary.json" ]; then
    BEST=$("$PY" - "$SFT_OUT/train_summary.json" <<'PY'
import json, sys
v = json.load(open(sys.argv[1], encoding="utf-8")).get("best_checkpoint") or ""
print(v)
PY
)
  fi
  if [ -z "$BEST" ] || [ "$BEST" = "None" ] || [ ! -d "$BEST" ]; then
    echo "[best] train_summary.json 没有可用的 best_checkpoint，回退到 $SFT_OUT"
    echo "[best] （load_best_model_at_end=true 时该目录保存的就是 best 权重）"
    BEST="$SFT_OUT"
  else
    echo "[best] SFT best checkpoint：$BEST"
  fi

  # ===========================================================================
  # 5. DPO：接 SFT 的 best adapter
  # ===========================================================================
  run_logged "DPO" "$LOG_DIR/03_dpo.log" \
    "$PY" backend/training/train_dpo.py \
      --model "$MODEL" --sft-adapter "$BEST" \
      --data backend/data/training/dpo \
      --batch 1 --grad-accum "$ACCUM" --max-len "$MAXLEN" \
      --output "$DPO_OUT"
  record_step "DPO" "$LAST_SECS" "$(read_vram "$DPO_OUT/train_summary.json")"

  # ===========================================================================
  # 6. 评测：88 条 OOD，base vs SFT(best) vs SFT(best)+DPO
  # ===========================================================================
  run_logged "OOD 评测（88 条 ×3 档）" "$LOG_DIR/04_eval.log" \
    "$PY" backend/training/eval_compare.py \
      --ood backend/data/eval_ood/ood.jsonl \
      --models "base=$MODEL" \
      --models "sft=$MODEL:$BEST" \
      --models "dpo=$MODEL:$DPO_OUT" \
      --pred-out "$OUT/preds" \
      --out-json "$OUT/eval_result.json" \
      --detail-csv "$OUT/eval_detail.csv"
  record_step "OOD 评测" "$LAST_SECS" "未记录"
fi

# =============================================================================
# 7. 汇总：步骤 / 耗时 / 峰值显存
# =============================================================================
echo
echo "================ 汇总（步骤 / 耗时 / 峰值显存） ================"
printf '%-24s %9s %14s\n' "步骤" "耗时" "峰值显存"
while IFS=$'\t' read -r label secs vram; do
  printf '%-24s %8ss %14s\n' "$label" "$secs" "$vram"
done < "$SUMMARY_TSV"
printf '%-24s %8ss\n' "合计" "$TOTAL_SECS"

echo
echo "== 产物："
echo "  $OUT/eval_result.json          （必须拷回）"
echo "  $OUT/eval_detail.csv           （必须拷回）"
echo "  $OUT/preds/{base,sft,dpo}.jsonl（必须拷回）"
echo "  $SFT_OUT/train_summary.json    （必须拷回）"
echo "  $SFT_OUT/checkpoint-*/         （adapter，几 GB，可不拷）"
echo "  $DPO_OUT/                      （adapter，几 GB，可不拷）"
echo "  $LOG_DIR/*.log                 （必须拷回）"
echo
echo "== 全部完成。"
