#!/usr/bin/env python
"""扫描「单边上涨」候选标的（供 final80 补 10 条使用）。

判定复用 trial70 的 ``trial70_samples.regime``：区间涨跌 > +15% 且最大回撤 < 10%。
再叠加趋势条件：窗口内收盘站上 60 日均线的天数占比 >= 60%，或上涨段回撤次数 <= 3。

候选池：沪深主板（sh.60 / sz.00）+ 创业板（sz.30），默认取沪深 300 与中证 500
成分股（两者都覆盖主板与创业板），并按 ST、上市不足 300 日、日均成交额过滤。

用法::

    python backend/scripts/screen_uptrend.py --out /tmp/uptrend.json
    python backend/scripts/screen_uptrend.py --pool all --limit 400
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for p in (str(BACKEND_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import baostock as bs  # noqa: E402

from app.baostock_gateway import fetch_bars_many, fetch_rows  # noqa: E402
from trial70_samples import regime  # noqa: E402

TRIAL70 = BACKEND_ROOT / "data" / "sft_v3" / "trial70" / "samples.jsonl"
BAR_START = "2025-05-01"
BAR_END = "2026-11-06"
WINDOWS = (150, 120, 90, 60)   # 与 trial70 的 60/90/120 日样本口径一致
MA_WARMUP = 60
MIN_AVG_AMOUNT_YI = 1.0   # 20 日均成交额下限（亿元）
LISTED_BEFORE = "2025-09-01"


def _existing_symbols() -> set[str]:
    out = set()
    for line in TRIAL70.read_text(encoding="utf-8").splitlines():
        if line.strip():
            sym = json.loads(line)["provenance"]["params"].get("symbol")
            if isinstance(sym, str):
                out.add(sym)
    return out


def _index_members() -> set[str]:
    codes: set[str] = set()
    for fn in (bs.query_hs300_stocks, bs.query_zz500_stocks):
        rs = fn()
        if rs is None or rs.error_code != "0":
            continue
        while rs.next():
            row = rs.get_row_data()
            codes.add(row[1])
    return codes


def _universe(pool: str) -> list[tuple[str, str]]:
    rs = bs.query_stock_basic()
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    listed = {r[0]: r[2] for r in rows if r[4] == "1" and r[5] == "1"}
    names = {r[0]: r[1] for r in rows if r[4] == "1" and r[5] == "1"}
    codes = set(listed)
    if pool == "index":
        codes &= _index_members()
    board = re.compile(r"^(sh\.60|sz\.00|sz\.30)")
    out = []
    for c in sorted(codes):
        if not board.match(c):
            continue
        nm = names.get(c, "")
        if "ST" in nm or "退" in nm:
            continue
        if listed.get(c, "") > LISTED_BEFORE:
            continue
        out.append((c, nm))
    return out


def _ma(closes: list[float], n: int, i: int) -> float | None:
    if i + 1 < n:
        return None
    return sum(closes[i + 1 - n:i + 1]) / n


def _pullbacks(closes: list[float], threshold: float = 3.0) -> int:
    """上涨段里收盘从阶段高点回撤 >= threshold% 的次数（创新高前记一次）。"""
    peak = closes[0]
    count = 0
    in_dd = False
    for c in closes[1:]:
        if c >= peak:
            peak = c
            in_dd = False
        elif (peak - c) / peak * 100 >= threshold and not in_dd:
            count += 1
            in_dd = True
    return count


def evaluate(bars: list[dict], n: int) -> dict | None:
    if len(bars) < n:
        return None
    win = bars[-n:]
    closes_all = [b["close"] for b in bars]
    closes = [b["close"] for b in win]
    lo = len(bars) - len(win)
    above = total = 0
    for k in range(len(win)):
        i = lo + k
        ma = _ma(closes_all, MA_WARMUP, i)
        if ma is None:
            continue
        total += 1
        if closes_all[i] > ma:
            above += 1
    rg = regime(win)
    amp20 = sum(b["close"] * b["volume"] for b in win[-20:]) / 20 / 1e8
    # 路径特征：区间内最大涨幅、期末距区间高点
    peak = closes[0]
    peak_i = 0
    max_run = 0.0
    for i, c in enumerate(closes):
        if c > peak:
            peak, peak_i = c, i
        max_run = max(max_run, (c / closes[0] - 1) * 100)
    end_from_peak = (closes[-1] / peak - 1) * 100
    return {
        "ret": round(rg["ret"], 2), "mdd": round(rg["mdd"], 2),
        "amp": round(rg["amp"], 2), "reg": rg["reg"],
        "start": rg["start"], "end": rg["end"], "n": rg["n"],
        "ma60_above_pct": round(above / total * 100, 1) if total else 0.0,
        "pullbacks": _pullbacks(closes),
        "avg_amount_yi": round(amp20, 2),
        "max_runup": round(max_run, 2),
        "end_from_peak": round(end_from_peak, 2),
        "peak_day": peak_i,
    }


def _eligible(m: dict) -> bool:
    if m["reg"] != "单边上涨":
        return False
    return m["ma60_above_pct"] >= 60 or m["pullbacks"] <= 3


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", choices=("index", "all"), default="index")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--chunk", type=int, default=100)
    args = ap.parse_args()

    # 先唤醒网关会话，再调用 baostock 的股票列表接口
    await fetch_rows("sh.600000", "date,close", BAR_END, BAR_END)
    uni = _universe(args.pool)
    skip = _existing_symbols()
    uni = [(c, n) for c, n in uni if c not in skip]
    if args.limit:
        uni = uni[: args.limit]
    print(f"候选池={args.pool} 待扫描 {len(uni)} 只（已剔除 trial70 的 {len(skip)} 只）")

    names = dict(uni)
    got: dict[str, list[dict]] = {}
    for i in range(0, len(uni), args.chunk):
        chunk = [c for c, _ in uni[i:i + args.chunk]]
        res = await fetch_bars_many(chunk, BAR_START, BAR_END, timeout=300)
        if res is None:
            print("baostock 批量取数不可用，退出")
            return 2
        got.update(res)
        print(f"  取数 {min(i + args.chunk, len(uni))}/{len(uni)}，已有 {len(got)} 只")

    items = []
    for code, bars in got.items():
        for n in WINDOWS:
            m = evaluate(bars, n)
            if not m:
                continue
            m.update({"symbol": code, "name": names.get(code, ""), "window": n,
                      "ok": _eligible(m), "liquid": m["avg_amount_yi"] >= MIN_AVG_AMOUNT_YI})
            items.append(m)

    ok = [x for x in items if x["ok"] and x["liquid"]]
    ok.sort(key=lambda x: (-x["window"], -x["ret"], -x["mdd"]))
    best: dict[str, dict] = {}
    for x in ok:
        best.setdefault(x["symbol"], x)
    ok = list(best.values())
    print(f"\n通过形态+趋势+流动性：{len(ok)} 只 / 有效窗口 {len(items)} 个\n")
    print(f"{'代码':<10}{'名称':<8}{'窗口':>5}{'涨幅':>8}{'回撤':>8}{'振幅':>8}{'MA60占比':>9}{'回撤次数':>7}{'日均额':>8}")
    for x in ok:
        print(f"{x['symbol']:<10}{x['name']:<8}{x['window']:>5}{x['ret']:>8.2f}{x['mdd']:>8.2f}"
              f"{x['amp']:>8.2f}{x['ma60_above_pct']:>9.1f}{x['pullbacks']:>7}"
              f"{x['avg_amount_yi']:>8.2f}")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"generated_at": datetime.now().isoformat(timespec="seconds"),
             "pool": args.pool, "universe": len(uni), "scanned": len(items),
             "ok": ok, "all": items},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
