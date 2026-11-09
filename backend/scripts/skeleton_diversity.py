#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""骨架重复度：衡量 SFT 答案的模板化程度（口径写死，运行即打印）。

口径
----
1. 每条 assistant 答案按 ``。；;!？`` 切句，连续分隔符不产生空句；
2. 每句取前 6 个汉字作为「开头模式」，不足 6 字按实际，无汉字记为 ``<无汉字>``；
3. 句子总数、distinct 开头模式数、骨架重复度 = 1 - distinct / 总数；
4. 四要件首句：每条答案第 1/2/3/4 段的首句。答案里有换行就按换行分段，
   没有换行就按句数四等分（前几段多分一句）——这个指标比整体重复度更能
   反映模板化程度，因为套模板的答案四段开头几乎固定。

用法::

    python backend/scripts/skeleton_diversity.py [samples.jsonl]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

DELIMS = "。；;!？"
HEAD_CHARS = 6
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
SENT_SPLIT_RE = re.compile(f"[{re.escape(DELIMS)}]")

DEFAULT_PATH = (Path(__file__).resolve().parents[1]
                / "data" / "sft_reasoning_samples" / "samples.jsonl")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT_RE.split(text) if s.strip()]


def head_pattern(sentence: str) -> str:
    cjk = "".join(CJK_RE.findall(sentence))
    return cjk[:HEAD_CHARS] if cjk else "<无汉字>"


def paragraphs(text: str) -> tuple[list[list[str]], str]:
    """返回 (四段的句子列表, 分段方式)。"""
    if "\n" in text:
        blocks = [b for b in re.split(r"\n+", text) if b.strip()]
        if len(blocks) >= 4:
            return [split_sentences(b) for b in blocks[:4]], "换行分段"
    sents = split_sentences(text)
    n = len(sents)
    base, rem = divmod(n, 4)
    groups, start = [], 0
    for i in range(4):
        size = base + (1 if i < rem else 0)
        groups.append(sents[start:start + size])
        start += size
    return groups, "句数四等分"


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    records = [json.loads(line) for line in
               path.read_text(encoding="utf-8").splitlines() if line.strip()]

    print("=== 骨架重复度 ===")
    print(f"数据文件：{path}")
    print("口径：")
    print(f"  切句：按 {DELIMS} 切分，连续分隔符不产生空句")
    print(f"  开头模式：每句前 {HEAD_CHARS} 个汉字（不足按实际，无汉字记 <无汉字>）")
    print("  骨架重复度 = 1 - distinct(开头模式) / 句子总数")
    print("  四要件首句：每条答案第 1/2/3/4 段首句；有换行按换行分段，否则按句数四等分")
    print()

    header = f"{'#':>2}  {'sample_id':<12}{'句数':>4}{'distinct':>9}{'重复度':>8}  {'段法':<8}{'四要件首句'}"
    print(header)
    print("-" * len(header))

    all_patterns: list[str] = []
    four_patterns: list[str] = []
    for i, rec in enumerate(records, 1):
        answers = [m["content"] for m in rec["messages"] if m["role"] == "assistant"]
        for k, text in enumerate(answers, 1):
            sents = split_sentences(text)
            pats = [head_pattern(s) for s in sents]
            all_patterns.extend(pats)
            groups, how = paragraphs(text)
            heads = [head_pattern(g[0]) for g in groups if g]
            four_patterns.extend(heads)
            dup = 1 - len(set(pats)) / len(pats) if pats else 0.0
            tag = rec["sample_id"] if len(answers) == 1 else f"{rec['sample_id']}#{k}"
            print(f"{i:>2}  {tag:<12}{len(pats):>4}{len(set(pats)):>9}"
                  f"{dup:>8.3f}  {how:<8}{len(set(heads))}/{len(heads)}")

    total = len(all_patterns)
    distinct = len(set(all_patterns))
    dup = 1 - distinct / total if total else 0.0
    f_total = len(four_patterns)
    f_distinct = len(set(four_patterns))

    print()
    print(f"句子总数 = {total}")
    print(f"distinct 开头模式 = {distinct}")
    print(f"骨架重复度 = 1 - {distinct}/{total} = {dup:.4f}")
    print(f"四要件首句：{f_total} 句，distinct 开头模式 = {f_distinct}"
          f"（{f_distinct / f_total * 100:.1f}%）" if f_total else "四要件首句：无")

    print()
    print("重复最多的开头模式（次数 / 模式）：")
    for pat, cnt in Counter(all_patterns).most_common(8):
        print(f"  {cnt}× {pat}")

    print()
    print("回炉线（见 backend/data/sft_v3/PLAN.md）：300 条精写集要求"
          "四要件首句 distinct ≥ 30 且骨架重复度 ≤ 0.30；当前样本量不同，仅作口径对照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
