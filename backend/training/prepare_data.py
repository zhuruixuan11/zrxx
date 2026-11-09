#!/usr/bin/env python
"""把衡策的合成数据整理成训练框架可直接吃的格式。

输入（默认）：
  backend/data/sft_synth/{train,val,test}.jsonl      messages: system/user/assistant
  backend/data/dpo_synth/{train,val,test}.jsonl      prompt / chosen / rejected

输出（默认 backend/data/training/）：
  sft/{train,val,test}.jsonl        TRL / transformers 的 messages 格式
  sft_sharegpt/{train,val,test}.jsonl  LLaMA-Factory 的 sharegpt 格式
  dpo/{train,val,test}.jsonl        仅保留 prompt / chosen / rejected
  system_prompt.txt                 最终使用的 system prompt（训练与推理必须一致）
  stats.json                        条数、system prompt 对齐方式、哈希

关键点：训练语料的 system prompt 与线上 `app/agent/nodes.py::node_analysis`
里传给 LLM 的 system_prompt 必须完全一致，否则训练/部署分布不一致会掉效果。
默认 --align-system runtime，会把所有样本的 system 改写成线上那一句。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
NODES = BACKEND / "app" / "agent" / "nodes.py"

# 骨架函数复用 report_diversity.py，避免两套口径（它正是产出「v2 136 个骨架」的那套）。
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from scripts.report_diversity import answer_text, question_text, skeleton  # noqa: E402


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def runtime_system_prompt() -> str | None:
    """从 nodes.py 里读出真实传给 LLM 的 system_prompt（字符串常量拼接）。"""
    if not NODES.exists():
        return None
    tree = ast.parse(NODES.read_text(encoding="utf-8", errors="ignore"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "system_prompt":
                continue
            parts: list[str] = []
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    parts.append(sub.value)
            text = "".join(parts).strip()
            if text:
                return text
    return None


# =============================================================================
# --mix：精写集 68/12 切分 + 辅助集按骨架采样 + 重复写入混合训练集
# =============================================================================
DEFAULT_CONFIG = Path(__file__).resolve().parent / "train_config.yaml"

# 配置文件缺失或 PyYAML 未安装时的内置默认值（与 train_config.yaml 保持一致）
MIX_DEFAULTS: dict = {
    "seed": 42,
    "paths": {
        "core_source": "backend/data/sft_v3/final80/samples.jsonl",
        "aux_dir": "backend/data/sft_synth_v2",
        "ood_eval": "backend/data/eval_ood/ood.jsonl",
        "core_dir": "backend/data/training/core",
        "mixed_dir": "backend/data/training/mixed",
        "mixed_train": "backend/data/training/mixed/train.jsonl",
        "core_val": "backend/data/training/core/val.jsonl",
        "output_dir": "backend/outputs/qwen3_5_4b_sft_mixed",
    },
    "split": {"method": "symbol_sha1", "val_size": 12, "val_fraction": 0.15},
    "mix": {
        "core_repeat": 5,
        "aux": {
            "strategy": "per_skeleton", "per_skeleton": 1, "seed": 42,
            "source_splits": ["train", "val"],
            "prefer_shorter_answer": True,
        },
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

SYMBOL_RE = re.compile(r"\b(?:sh|sz)\.\d{6}\b")


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_mix_config(path: Path) -> dict:
    """读 YAML；缺文件/缺 PyYAML/解析失败都退回内置默认值，不崩溃。"""
    cfg = json.loads(json.dumps(MIX_DEFAULTS))  # 深拷贝，避免改到模块级默认值
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
    except Exception as exc:  # YAML 语法错误不应该让数据准备直接失败
        print(f"[警告] 配置解析失败（{exc}），使用内置默认值")
        return cfg
    return _deep_merge(cfg, loaded)


def resolve(path_str: str) -> Path:
    path = Path(path_str)
    return path if path.is_absolute() else REPO_ROOT / path


def align_messages(messages: list[dict], system: str) -> list[dict]:
    """把 messages[0] 的 system 改写成线上 system prompt，其余消息原样保留。"""
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0].get("role") == "system":
        msgs[0]["content"] = system
    else:
        msgs.insert(0, {"role": "system", "content": system})
    return msgs


def symbol_of(sample: dict) -> str:
    params = (sample.get("provenance") or {}).get("params") or {}
    if params.get("symbol"):
        return str(params["symbol"])
    for msg in sample.get("messages", []):
        found = SYMBOL_RE.findall(msg.get("content", ""))
        if found:
            return found[0]
    return str(sample.get("sample_id"))


def split_core(rows: list[dict], seed: int, val_size: int) -> tuple[list[dict], list[dict], list[str]]:
    """按标的哈希切分：同一标的只落在一侧，避免标的级泄漏。

    排序键 sha1(f"{seed}:{symbol}")，取前 val_size 个标的进 val。
    final80 每个标的恰好 1 条，所以结果精确为 68/12。
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(symbol_of(row), []).append(row)
    ranked = sorted(groups.items(),
                    key=lambda kv: hashlib.sha1(f"{seed}:{kv[0]}".encode()).hexdigest())
    val_rows: list[dict] = []
    val_symbols: list[str] = []
    for sym, grp in ranked:
        if len(val_rows) + len(grp) > val_size:
            continue
        val_rows.extend(grp)
        val_symbols.append(sym)
        if len(val_rows) == val_size:
            break
    if len(val_rows) != val_size:
        raise ValueError(f"按标的哈希切出 val={len(val_rows)} 条，与 split.val_size={val_size} 不符")
    val_ids = {r.get("sample_id") for r in val_rows}
    train_rows = [r for r in rows if r.get("sample_id") not in val_ids]
    return train_rows, val_rows, val_symbols


