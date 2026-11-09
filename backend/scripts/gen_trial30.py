#!/usr/bin/env python
"""试产 30 条精写推理样本（v3 trial）。

- 三条原样复用 ``data/sft_reasoning_samples/samples.jsonl`` 的 sft-000002 /
  sft-000006 / sft-000010，保留 sample_id，加 ``reused=true``；
- 其余 27 条真跑 baostock 行情与 ``app.backtest_engine``，数字全部经 FactBook 登记；
- 不改 sft_synth_v2 / eval_ood / 训练脚本，只写 ``data/sft_v3/trial30/``。

用法::

    python backend/scripts/gen_trial30.py            # 取数、生成、自检、落盘
    python backend/scripts/gen_trial30.py --verify   # 只校验已落盘文件
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for p in (str(BACKEND_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from gen_sft_synth import FactBook, check_numbers, _DATE_RE  # noqa: E402
from gen_reasoning_samples import (  # noqa: E402
    CAPITAL, SYSTEM_PROMPT, load_symbol, register_backtest,
)
import app.backtest_engine.runner as runner_mod  # noqa: E402
from app.backtest_engine import run_strategy_backtest  # noqa: E402
from app.backtest_engine.runner import STRATEGY_META  # noqa: E402
import trial30_samples as TS  # noqa: E402

OUT_DIR = BACKEND_ROOT / "data" / "sft_v3" / "trial30"
SRC_PATH = BACKEND_ROOT / "data" / "sft_reasoning_samples" / "samples.jsonl"
REUSE_IDS = ("sft-000002", "sft-000006", "sft-000010")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
SENT_SPLIT_RE = re.compile("[。；;!？]")


def cjk_len(text: str) -> int:
    return len(CJK_RE.findall(text))


def head6(text: str) -> str:
    cjk = "".join(CJK_RE.findall(text))
    return cjk[:6] if cjk else "<无汉字>"


# ============================ 样本构造 ============================
class Ctx:
    def __init__(self) -> None:
        self.sources: dict[str, dict] = {}
        self.samples: list[dict] = []

    def source(self, sid: str, payload: dict) -> None:
        self.sources.setdefault(sid, payload)

    def emit(self, *, sample_id: str, trial_scene: str, intent: str, task: str,
             subtype: str, tone: str, questions: list[str], answers: list[str],
             fb: FactBook, source_ids: list[str], params: dict, interval_key: str,
             tool_calls: list[dict], evidence: str, codes: list[str],
             extra_prov: dict | None = None) -> dict:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for q, a in zip(questions, answers):
            messages.append({"role": "user", "content": q})
            messages.append({"role": "assistant", "content": a})
        provenance = {
            "sample_id": sample_id, "intent": intent, "task": task,
            "subtype": subtype, "tone": tone, "turns": len(answers),
            "source_ids": source_ids, "params": params,
            "interval_key": interval_key,
            "facts": {k: v["value"] for k, v in fb.facts.items()},
            "fact_kinds": {k: v["kind"] for k, v in fb.facts.items()},
            "numbers": sorted(fb.allowed), "dates": sorted(fb.dates),
            "evidence": evidence,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if extra_prov:
            provenance.update(extra_prov)
        sample = {
            "sample_id": sample_id, "intent": intent, "task": task,
            "subtype": subtype, "tone": tone, "style": "reasoning",
            "trial_scene": trial_scene, "reused": False,
            "messages": messages, "tool_trace": tool_calls,
            "provenance": provenance,
        }
        assert len(answers) == len(questions)
        self.samples.append(sample)
        return sample


# ============================ 取数 ============================
async def run_bt(sym: str, strategy: str, days: int,
                 params: dict | None = None, tag: str = "") -> dict:
    result = await run_strategy_backtest(sym, strategy, params=params or {},
                                         initial_capital=CAPITAL, days=days)
    if result.get("warning"):
        raise RuntimeError(f"{sym} {strategy} {days}d 资金不足一手")
    sells = [t for t in result["trades"] if t["action"] == "SELL"]
    buys = [t for t in result["trades"] if t["action"] == "BUY"]
    fetched = len(await runner_mod.fetch_history_bars(sym, days=days + 90))
    return {
        "result": result, "metrics": result["metrics"],
        "curve": result["equity_curve"], "trades": result["trades"],
        "bars": result["bars_count"], "fetched": fetched,
        "warmup": fetched - result["bars_count"],
        "buys": len(buys), "sells": len(sells), "tag": tag,
        "start": result["equity_curve"][0]["date"],
        "end": result["equity_curve"][-1]["date"],
        "strategy_name": STRATEGY_META[strategy]["name"],
        "params": result.get("params") or {},
    }


def reg(ctx: Ctx, code: str, strategy: str, requested: int, bt: dict) -> str:
    return register_backtest(ctx, code, strategy, requested, bt, tag=bt.get("tag", ""))


# ============================ 复用三条 ============================
def load_reused() -> list[dict]:
    pool = {json.loads(x)["sample_id"]: json.loads(x)
            for x in SRC_PATH.read_text(encoding="utf-8").splitlines() if x.strip()}
    out = []
    for sid in REUSE_IDS:
        s = copy.deepcopy(pool[sid])
        s["reused"] = True
        s["trial_scene"] = {"sft-000002": "回测", "sft-000006": "风险",
                            "sft-000010": "多轮"}[sid]
        out.append(s)
    return out


# ============================ 自检 ============================
def reconstruct_fb(sample: dict) -> tuple[FactBook, set[str]]:
    prov = sample["provenance"]
    fb = FactBook()
    fb.allowed.update(float(x) for x in prov.get("numbers") or [])
    fb.dates.update(prov.get("dates") or [])
    codes: set[str] = set(prov.get("codes") or [])
    for v in (prov.get("params") or {}).values():
        if isinstance(v, str) and _DATE_RE.fullmatch(v):
            fb.dates.add(v)
        if isinstance(v, str):
            codes.update(x for x in v.split(".") if x.isdigit())
        if isinstance(v, list):
            for x in v:
                if isinstance(x, str) and _DATE_RE.fullmatch(x):
                    fb.dates.add(x)
        if isinstance(v, dict):
            for x in v.values():
                if isinstance(x, str) and _DATE_RE.fullmatch(x):
                    fb.dates.add(x)
    # 正文里出现的所有六位代码（组合/多轮会有多个）
    for a in [m["content"] for m in sample["messages"] if m["role"] == "assistant"]:
        codes.update(re.findall(r"\b(?:60|00|30|68|83|92)\d{4}\b", a))
    return fb, codes


def answers_of(sample: dict) -> list[str]:
    return [m["content"] for m in sample["messages"] if m["role"] == "assistant"]


def first_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT_RE.split(text) if s.strip()]


def paragraph_heads(text: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n+", text) if b.strip()]
    if len(blocks) >= 4:
        return [head6(first_sentences(b)[0]) for b in blocks[:4]]
    sents = first_sentences(text)
    n = len(sents)
    base, rem = divmod(n, 4)
    heads, start = [], 0
    for i in range(4):
        size = base + (1 if i < rem else 0)
        if size and start < n:
            heads.append(head6(sents[start]))
        start += size
    return heads


def verify(samples: list[dict], verbose: bool = True) -> dict:
    bad_samples: list[str] = []
    bad_detail: dict[str, list[str]] = {}
    lengths: list[int] = []
    firsts: list[str] = []
    fours: list[str] = []
    scenes = Counter(s.get("trial_scene") for s in samples)
    over: list[tuple[str, int]] = []
    multi = 0

    for s in samples:
        fb, codes = reconstruct_fb(s)
        answers = answers_of(s)
        if len(answers) > 1:
            multi += 1
        for a in answers:
            bad = check_numbers(a, fb, codes)
            if bad:
                bad_samples.append(s["sample_id"])
                bad_detail[s["sample_id"]] = sorted(set(bad))
            L = cjk_len(a)
            lengths.append(L)
            if L < 250 or L > 400:
                over.append((s["sample_id"], L))
            fs = first_sentences(a)
            if fs:
                firsts.append(head6(fs[0]))
            fours.extend(paragraph_heads(a))

    result = {
        "n": len(samples), "unregistered": len(set(bad_samples)),
        "bad_detail": bad_detail, "first_total": len(firsts),
        "first_distinct": len(set(firsts)), "first_dups": [k for k, v in
                                                           Counter(firsts).items() if v > 1],
        "four_total": len(fours), "four_distinct": len(set(fours)),
        "four_dups": [(k, v) for k, v in Counter(fours).items() if v > 1],
        "scenes": dict(scenes), "min": min(lengths), "max": max(lengths),
        "avg": round(sum(lengths) / len(lengths), 1), "over": over,
        "multi": multi,
    }
    if verbose:
        print("=== trial30 自检 ===")
        print(f"条数={result['n']}  多轮={multi}  七类={dict(scenes)}")
        print(f"unregistered={result['unregistered']}/{result['n']}  {bad_detail or ''}")
        print(f"首句（{result['first_total']} 句）distinct={result['first_distinct']}"
              f"  重复={result['first_dups'] or '无'}")
        print(f"四段首句（{result['four_total']} 句）distinct={result['four_distinct']}"
              f"  重复={result['four_dups'] or '无'}")
        print(f"汉字数 min={result['min']} max={result['max']} avg={result['avg']}"
              f"  越界={over or '无'}")
    return result


# ============================ 落盘 ============================
def write_outputs(records: list[tuple[str, dict]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "samples.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for _, s in records:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    lines = [
        "# SFT v3 试产 30 条（精写推理样本）", "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}；"
        "行情与回测均来自 baostock 日线 + app.backtest_engine 实跑，"
        "每条样本 provenance.numbers 覆盖正文全部数字（日期与代码除外）。"
        "sft-000002 / sft-000006 / sft-000010 三条原样复用，保留原 sample_id。", "",
    ]
    for i, (title, s) in enumerate(records, 1):
        ans = answers_of(s)
        lens = "+".join(str(cjk_len(a)) for a in ans)
        flag = "（复用）" if s.get("reused") else ""
        lines.append(f"## {i}. {s['trial_scene']} · {title}（{lens} 字）{flag}")
        lines.append("")
        users = [m["content"] for m in s["messages"] if m["role"] == "user"]
        for j, (u, a) in enumerate(zip(users, ans), 1):
            tag = f"（第 {j} 轮）" if len(users) > 1 else ""
            lines.append(f"**user{tag}**")
            lines.append("")
            lines.append(u)
            lines.append("")
            lines.append(f"**assistant{tag}**")
            lines.append("")
            lines.append(a)
            lines.append("")
        lines.append("---")
        lines.append("")
    (OUT_DIR / "samples.md").write_text("\n".join(lines), encoding="utf-8")


# ============================ 主流程 ============================
async def build_all() -> list[tuple[str, dict]]:
    ctx = Ctx()
    records: list[tuple[str, dict]] = []
    records += await TS.build(ctx, run_bt, reg, load_symbol)
    return records


async def main() -> int:
    args = parse_args()
    if args.verify:
        samples = [json.loads(x) for x in
                   (OUT_DIR / "samples.jsonl").read_text(encoding="utf-8").splitlines()
                   if x.strip()]
        r = verify(samples)
        missing = []
        return 0 if (r["unregistered"] == 0 and not r["over"]) else 1

    new_records = await build_all()
    reused = load_reused()
    records = reused + [s for _, s in new_records]
    titles = ["复用 #2 · sh.600036 · 均线 250 日（方法优先）",
              "复用 #6 · sh.600036 · 60 日位置与 20 日波动",
              "复用 #10 · sz.000001 · 均线 120 日 → 切尾 20/40 根实测"]
    records = list(zip(titles, records)) + new_records

    samples = [s for _, s in records]
    r = verify(samples)
    write_outputs(records)

    print("\nsource 快照：", len(TS.CTX_SOURCES) if hasattr(TS, "CTX_SOURCES") else "n/a")
    print(f"输出：{OUT_DIR / 'samples.jsonl'} / {OUT_DIR / 'samples.md'}")
    return 0 if (r["unregistered"] == 0 and not r["over"]) else 1


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
