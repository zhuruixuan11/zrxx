#!/usr/bin/env python
"""生成 70 条封版试产集（v3 trial70）。

用法::

    python backend/scripts/gen_trial70.py            # 取数、生成、自检、落盘
    python backend/scripts/gen_trial70.py --verify   # 只校验已落盘文件
"""
from __future__ import annotations

import argparse
import asyncio
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
import trial70_samples as TS  # noqa: E402

OUT_DIR = BACKEND_ROOT / "data" / "sft_v3" / "trial70"
TRIAL30 = BACKEND_ROOT / "data" / "sft_v3" / "trial30" / "samples.jsonl"
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
SENT_SPLIT_RE = re.compile("[。；;!？]")


def cjk_len(text: str) -> int:
    return len(CJK_RE.findall(text))


def head6(text: str) -> str:
    cjk = "".join(CJK_RE.findall(text))
    return cjk[:6] if cjk else "<无汉字>"


def reconstruct_fb(sample: dict) -> tuple[FactBook, set[str]]:
    prov = sample["provenance"]
    fb = FactBook()
    fb.allowed.update(float(x) for x in prov.get("numbers") or [])
    fb.dates.update(prov.get("dates") or [])
    codes: set[str] = set(prov.get("codes") or [])
    for v in (prov.get("params") or {}).values():
        if isinstance(v, str) and _DATE_RE.fullmatch(v):
            fb.dates.add(v)
        if isinstance(v, list):
            for x in v:
                if isinstance(x, str) and _DATE_RE.fullmatch(x):
                    fb.dates.add(x)
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
    base, rem = divmod(len(sents), 4)
    heads, start = [], 0
    for i in range(4):
        size = base + (1 if i < rem else 0)
        if size and start < len(sents):
            heads.append(head6(sents[start]))
        start += size
    return heads


def verify(samples: list[dict], verbose: bool = True) -> dict:
    bad_detail: dict[str, list[str]] = {}
    lengths: list[int] = []
    firsts, fours = [], []
    scenes = Counter(s.get("trial_scene") for s in samples)
    over, thin, nodisc, noshape = [], [], [], []
    multi = 0

    for s in samples:
        fb, codes = reconstruct_fb(s)
        prov = s["provenance"]
        answers = answers_of(s)
        if len(answers) > 1:
            multi += 1
        closed = prov.get("closed_trades")
        for a in answers:
            bad = check_numbers(a, fb, codes)
            if bad:
                bad_detail[s["sample_id"]] = sorted(set(bad))
            L = cjk_len(a)
            lengths.append(L)
            if L < 250 or L > 400:
                over.append((s["sample_id"], L))
            blocks = [b.strip() for b in re.split(r"\n+", a) if b.strip()]
            if len(blocks) != 4:
                thin.append((s["sample_id"], "段数"))
            else:
                if "区间" not in blocks[0]:
                    thin.append((s["sample_id"], "数据"))
                if "口径" not in blocks[1]:
                    thin.append((s["sample_id"], "怎么算"))
                if not any(k in blocks[3] for k in ("反转", "如果", "不确定", "边界")):
                    thin.append((s["sample_id"], "成立条件"))
            fs = first_sentences(a)
            if fs:
                firsts.append(head6(fs[0]))
            fours.extend(paragraph_heads(a))
        if s.get("trial_scene") in ("回测", "多轮") and closed is not None and closed < 5:
            disclosed = all(("样本不足" in a and "不能评价策略有效性" in a)
                            for a in answers)
            if not disclosed:
                nodisc.append(s["sample_id"])
        if prov.get("market_regime") not in ("单边上涨", "震荡", "急跌", "其他"):
            noshape.append(s["sample_id"])

    regimes = Counter(s["provenance"].get("market_regime") for s in samples)
    scenes_by_code: dict[str, set[str]] = {}
    for s in samples:
        sym = s["provenance"].get("params", {}).get("symbol")
        scenes_by_code.setdefault(sym, set()).add(s["trial_scene"])
    cross = {k: sorted(v) for k, v in scenes_by_code.items() if len(v) > 1}

    result = {
        "n": len(samples), "multi": multi, "scenes": dict(scenes),
        "symbols": scenes_by_code, "cross_scene": cross,
        "unregistered": len(bad_detail), "bad_detail": bad_detail,
        "first_total": len(firsts), "first_distinct": len(set(firsts)),
        "first_dups": [k for k, v in Counter(firsts).items() if v > 1],
        "four_total": len(fours), "four_distinct": len(set(fours)),
        "four_dups": [k for k, v in Counter(fours).items() if v > 1],
        "min": min(lengths), "max": max(lengths),
        "avg": round(sum(lengths) / len(lengths), 1), "over": over,
        "thin": thin, "nodisc": nodisc, "noshape": noshape,
        "regimes": dict(regimes),
        "insufficient": [s["sample_id"] for s in samples
                         if s["provenance"].get("closed_trades") is not None
                         and s["provenance"]["closed_trades"] < 5],
    }
    if verbose:
        _print_report(result, samples)
    return result