def sample_aux(aux_dir: Path, aux_cfg: dict,
               splits: list[str] | None = None) -> list[tuple[str, str, dict]]:
    """每个骨架取 per_skeleton 条。

    候选池优先级：source_splits 里靠前的 split 优先（默认 train > val）；
    同一骨架、同一 split 内先取答案更短的，再按 sha1(seed:sample_id) 定序。
    splits 显式给出时覆盖配置里的 source_splits（用于统计「若含 test」会多出多少条）。
    """
    seed = int(aux_cfg.get("seed", 42))
    per = int(aux_cfg.get("per_skeleton", 1))
    splits = list(splits if splits is not None
                  else (aux_cfg.get("source_splits") or ["train"]))
    order = {sp: i for i, sp in enumerate(splits)}
    groups: dict[str, list[tuple[str, dict]]] = {}
    for sp in splits:
        for row in load_jsonl(aux_dir / f"{sp}.jsonl"):
            groups.setdefault(skeleton(question_text(row)), []).append((sp, row))

    picked: list[tuple[str, str, dict]] = []
    for sk in sorted(groups):
        candidates = groups[sk]
        best_split = min(order[sp] for sp, _ in candidates)
        candidates = [(sp, r) for sp, r in candidates if order[sp] == best_split]
        if aux_cfg.get("prefer_shorter_answer", True):
            key = lambda t: (len(answer_text(t[1])),
                             hashlib.sha1(f"{seed}:{t[1].get('sample_id')}".encode()).hexdigest())
        else:
            key = lambda t: hashlib.sha1(f"{seed}:{t[1].get('sample_id')}".encode()).hexdigest()
        for sp, row in sorted(candidates, key=key)[:per]:
            picked.append((sp, sk, row))
    return picked


