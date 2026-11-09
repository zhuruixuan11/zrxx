#!/usr/bin/env python
"""从已生成的 SFT 样本派生 DPO 偏好对（chosen = 原 SFT 答案，rejected = 有意义的错误）。

rejected 的六种错误都是**有针对性构造**的，不是随机拼接：
  1. hallucinated_number      把已登记数字改成未登记数字（答案里出现无 provenance 的数）
  2. refusal_when_should_answer 该正常作答却用「我不确定」软拒答
  3. answer_when_should_refuse  该拒答（预测涨跌）却给出涨跌/目标价
  4. backtest_as_prediction   把回测的历史结论说成对未来收益的预测
  5. fake_citation            引用不存在的研报/数据源
  6. over_boundary            超出能力边界硬答（逐笔资金流、机构持仓占比等）

重要：DPO 对由 SFT 样本派生，**不代表真实用户偏好**，不能对外宣称「基于用户反馈对齐」。
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

REASONS = [
    "hallucinated_number",
    "refusal_when_should_answer",
    "answer_when_should_refuse",
    "backtest_as_prediction",
    "fake_citation",
    "over_boundary",
]

SOFT_REFUSAL = (
    "我不太确定这个问题的答案，行情变化太快，建议你自己再看看盘面，"
    "投资有风险，决策需要谨慎。"
)


def load_sft(sft_dir: Path) -> tuple[list[dict], dict[str, str]]:
    rows: list[dict] = []
    for split in ("train", "val", "test"):
        p = sft_dir / f"{split}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                s = json.loads(line)
                s["_split"] = split
                rows.append(s)
    split_map: dict[str, str] = {}
    prov = sft_dir / "provenance.jsonl"
    if prov.exists():
        for line in prov.read_text(encoding="utf-8").splitlines():
            if line.strip():
                p = json.loads(line)
                split_map[p.get("sample_id")] = p.get("split") or ""
    return rows, split_map


def prompt_of(sample: dict) -> str:
    msgs = sample["messages"]
    return f"{msgs[0]['content']}\n\n{msgs[1]['content']}"


def _is_normal(sample: dict) -> bool:
    return sample["intent"] != "refusal"


def _numbers(text: str, codes: set[str]) -> list[tuple[str, float]]:
    masked = DATE_RE.sub(" ", text)
    for c in codes:
        masked = masked.replace(c, " ")
    out = []
    for m in NUM_RE.finditer(masked):
        try:
            out.append((m.group(), float(m.group().replace(",", ""))))
        except ValueError:
            continue
    return out


def corrupt(sample: dict, reason: str, rng: random.Random) -> tuple[str, str] | None:
    chosen = sample["messages"][2]["content"]
    prov = sample.get("provenance") or {}
    codes = {str(x) for x in (prov.get("params") or {}).values() if isinstance(x, str)}
    allowed = {round(float(x), 6) for x in (prov.get("numbers") or [])}

    if reason == "hallucinated_number":
        scored = []
        for raw, v in _numbers(chosen, codes):
            if abs(v) < 1:
                continue
            idx = chosen.find(raw)
            tail = chosen[idx + len(raw): idx + len(raw) + 2] if idx >= 0 else ""
            scored.append((0 if "%" in tail else 1, raw, v))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0])
        _, raw, val = scored[0]
        new = val
        for factor in (1.37, 0.63, 1.23, 0.81, 1.09):
            candidate = round(val * factor, 2)
            if round(candidate, 6) not in allowed and candidate != round(val, 6):
                new = candidate
                break
        else:
            return None
        rejected = chosen.replace(raw, f"{new:,.2f}" if "," in raw else f"{new:.2f}", 1)
        if rejected == chosen:
            return None
        return rejected, "把已登记数字改成未登记数字（provenance 里查不到这个数）"

    if reason == "refusal_when_should_answer":
        if not _is_normal(sample):
            return None
        return SOFT_REFUSAL, "数据齐全却用「我不确定」软拒答，也没有说明缺什么数据"

    if reason == "answer_when_should_refuse":
        if sample["intent"] != "refusal":
            return None
        probe = next((v for k, v in (prov.get("facts") or {}).items()
                      if "price" in k and isinstance(v, (int, float)) and v > 0), None)
        target = f"，目标价可看高至 {probe * 1.12:,.2f} 元" if probe else "，目标价可看高 12%"
        rejected = (
            "从当前量价形态看，明天大概率上涨 3% 左右，短线可以继续持有"
            f"{target}，回踩 5 日线是加仓机会。"
        )
        return rejected, "该拒答（预测涨跌/目标价）却给出方向、涨幅与目标价"

    if reason == "backtest_as_prediction":
        if sample["task"] != "backtest_review":
            return None
        rejected = chosen + (
            "\n照这个回测表现，接下来一个月大概率还能维持类似收益，"
            "按这个策略继续持有就行，回撤也不会太大。"
        )
        return rejected, "把历史回测结论当成未来收益预测，并给出持有建议"

    if reason == "fake_citation":
        rejected = chosen + (
            "\n（数据来源：中金公司 2026 年 10 月《A 股策略深度报告》，"
            "以及 Wind 一致预期数据）"
        )
        return rejected, "引用了系统中不存在、也未接入的研报与数据源"

    if reason == "over_boundary":
        rejected = chosen + (
            "\n另外，交易所 Level-2 逐笔数据显示主力资金净流入 3.2 亿元，"
            "机构持仓占比 18.6%，筹码正在快速集中。"
        )
        return rejected, "超出能力边界硬答：系统没有逐笔资金流与机构持仓数据，数字也是编的"

    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="从衡策 SFT 样本派生 DPO 偏好对")
    ap.add_argument("--sft-dir", default=str(BACKEND / "data" / "sft_synth"))
    ap.add_argument("--out", default=str(BACKEND / "data" / "dpo_synth"))
    ap.add_argument("--per-reason", type=int, default=45, help="每种错误构造多少对")
    ap.add_argument("--seed", type=int, default=20261107)
    args = ap.parse_args()

    rows, split_map = load_sft(Path(args.sft_dir))
    if not rows:
        print(json.dumps({"error": "没有找到 SFT 样本，请先运行 gen_sft_synth.py"}, ensure_ascii=False))
        return 1

    rng = random.Random(args.seed)
    pairs: list[dict] = []
    skipped: Counter = Counter()
    seen: set[tuple[str, str]] = set()

    for reason in REASONS:
        pool = rows[:]
        rng.shuffle(pool)
        made = 0
        for s in pool:
            if made >= args.per_reason:
                break
            if (s["sample_id"], reason) in seen:
                continue
            got = corrupt(s, reason, rng)
            if not got:
                skipped[reason] += 1
                continue
            rejected, detail = got
            chosen = s["messages"][2]["content"]
            if rejected == chosen:
                skipped[reason] += 1
                continue
            pairs.append({
                "pair_id": f"dpo-{len(pairs) + 1:05d}",
                "prompt": prompt_of(s),
                "chosen": chosen,
                "rejected": rejected,
                "rejected_reason": reason,
                "rejected_detail": detail,
                "source_sample_id": s["sample_id"],
                "intent": s["intent"],
                "task": s["task"],
                "split": split_map.get(s["sample_id"]) or s.get("_split"),
                "provenance": {
                    "source_ids": (s.get("provenance") or {}).get("source_ids") or [],
                    "numbers": (s.get("provenance") or {}).get("numbers") or [],
                },
            })
            seen.add((s["sample_id"], reason))
            made += 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_split: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        by_split[p["split"] or "train"].append(p)
    for split in ("train", "val", "test"):
        with open(out_dir / f"{split}.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for p in by_split.get(split, []):
                f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # 自检：chosen 与 rejected 必须不同，且 rejected_reason 必有值
    bad = [p["pair_id"] for p in pairs if p["chosen"] == p["rejected"] or not p["rejected_reason"]]
    manifest = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_pairs": len(pairs),
        "splits": {k: len(v) for k, v in by_split.items()},
        "by_reason": dict(Counter(p["rejected_reason"] for p in pairs)),
        "by_intent": dict(Counter(p["intent"] for p in pairs)),
        "skipped_candidates": dict(skipped),
        "invalid_pairs": bad,
        "source": "派生自 data/sft_synth（非真实用户偏好）",
        "note": "chosen 为原 SFT 答案；rejected 为定向构造的错误答案，不能宣称基于用户反馈对齐",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if pairs else 1


if __name__ == "__main__":
    raise SystemExit(main())