def _print_report(r: dict, samples: list[dict]) -> None:
    print("=== trial70 自检 ===")
    print(f"[1] 场景分布：{r['scenes']}  合计={r['n']}（多轮={r['multi']}）")
    trial30_syms = set()
    if TRIAL30.exists():
        for line in TRIAL30.read_text(encoding="utf-8").splitlines():
            if line.strip():
                sym = json.loads(line)["provenance"]["params"].get("symbol")
                if isinstance(sym, str):
                    trial30_syms.add(sym)
    print(f"[2] 标的数={len(r['symbols'])}  跨场景重复={r['cross_scene'] or '无'}")
    for sym, sc in sorted(r["symbols"].items(), key=lambda kv: kv[0]):
        tag = "" if sym in trial30_syms else "  [新增]"
        print(f"    {sym} {TS.NAMES.get(sym, '?')} · {'/'.join(sorted(sc))}{tag}")
    print(f"    相对 trial30 新增={len(set(r['symbols']) - trial30_syms)} 只")
    print(f"[3] unregistered={r['unregistered']}  {r['bad_detail'] or ''}")
    print(f"[4] 首句 {r['first_distinct']}/{r['first_total']} distinct"
          f"  重复={r['first_dups'] or '无'}")
    print(f"    四段首句 {r['four_distinct']}/{r['four_total']} distinct"
          f"  重复={r['four_dups'] or '无'}")
    print(f"[5] 平仓<5 的样本 {len(r['insufficient'])} 条：{r['insufficient']}")
    print(f"    未如实说明={r['nodisc'] or '无'}")
    print(f"[6] 行情形态：{r['regimes']}")
    need = ("单边上涨", "震荡", "急跌")
    print("    三类均>=8：", all(r["regimes"].get(k, 0) >= 8 for k in need))
    print(f"[7] 汉字数 min={r['min']} max={r['max']} avg={r['avg']}"
          f"  越界={r['over'] or '无'}")
    print(f"[8] 仍不合格：长度越界 {len(r['over'])} 条、未登记 {r['unregistered']} 条、"
          f"缺要素 {len(r['thin'])} 条、成交不足未说明 {len(r['nodisc'])} 条")
    if r["thin"]:
        print(f"    缺要素明细={r['thin']}")


def write_outputs(records: list[tuple[str, dict]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda kv: kv[1]["sample_id"])
    with open(OUT_DIR / "samples.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for _, s in records:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    lines = [
        "# SFT v3 试产 70 条（封版）", "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}；"
        "行情与回测来自 baostock 日线 + app.backtest_engine 实跑，"
        "每条正文数字均由 provenance.numbers 登记。", "",
    ]
    for i, (title, s) in enumerate(records, 1):
        ans = answers_of(s)
        lens = "+".join(str(cjk_len(a)) for a in ans)
        lines.append(f"## {i}. {s['sample_id']} · {s['trial_scene']} · {title}（{lens} 字）")
        lines.append("")
        users = [m["content"] for m in s["messages"] if m["role"] == "user"]
        for j, (u, a) in enumerate(zip(users, ans), 1):
            tag = f"（第 {j} 轮）" if len(users) > 1 else ""
            lines += [f"**user{tag}**", "", u, "",
                      f"**assistant{tag}**", "", a, ""]
        lines += ["---", ""]
    (OUT_DIR / "samples.md").write_text("\n".join(lines), encoding="utf-8")


async def main() -> int:
    args = parse_args()
    if args.verify:
        path = OUT_DIR / "samples.jsonl"
        samples = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
                   if x.strip()]
        r = verify(samples)
        ok = (r["unregistered"] == 0 and not r["over"] and not r["nodisc"]
              and not r["cross_scene"] and len(samples) == 70)
        print(f"验收：{'通过' if ok else '未通过'}；文件"
              f"{'存在' if (OUT_DIR / 'samples.md').exists() else '缺失'}")
        return 0 if ok else 1

    records = await TS.build_all()
    samples = [s for _, s in records]
    r = verify(samples)
    write_outputs(records)
    print(f"\n输出：{OUT_DIR / 'samples.jsonl'}（{len(records)} 行） / {OUT_DIR / 'samples.md'}")
    ok = (r["unregistered"] == 0 and not r["over"] and not r["nodisc"]
          and not r["cross_scene"] and len(records) == 70)
    return 0 if ok else 1


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    return ap.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