def run_mix(config_path: Path) -> int:
    cfg = load_mix_config(config_path)
    seed = int(cfg.get("seed", 42))
    paths, split_cfg = cfg["paths"], cfg["split"]
    mix_cfg, train_cfg = cfg["mix"], cfg["train"]

    system = runtime_system_prompt()
    if not system:
        print("[错误] 从 app/agent/nodes.py 抽不到线上 system_prompt")
        return 1

    # ---------- 1. 精写集切分 68/12 ----------
    core_rows = load_jsonl(resolve(paths["core_source"]))
    if not core_rows:
        print(f"[错误] 精写集为空：{paths['core_source']}")
        return 1
    core_train, core_val, val_symbols = split_core(core_rows, seed, int(split_cfg["val_size"]))

    def to_trl(sample: dict) -> dict:
        row = {"id": sample["sample_id"],
               "messages": align_messages(sample["messages"], system),
               "symbol": symbol_of(sample)}
        for key in ("intent", "task", "subtype", "tone"):
            if key in sample:
                row[key] = sample[key]
        return row

    core_train_trl = [to_trl(r) for r in core_train]
    core_val_trl = [to_trl(r) for r in core_val]
    core_dir = resolve(paths["core_dir"])
    write_jsonl(core_dir / "train.jsonl", core_train_trl)
    write_jsonl(core_dir / "val.jsonl", core_val_trl)

    # ---------- 2. 辅助集按骨架采样 ----------
    aux_cfg = mix_cfg["aux"]
    picked = sample_aux(resolve(paths["aux_dir"]), aux_cfg)
    source_counts = {sp: 0 for sp in aux_cfg.get("source_splits", [])}
    aux_rows = []
    for sp, _sk, row in picked:
        source_counts[sp] = source_counts.get(sp, 0) + 1
        aux_rows.append({
            "id": row["sample_id"],
            "messages": align_messages(row["messages"], system),
            "source": "aux",
            "origin_split": sp,
        })
    aux_skeletons = len({sk for _sp, sk, _r in picked})

    # 统计「被剔除的 split 原本会贡献多少条」（默认 test 被剔除，预期 5 条）。
    configured_splits = list(aux_cfg.get("source_splits") or ["train"])
    excluded_splits = [sp for sp in ("train", "val", "test") if sp not in configured_splits]
    aux_excluded = 0
    if excluded_splits:
        full_picked = sample_aux(resolve(paths["aux_dir"]), aux_cfg,
                                 splits=["train", "val", "test"])
        aux_excluded = sum(1 for sp, _sk, _r in full_picked if sp in excluded_splits)

    # ---------- 3. 混合训练集：精写 × repeat + 辅助 ----------
    repeat = int(mix_cfg["core_repeat"])
    core_mixed = []
    for i in range(repeat):
        for row in core_train_trl:
            core_mixed.append({
                "id": f"{row['id']}::r{i}",
                "messages": row["messages"],
                "source": "core",
                "repeat": i,
                "symbol": row["symbol"],
            })
    mixed_rows = core_mixed + aux_rows
    mixed_dir = resolve(paths["mixed_dir"])
    write_jsonl(mixed_dir / "train.jsonl", mixed_rows)

    # ---------- 4. 真算步数与唯一内容 ----------
    batch = int(train_cfg.get("per_device_train_batch_size", 2))
    accum = int(train_cfg.get("gradient_accumulation_steps", 8))
    effective_batch = batch * accum
    epochs = train_cfg.get("epochs", 3)
    steps_per_epoch = math.ceil(len(mixed_rows) / effective_batch)
    total_steps = math.ceil(steps_per_epoch * float(epochs))
    unique_contents = len({json.dumps(r["messages"], ensure_ascii=False, sort_keys=True)
                           for r in mixed_rows})

    ood_rows = len(load_jsonl(resolve(paths["ood_eval"])))
    stats = {
        "seed": seed,
        "config": str(config_path),
        "system_prompt_sha1": hashlib.sha1(system.encode()).hexdigest()[:12],
        "split": {
            "method": split_cfg.get("method", "symbol_sha1"),
            "seed": seed,
            "val_size": int(split_cfg["val_size"]),
            "core_train_rows": len(core_train_trl),
            "core_val_rows": len(core_val_trl),
            "val_symbols": val_symbols,
        },
        "mix": {
            "core": {"rows": len(core_mixed), "unique": len(core_train_trl), "repeat": repeat},
            "aux": {
                "rows": len(aux_rows),
                "unique": len(aux_rows),
                "skeletons": aux_skeletons,
                "strategy": aux_cfg.get("strategy", "per_skeleton"),
                "seed": int(aux_cfg.get("seed", 42)),
                "source_split_counts": source_counts,
                "source_splits": configured_splits,
                "aux_excluded_v2_test": aux_excluded,
            },
            "total_rows": len(mixed_rows),
            "unique_total": unique_contents,
            "unique_contents": unique_contents,
            "core_share_pct": round(len(core_mixed) / len(mixed_rows) * 100, 2),
        },
        "steps": {
            "single_gpu": True,
            "per_device_batch": batch,
            "grad_accum": accum,
            "effective_batch": effective_batch,
            "steps_per_epoch": steps_per_epoch,
            "epochs": epochs,
            "total_steps": total_steps,
        },
        "ood_eval": {"path": paths["ood_eval"], "rows": ood_rows, "used_for_training": False},
    }
    (mixed_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                          encoding="utf-8")

    # ---------- 5. 打印（验收用的三个数字必须出现） ----------
    print(f"[mix] core: train {len(core_train_trl)} / val {len(core_val_trl)}"
          f"（按标的 sha1 切分，val 标的 {len(val_symbols)} 个）")
    print(f"[mix] aux: {len(aux_rows)} 行，覆盖骨架 {aux_skeletons} 个，"
          f"来源 split {source_counts}；剔除的 split {excluded_splits} 原本贡献 {aux_excluded} 条")
    print(f"[mix] mixed/train.jsonl: {len(mixed_rows)} 行 = core {len(core_mixed)}"
          f"（{len(core_train_trl)}×{repeat}） + aux {len(aux_rows)}；"
          f"唯一内容 {unique_contents} 条；精写占比 {stats['mix']['core_share_pct']}%")
    print(f"[mix] steps: ceil({len(mixed_rows)}/{effective_batch})={steps_per_epoch} 步/epoch，"
          f"{epochs} epoch = {total_steps} 步（单卡，batch {batch}×accum {accum}）")
    print(f"[mix] OOD {ood_rows} 条未参与训练，仅用于最终评测")
    print(f"[mix] 已写入 {core_dir} 与 {mixed_dir}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="衡策训练数据格式转换")
    ap.add_argument("--mix", action="store_true",
                    help="生成 68/12 精写切分 + 按骨架采样的 471 行混合训练集")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="--mix 使用的 YAML 配置")
    ap.add_argument("--sft-dir", default=str(BACKEND / "data" / "sft_synth"))
    ap.add_argument("--dpo-dir", default=str(BACKEND / "data" / "dpo_synth"))
    ap.add_argument("--out", default=str(BACKEND / "data" / "training"))
    ap.add_argument("--align-system", choices=["runtime", "corpus", "none"], default="runtime",
                    help="runtime=改写为线上 system_prompt；corpus=保留语料原句；none=不改")
    args = ap.parse_args()

    if args.mix:
        return run_mix(Path(args.config))

    sft_dir, dpo_dir, out = Path(args.sft_dir), Path(args.dpo_dir), Path(args.out)
    corpus_system = ""
    for split in ("train", "val", "test"):
        rows = load_jsonl(sft_dir / f"{split}.jsonl")
        if rows:
            corpus_system = rows[0]["messages"][0]["content"]
            break

    rt = runtime_system_prompt() if args.align_system == "runtime" else None
    system = rt or corpus_system
    if not system:
        print(json.dumps({"error": "拿不到 system prompt，检查 --sft-dir"}, ensure_ascii=False))
        return 1

    stats: dict = {"align_system": args.align_system, "system_prompt": system,
                   "system_prompt_sha1": hashlib.sha1(system.encode()).hexdigest()[:12],
                   "splits": {}}

    for split in ("train", "val", "test"):
        raw = load_jsonl(sft_dir / f"{split}.jsonl")
        trl_rows, sharegpt_rows = [], []
        for s in raw:
            msgs = [dict(m) for m in s["messages"]]
            if args.align_system != "none":
                msgs[0]["content"] = system
            trl_rows.append({"id": s["sample_id"], "messages": msgs})
            sharegpt_rows.append({
                "system": msgs[0]["content"],
                "conversations": [
                    {"from": "human", "value": msgs[1]["content"]},
                    {"from": "gpt", "value": msgs[2]["content"]},
                ],
            })
        write_jsonl(out / "sft" / f"{split}.jsonl", trl_rows)
        write_jsonl(out / "sft_sharegpt" / f"{split}.jsonl", sharegpt_rows)
        stats["splits"][f"sft_{split}"] = len(trl_rows)

    dpo_unaligned = 0
    for split in ("train", "val", "test"):
        raw = load_jsonl(dpo_dir / f"{split}.jsonl")
        rows = []
        for r in raw:
            prompt = r["prompt"]
            if args.align_system != "none" and corpus_system:
                if prompt.startswith(corpus_system):
                    prompt = system + prompt[len(corpus_system):]
                else:
                    dpo_unaligned += 1
            rows.append({"prompt": prompt, "chosen": r["chosen"], "rejected": r["rejected"]})
        write_jsonl(out / "dpo" / f"{split}.jsonl", rows)
        stats["splits"][f"dpo_{split}"] = len(rows)
    stats["dpo_prompt_unmatched"] = dpo_unaligned

    (out / "system_prompt.txt").write_text(system, encoding="utf-8")
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
