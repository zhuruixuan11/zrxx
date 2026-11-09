"""11 条 RAG 探针三档对比：纯词法 / 词法+BM25 / 三路融合（RRF）。

用法（在 backend 目录）：
    python scripts/eval_rag_probes.py

三档只差「启用哪些通道」，配额、min_score、比较型拆解全部复用真实 retrieve 路径，
通过临时替换排序函数实现，测完即还原。向量模型不可用时第三档会自动降级为两路。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import rag  # noqa: E402

PROBES = [
    ("A股交易费用有哪些", "k15"),
    ("佣金最低多少", "k15"),
    ("印花税怎么收", "k15"),
    ("ETF和股票有什么区别", "k3"),
    ("T+1和涨跌停的关系", "k13"),
    ("北交所和科创板哪个涨跌幅大", "k11"),
    ("如果我买了股票当天能卖吗", "k13"),
    ("PE和PB哪个更能反映估值", "k9"),
    ("止损应该怎么设置", "k6"),
    ("定投适合什么人", "k7"),
    ("科创板最小买入多少股", "k14"),
]

TIERS = [
    # 仅 bigram 覆盖率单通道，作为「去掉 BM25 和向量」的参照。
    # 真正的「改前」是 HEAD 里的 keyword_score + bigram 实现（top1 4/11、top5 7/11），
    # 该函数已被删除，无法在本脚本内复现。
    ("纯词法(仅bigram)", ["lexical"]),
    ("词法+BM25", ["lexical", "bm25"]),
    ("三路融合", ["lexical", "bm25", "vector"]),
]


def _gate(scores, channels, i):
    """候选门槛：启用的词面通道至少一个命中，纯向量不单独入选。"""
    if scores["lexical"][i] > 0:
        return True
    return "bm25" in channels and scores["bm25"][i] > 0


def _rank_single(idx, query, channels, rrf_k):
    scores = idx.channel_scores(query)
    fused = rag._rrf_fuse([scores[c] for c in channels], rrf_k, idx.n)
    fused = [f if _gate(scores, channels, i) else 0.0 for i, f in enumerate(fused)]
    norm = rag._normalize(fused)
    rows = [
        {"doc": d, "score": norm[i], "lexical": scores["lexical"][i],
         "bm25": scores["bm25"][i],
         "vector": scores["vector"][i] if "vector" in channels else None,
         "coverage": None}
        for i, d in enumerate(idx.docs) if norm[i] > 0
    ]
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows


def _rank_multi(idx, plan, channels, rrf_k):
    lists, hit = [], [0] * idx.n
    for sub in plan["sub_queries"]:
        scores = idx.channel_scores(sub)
        lists.extend(scores[c] for c in channels)
        for i in range(idx.n):
            if _gate(scores, channels, i):
                hit[i] += 1
    fused = rag._rrf_fuse(lists, rrf_k, idx.n)
    fused = [f if hit[i] > 0 else 0.0 for i, f in enumerate(fused)]
    norm = rag._normalize(fused)
    groups = [[a for a in rag._aliases(c) if a] for c in plan.get("concepts", []) if c]
    rows = []
    for i, d in enumerate(idx.docs):
        if norm[i] <= 0:
            continue
        text = f"{d.get('topic', '')} {d.get('content', '')[:300]}".lower()
        cov = sum(1 for g in groups if any(a in text for a in g)) / len(groups) if groups else 0.0
        rows.append({"doc": d, "score": 0.7 * norm[i] + 0.3 * cov, "lexical": None,
                     "bm25": None, "vector": None, "coverage": cov})
    rows.sort(key=lambda x: (x["coverage"], x["score"]), reverse=True)
    return rows


async def run_tier(channels, rrf_k):
    orig_single, orig_multi = rag._rank_single, rag._multi_query_rank
    rag._rank_single = lambda idx, q, k=rrf_k: _rank_single(idx, q, channels, k)
    rag._multi_query_rank = lambda idx, p, k=rrf_k: _rank_multi(idx, p, channels, k)
    try:
        rows, top1, top5 = [], 0, 0
        for query, expected in PROBES:
            result = await rag.retrieve(query)
            ids = [s["id"] for s in result["sources"]]
            a, b = bool(ids) and ids[0] == expected, expected in ids
            top1 += a
            top5 += b
            rows.append((query, expected, a, b, ids))
        return rows, top1, top5
    finally:
        rag._rank_single, rag._multi_query_rank = orig_single, orig_multi


async def main():
    default_docs = rag._load_knowledge()
    user_docs = await rag._load_user_published_knowledge()
    t0 = time.time()
    idx = await asyncio.to_thread(rag._get_index, user_docs + default_docs)
    print(f"corpus: default={len(default_docs)} user={len(user_docs)} n={idx.n}")
    print(f"channels={idx.channels} embedding={rag._EMBED_STATE['status']} "
          f"dim={rag._EMBED_STATE['dim']} index_build={time.time() - t0:.1f}s")
    if rag._EMBED_STATE["error"]:
        print(f"embedding_error: {rag._EMBED_STATE['error']}")

    for name, channels in TIERS:
        effective = [c for c in channels if c in idx.channels]
        rows, top1, top5 = await run_tier(effective, rag.settings.RAG_RRF_K)
        print(f"\n=== {name} (channels={effective}) ===")
        for query, expected, a, b, ids in rows:
            print(f"{query:<24} exp={expected:<5} top1={a!s:<5} top5={b!s:<5} ids={ids}")
        print(f"SUMMARY {name}: top1 {top1}/11  top5 {top5}/11")


if __name__ == "__main__":
    asyncio.run(main())
