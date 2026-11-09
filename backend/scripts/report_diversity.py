#!/usr/bin/env python
"""对 SFT 数据集做可复核的多样性统计，并生成 v2 的 DATA_CARD.md。

口径（报告里会原样写出，避免读者误解）
--------------------------------------
* distinct-n：中文没有分词库，按字符级 token 计算。先把问句按
  `[^\\w\\u4e00-\\u9fff]` 切成若干段，再把每段按单字切开，n-gram 不跨段。
* 问句长度：一条样本里所有 user 消息的**问句部分**长度之和，已去掉随问句附带的
  `<data>...</data>` 数据块，只留用户真正说出口的那句话。
* 答案长度：一条样本里所有 assistant 消息文本长度之和；v1 全是单轮，
  所以与「最后一条回答长度」等价。
* 模板覆盖：把问句里的日期替换成 <date>、数字替换成 <num>、代码替换成 <code>，
  去掉标点后作为骨架，骨架去重数即模板覆盖数。

用法：
  cd backend
  python scripts/report_diversity.py                    # 打印 v1 vs v2 并写 DATA_CARD
  python scripts/report_diversity.py --no-write         # 只打印
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
SPLIT_RE = re.compile(r"[^\w\u4e00-\u9fff]")
CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")

INTENT_ORDER = ["market_query", "investment_analysis", "risk_assessment",
                "portfolio_query", "refusal", "clarification"]
INTENT_CN = {
    "market_query": "看盘", "investment_analysis": "回测",
    "risk_assessment": "风险", "portfolio_query": "持仓",
    "refusal": "拒答", "clarification": "澄清",
}
TONE_ORDER = ["colloquial", "formal", "brief", "typo", "multi_need", "n/a"]
TONE_CN = {"colloquial": "口语", "formal": "正式", "brief": "极简",
           "typo": "错别字/倒装", "multi_need": "多重需求", "n/a": "未标注"}


# ============================ 读取 ============================
def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_dataset(root: Path, splits: tuple[str, ...] = ("train", "val", "test")):
    rows, per_split = [], {}
    for sp in splits:
        got = load_jsonl(root / f"{sp}.jsonl")
        per_split[sp] = len(got)
        for r in got:
            r["_split"] = sp
        rows.extend(got)
    manifest = {}
    mp = root / "manifest.json"
    if mp.exists():
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    return rows, per_split, manifest


# ============================ 统计口径 ============================
def _utterance_text(content: str) -> str:
    """去掉附在问句后面的 <data> 块，只留用户真正说的那句话。"""
    idx = content.find("<data>")
    return (content[:idx] if idx >= 0 else content).strip()


def user_texts(sample: dict) -> list[str]:
    return [_utterance_text(m["content"]) for m in sample["messages"]
            if m.get("role") == "user"]


def question_text(sample: dict) -> str:
    return "\n".join(t.strip() for t in user_texts(sample))


def answer_text(sample: dict) -> str:
    return "\n".join(m["content"] for m in sample["messages"] if m.get("role") == "assistant")


def turns(sample: dict) -> int:
    return len(user_texts(sample))


def skeleton(text: str) -> str:
    t = DATE_RE.sub("<date>", text)
    t = CODE_RE.sub("<code>", t)
    t = NUM_RE.sub("<num>", t)
    t = re.sub(r"[^\w\u4e00-\u9fff<>]", "", t)
    return t


def char_ngrams(text: str, n: int) -> list[str]:
    out: list[str] = []
    for seg in SPLIT_RE.split(text):
        if not seg:
            continue
        chars = list(seg)
        if len(chars) < n:
            continue
        out.extend("".join(chars[i:i + n]) for i in range(len(chars) - n + 1))
    return out


def distinct_n(texts: list[str], n: int) -> dict:
    grams: list[str] = []
    for t in texts:
        grams.extend(char_ngrams(t, n))
    total = len(grams)
    uniq = len(set(grams))
    return {"distinct": uniq, "total": total,
            "ratio": round(uniq / total, 4) if total else 0.0}


def pct(a: int, b: int) -> float:
    return round(a / b * 100, 2) if b else 0.0


def refusal_subtype(sample: dict) -> str:
    sub = (sample.get("provenance") or {}).get("subtype") or ""
    if sub:
        return sub
    # v1 没记 subtype，这里按问句关键词归类，仅用于对比，报告中已注明
    q = question_text(sample)
    if ("明天会涨" in q) or ("目标价" in q):
        return "prediction_refusal(推断)"
    for kw in ("AAPL", "截面", "分钟", "逐笔", "十档", "机构", "北向", "新闻",
               "研报", "期权", "期货", "CPI", "宏观", "估值分位"):
        if kw in q:
            return "out_of_capability(推断)"
    return "data_insufficient(推断)"


def analyze(rows: list[dict], per_split: dict, manifest: dict) -> dict:
    total = len(rows)
    by_intent = Counter(r.get("intent") or "unknown" for r in rows)
    by_tone = Counter(r.get("tone") or "n/a" for r in rows)
    qlens = [len(question_text(r)) for r in rows]
    alens = [len(answer_text(r)) for r in rows]
    qtexts = [question_text(r) for r in rows]
    skel_by_intent: dict[str, set[str]] = {}
    skel_by_intent_tone: dict[str, set[str]] = {}
    for r in rows:
        intent = r.get("intent") or "unknown"
        sk = skeleton(question_text(r))
        skel_by_intent.setdefault(intent, set()).add(sk)
        key = f"{intent}/{r.get('tone') or 'n/a'}"
        skel_by_intent_tone.setdefault(key, set()).add(sk)
    multi = [r for r in rows if turns(r) >= 2]
    mt_hist = Counter(turns(r) for r in rows)
    refusal_rows = [r for r in rows if r.get("intent") == "refusal"]
    refusal_sub = Counter(refusal_subtype(r) for r in refusal_rows)
    subtype = Counter(f"{r.get('intent')}/{r.get('task')}" for r in rows)
    return {
        "total": total,
        "per_split": per_split,
        "by_intent": {k: {"count": v, "pct": pct(v, total)}
                      for k, v in sorted(by_intent.items(),
                                         key=lambda kv: (-kv[1], kv[0]))},
        "by_tone": {k: v for k, v in sorted(by_tone.items(), key=lambda kv: -kv[1])},
        "templates_total": len({skeleton(t) for t in qtexts}),
        "templates_by_intent": {k: len(v) for k, v in sorted(skel_by_intent.items())},
        "templates_by_intent_tone": {k: len(v) for k, v in
                                      sorted(skel_by_intent_tone.items())},
        "distinct": {n: distinct_n(qtexts, n) for n in (1, 2, 3)},
        "q_len": {"mean": round(sum(qlens) / total, 1) if total else 0.0,
                  "max": max(qlens) if qlens else 0},
        "a_len": {"mean": round(sum(alens) / total, 1) if total else 0.0,
                  "max": max(alens) if alens else 0},
        "multi_turn": {"count": len(multi), "pct": pct(len(multi), total),
                       "turn_hist": {str(k): v for k, v in sorted(mt_hist.items())}},
        "refusal_subtypes": dict(refusal_sub),
        "refusal_total": len(refusal_rows),
        "subtype_counts": dict(subtype),
        "manifest_counts": (manifest.get("counts") or {}),
        "manifest_rejects": (manifest.get("rejected_by_reason") or {}),
        "manifest_leakage": (manifest.get("leakage_check") or {}),
        "manifest_generated_at": manifest.get("generated_at", ""),
    }


# ============================ 打印 ============================
def _table(title: str, headers: list[str], rows: list[list]) -> str:
    out = [f"### {title}", "", "| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    out.append("")
    return "\n".join(out)


def render_stats_markdown(st: dict, label: str) -> str:
    parts: list[str] = []
    parts.append(_table(
        "意向分布", ["意图", "条数", "占比"],
        [[INTENT_CN.get(k, k), v["count"], f"{v['pct']}%"]
         for k, v in st["by_intent"].items()]))
    parts.append(_table(
        "问句骨架覆盖", ["意图", "骨架数"],
        [[INTENT_CN.get(k, k), v] for k, v in st["templates_by_intent"].items()] +
        [["合计", st["templates_total"]]]))
    parts.append(_table(
        "distinct-n（字符级，问句）", ["n", "去重 n-gram", "总 n-gram", "distinct-n"],
        [[n, d["distinct"], d["total"], d["ratio"]] for n, d in st["distinct"].items()]))
    return "\n".join(parts)


def render_data_card(v1: dict, v2: dict, v2_dir: Path) -> str:
    mc = v2["manifest_counts"]
    lines = [
        "# 衡策 SFT 合成数据 v2（DATA CARD）",
        "",
        f"生成时间：{v2['manifest_generated_at'] or '未知'}（统计脚本：`backend/scripts/report_diversity.py`）",
        "",
        "## 这是什么",
        "",
        "A 股投研 Agent「衡策」的监督微调数据。v2 在 v1 的基础上重做问法分布与意图配比，"
        "所有数字来自仓库内真实接口：baostock 前复权日线（`app/agent/tools.py::get_historical`）、"
        "指标计算（`app/agent/indicators.py::compute_indicators`）与回测引擎"
        "（`app/backtest_engine/runner.py::run_strategy_backtest`）。答案里的每个数字先在 "
        "`FactBook` 登记，再由 `run_gate_v2` 逐条复核；未登记数字直接拒收。",
        "",
        "## 构造方式（可核对）",
        "",
        "- 标的池 40 只 A 股；每只拉 340 根日线，从中截取 0 / 20 / 40 个交易日前的三个收盘快照，"
        "同一标的的不同样本因此对应不同的真实交易日，而不是只换代码。",
        "- 四类意图各 12 条问句模板，带语气标签（口语 / 正式 / 极简 / 错别字 / 多重需求）；"
        "子主题决定答案渲染哪些字段：看盘=快照/技术面/区间，风险=波动/回撤/位置，"
        "持仓=盈亏/占比/成本。",
        "- 回测：4 个策略 × 3 个窗口（90/150/250 个交易日）× 40 只标的，逐次真实调用回测引擎。",
        "- 多轮：看盘→风险、风险→位置、持仓→成本、双标的组合→单只追问；两轮共用一个 "
        "`FactBook`，`numbers` / `dates` / `source_ids` 取并集。",
        "- 澄清：8 类歧义（整体/单只、维度不明、指代不清、缺成本、缺标的与策略、多标的、要建议、"
        "时间范围不明）× 4 种问法，先反问，不取数。",
        "- 拒答：三类分别计数（见下表），其中「数据不足」与「能力边界」的样本都是先真实调用失败、"
        "再写拒答，不是凭空声明。",
        "",
        "## 规模与划分",
        "",
        f"- 保留样本：{v2['total']} 条；train / val / test = "
        f"{v2['per_split'].get('train', 0)} / {v2['per_split'].get('val', 0)} / "
        f"{v2['per_split'].get('test', 0)}",
        f"- 生成 {mc.get('generated', '未知')} 条，闸门保留 {mc.get('kept', '未知')} 条，"
        f"拒收 {mc.get('rejected', '未知')} 条，跳过 {mc.get('skipped', '未知')} 条；"
        f"数据源 {mc.get('sources', '未知')} 条",
        f"- 目标 1200 条，缺口 {mc.get('gap_to_target', '未知')} 条",
        "",
    ]
    lines.append(render_stats_markdown(v2, "v2"))
    lines += [
        "统计口径（下表数字都按这一口径计算）：",
        "",
        "- distinct-n：中文没有分词库，按**字符级** token 计算。先把问句按 "
        "`[^\\w\\u4e00-\\u9fff]` 切成若干段，再把每段按单字切开，n-gram 不跨段。",
        "- 问句长度：一条样本所有 user 消息的**问句部分**长度之和，已去掉 `<data>` 块。",
        "- 答案长度：一条样本所有 assistant 消息长度之和。",
        "- 模板覆盖：把问句里的日期换成 `<date>`、数字换成 `<num>`、代码换成 `<code>`，"
        "去掉标点后作为骨架，骨架去重数即覆盖数。",
        "- 多轮：一条样本里 user 消息 ≥ 2 条。",
        "",
    ]
    lines += [
        "骨架数是实测去重结果，包含多轮样本「第一轮 + 第二轮」的组合，因此高于设计模板数；"
        "四类意图各设计 12 条模板（合计 48 条），澄清 8 类 × 4 种问法，拒答 25 条模板。"
        "同一骨架在多轮里仍按一条模板计，不会因为换了追问句而算成新模板。",
        "",
    ]
    lines.append(_table(
        "语气分布（v2）", ["语气", "条数", "占全部"],
        [[TONE_CN.get(k, k), v, f"{pct(v, v2['total'])}%"]
         for k, v in v2["by_tone"].items()]))
    lines.append(_table(
        "骨架覆盖（意图 × 语气，v2）", ["意图/语气", "骨架数"],
        [[k, v] for k, v in v2["templates_by_intent_tone"].items()]))
    lines.append(_table(
        "多轮与长度（v2）", ["指标", "数值"],
        [["多轮条数", v2["multi_turn"]["count"]],
         ["多轮占比", f"{v2['multi_turn']['pct']}%"],
         ["轮次分布", json.dumps(v2["multi_turn"]["turn_hist"], ensure_ascii=False)],
         ["问句平均长度（已去 <data> 块）", v2["q_len"]["mean"]],
         ["问句最长", v2["q_len"]["max"]],
         ["答案平均长度", v2["a_len"]["mean"]],
         ["答案最长", v2["a_len"]["max"]]]))
    lines.append(_table(
        "拒答子类（v2）", ["子类", "条数"],
        [[k, v] for k, v in sorted(v2["refusal_subtypes"].items(),
                                   key=lambda kv: -kv[1])] +
        [["合计", v2["refusal_total"]]]))
    lines.append(_table(
        "闸门拒收原因（v2）", ["原因", "条数"],
        [[k, v] for k, v in sorted(v2["manifest_rejects"].items(),
                                   key=lambda kv: -kv[1])] or [["无", 0]]))
    lk = v2["manifest_leakage"] or {}
    lines.append(_table(
        "切分泄漏检查（v2）", ["检查项", "结果"],
        [["跨 split 的标的",
          len(lk.get("symbols_in_multiple_splits") or [])],
         ["跨 split 的区间键",
          len(lk.get("intervals_in_multiple_splits") or [])],
         ["检查的标的数", lk.get("symbols_checked", "未知")],
         ["检查的区间键数", lk.get("intervals_checked", "未知")]]))
    lines += [
        "## v2 与 v1 对比",
        "",
        "| 指标 | v1 | v2 |",
        "|---|---|---|",
        f"| 样本数 | {v1['total']} | {v2['total']} |",
        f"| 回测占比 | {v1['by_intent'].get('investment_analysis', {}).get('pct', 0)}% | "
        f"{v2['by_intent'].get('investment_analysis', {}).get('pct', 0)}% |",
        f"| 看盘 / 风险 / 持仓 | "
        f"{v1['by_intent'].get('market_query', {}).get('count', 0)} / "
        f"{v1['by_intent'].get('risk_assessment', {}).get('count', 0)} / "
        f"{v1['by_intent'].get('portfolio_query', {}).get('count', 0)} | "
        f"{v2['by_intent'].get('market_query', {}).get('count', 0)} / "
        f"{v2['by_intent'].get('risk_assessment', {}).get('count', 0)} / "
        f"{v2['by_intent'].get('portfolio_query', {}).get('count', 0)} |",
        f"| 澄清样本 | {v1['by_intent'].get('clarification', {}).get('count', 0)} | "
        f"{v2['by_intent'].get('clarification', {}).get('count', 0)} |",
        f"| 多轮占比 | {v1['multi_turn']['pct']}% | {v2['multi_turn']['pct']}% |",
        f"| 问句骨架数 | {v1['templates_total']} | {v2['templates_total']} |",
        f"| distinct-1 | {v1['distinct'][1]['ratio']} | {v2['distinct'][1]['ratio']} |",
        f"| distinct-2 | {v1['distinct'][2]['ratio']} | {v2['distinct'][2]['ratio']} |",
        f"| distinct-3 | {v1['distinct'][3]['ratio']} | {v2['distinct'][3]['ratio']} |",
        f"| 问句平均/最长长度 | {v1['q_len']['mean']} / {v1['q_len']['max']} | "
        f"{v2['q_len']['mean']} / {v2['q_len']['max']} |",
        f"| 答案平均/最长长度 | {v1['a_len']['mean']} / {v1['a_len']['max']} | "
        f"{v2['a_len']['mean']} / {v2['a_len']['max']} |",
        "",
        "v1 与 v2 的 distinct-n 口径相同：字符级、按标点切段后段内取字，n-gram 不跨段。"
        "v1 的拒答没有记录子类，本卡对 v1 不展示子类拆分；v2 的三子类见上表。",
        "",
        "## 剩余短板（必须一起读）",
        "",
        "1. 答案仍由同一套规则模板 + 同一批真实行情生成，不是人类示范。"
        "句式受模板约束，distinct-n 的提升主要来自标的、交易日和字段组合的变化，"
        "不代表语言风格真正多样。",
        "2. 多轮样本的两轮回答仍是各自独立生成的拼接：第一轮不知道第二轮会问什么，"
        "第二轮也没有改写第一轮的措辞，只有指代（「它」「这笔」「其中 X」）是依赖上下文的。",
        "3. 少数子主题（持仓占比、风险回撤）共用同一份日线快照，答案中部分句子结构固定，"
        "同一模板在 40 只标的上重复出现，只是数字不同。",
        "4. 回测样本的收益、回撤等指标依赖引擎的固定撮合假设（次日开盘成交、固定费率），"
        "不是对策略未来表现的估计。",
        "5. 澄清与拒答样本的答案没有真实用户反馈校验，只是按当前系统提示词的行为约定撰写。",
        "6. OOD 评测集（`backend/data/eval_ood/ood.jsonl`）由人工逐条撰写，但仍是构造数据，"
        "不是真实用户日志；它只有 expected_action，没有标准答案文本，只能评动作，不能评数字。",
        "",
        "## 使用限制",
        "",
        "- 只用于本项目内的 SFT / 评测，不对外宣称是真实用户日志。",
        "- 引用统计数字时请连同口径一起引用（字符级 distinct-n、骨架去重、多轮=user 消息≥2）。",
        "",
    ]
    return "\n".join(lines)


def write_data_card(v2_dir: Path, v1_dir: Path | None = None) -> Path:
    v1_dir = v1_dir or (v2_dir.parent / "sft_synth_v1")
    v1_rows, v1_split, v1_manifest = load_dataset(v1_dir)
    v2_rows, v2_split, v2_manifest = load_dataset(v2_dir)
    v1 = analyze(v1_rows, v1_split, v1_manifest)
    v2 = analyze(v2_rows, v2_split, v2_manifest)
    card = render_data_card(v1, v2, v2_dir)
    out = v2_dir / "DATA_CARD.md"
    out.write_text(card, encoding="utf-8")
    return out


def print_report(v1: dict, v2: dict) -> None:
    for label, st in (("v1", v1), ("v2", v2)):
        print(f"\n===== {label} =====")
        print(f"总数 {st['total']}  train/val/test = "
              f"{st['per_split'].get('train', 0)}/{st['per_split'].get('val', 0)}/"
              f"{st['per_split'].get('test', 0)}")
        for k, v in st["by_intent"].items():
            print(f"  {INTENT_CN.get(k, k):<4} {v['count']:>5}  {v['pct']}%")
        print(f"  多轮 {st['multi_turn']['count']} 条（{st['multi_turn']['pct']}%）"
              f"  轮次 {st['multi_turn']['turn_hist']}")
        print(f"  骨架数 {st['templates_total']}  "
              f"distinct-1/2/3 = {st['distinct'][1]['ratio']}/"
              f"{st['distinct'][2]['ratio']}/{st['distinct'][3]['ratio']}")
        print(f"  问句 平均 {st['q_len']['mean']} 最长 {st['q_len']['max']}；"
              f"答案 平均 {st['a_len']['mean']} 最长 {st['a_len']['max']}")
        if st["refusal_subtypes"]:
            print(f"  拒答子类 {st['refusal_subtypes']}")
        print(f"  拒收 {st['manifest_rejects']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="v1/v2 多样性统计 + 写 DATA_CARD")
    ap.add_argument("--v1", default=str(BACKEND_ROOT / "data" / "sft_synth"))
    ap.add_argument("--v2", default=str(BACKEND_ROOT / "data" / "sft_synth_v2"))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    v1_rows, v1_split, v1_manifest = load_dataset(Path(args.v1))
    v2_rows, v2_split, v2_manifest = load_dataset(Path(args.v2))
    if not v2_rows:
        print(f"[错误] {args.v2} 没有样本，先跑 gen_sft_synth_v2.py")
        return 1
    v1 = analyze(v1_rows, v1_split, v1_manifest)
    v2 = analyze(v2_rows, v2_split, v2_manifest)
    print_report(v1, v2)
    if not args.no_write:
        out = write_data_card(Path(args.v2), Path(args.v1))
        print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
