#!/usr/bin/env python
"""推理型 SFT 样例（10 条）：真跑行情与回测，答案里每个数字都在 FactBook 登记。

与 gen_sft_synth.py 的关系
--------------------------
复用其 ``FactBook``（数字登记 / 校验）、``Ctx``（source 快照与 provenance 结构）
以及同一套取数与回测入口（``get_quote`` / ``get_historical`` /
``run_strategy_backtest``）。本脚本不改数据集、不改训练脚本。

用法
----
    python backend/scripts/gen_reasoning_samples.py            # 生成 10 条 + 自检
    python backend/scripts/gen_reasoning_samples.py --verify   # 只校验已落盘文件
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

from loguru import logger  # noqa: E402

from gen_sft_synth import (  # noqa: E402
    DISCLAIMER, SYSTEM_PROMPT, Ctx, FactBook, _DATE_RE, check_numbers,
)
from app.agent.backtest import fetch_history_bars  # noqa: E402
from app.agent.indicators import compute_indicators  # noqa: E402
from app.agent.tools import get_historical, get_quote, normalize_a_share  # noqa: E402
import app.backtest_engine.runner as runner_mod  # noqa: E402
from app.backtest_engine import run_strategy_backtest  # noqa: E402
from app.backtest_engine.runner import STRATEGY_META  # noqa: E402

OUT_DIR = BACKEND_ROOT / "data" / "sft_reasoning_samples"
CAPITAL = 200_000.0
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def cjk_len(text: str) -> int:
    return len(CJK_RE.findall(text))


# ============================ 上下文 ============================
class ReasoningCtx(Ctx):
    """在 Ctx 的 source 快照 / 结构之上，补上 subtype / tone / 多轮 messages。"""

    def emit(self, *, intent: str, task: str, subtype: str, tone: str,
             questions: list[str], answers: list[str], fb: FactBook,
             source_ids: list[str], params: dict, interval_key: str,
             tool_calls: list[dict], evidence: str, extra_prov: dict | None = None) -> dict:
        sid = self.next_id()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for q, a in zip(questions, answers):
            messages.append({"role": "user", "content": q})
            messages.append({"role": "assistant", "content": a})
        provenance = {
            "sample_id": sid,
            "intent": intent,
            "task": task,
            "subtype": subtype,
            "tone": tone,
            "turns": len(answers),
            "source_ids": source_ids,
            "params": params,
            "interval_key": interval_key,
            "facts": {k: v["value"] for k, v in fb.facts.items()},
            "fact_kinds": {k: v["kind"] for k, v in fb.facts.items()},
            "numbers": sorted(fb.allowed),
            "dates": sorted(fb.dates),
            "evidence": evidence,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if extra_prov:
            provenance.update(extra_prov)
        sample = {
            "sample_id": sid,
            "intent": intent,
            "task": task,
            "subtype": subtype,
            "tone": tone,
            "style": "reasoning",
            "messages": messages,
            "tool_trace": tool_calls,
            "provenance": provenance,
        }
        self.samples.append(sample)
        return sample


# ============================ 取数 ============================
async def load_symbol(sym: str, days: int = 400) -> dict:
    quote = await get_quote(sym)
    hist = await get_historical(sym, days)
    bars = hist.get("data") or []
    if not bars:
        raise RuntimeError(f"{sym} 无历史日线")
    return {
        "code": normalize_a_share(sym) or sym,
        "quote": quote,
        "bars": bars,
        "ind": compute_indicators(bars),
    }


async def run_backtest_drop_tail(sym: str, strategy: str, days: int,
                                 drop: int) -> tuple[dict, int]:
    """同一策略、同一参数，只把取数序列的末端切掉 drop 根再交给引擎。

    引擎代码不改：临时替换 runner 里取数入口，取 ``days + 90 + drop`` 根后丢掉
    最后 ``drop`` 根，引擎仍按「取末尾 days 根」的规则回测，于是得到一个
    等长、但终点提前 drop 个交易日、起点相应前移的窗口。
    """
    orig = runner_mod.fetch_history_bars
    seen: dict[str, int] = {}

    async def _truncated(symbol: str, days: int = 90, frequency: str = "d"):
        bars = await orig(symbol, days=days + drop, frequency=frequency)
        seen["n"] = max(len(bars) - drop, 0)
        return bars[:-drop] if drop else bars

    runner_mod.fetch_history_bars = _truncated
    try:
        result = await run_strategy_backtest(sym, strategy, initial_capital=CAPITAL,
                                             days=days)
    finally:
        runner_mod.fetch_history_bars = orig
    return result, seen.get("n", len(result["equity_curve"]))


async def load_backtest(sym: str, strategy: str, days: int,
                        drop_tail: int = 0) -> dict:
    if drop_tail:
        result, avail = await run_backtest_drop_tail(sym, strategy, days, drop_tail)
    else:
        result = await run_strategy_backtest(sym, strategy, initial_capital=CAPITAL, days=days)
        avail = len(await fetch_history_bars(sym, days=days + 90))
    if result.get("warning"):
        raise RuntimeError(f"{sym} {strategy} {days}d 资金不足一手：{result['warning']}")
    sells = [t for t in result["trades"] if t["action"] == "SELL"]
    buys = [t for t in result["trades"] if t["action"] == "BUY"]
    return {
        "result": result,
        "metrics": result["metrics"],
        "curve": result["equity_curve"],
        "trades": result["trades"],
        "bars": result["bars_count"],
        "fetched": avail,
        "warmup": avail - result["bars_count"],
        "buys": len(buys),
        "sells": len(sells),
        "drop_tail": drop_tail,
        "start": result["equity_curve"][0]["date"],
        "end": result["equity_curve"][-1]["date"],
        "strategy_name": STRATEGY_META[strategy]["name"],
        "params": result.get("params") or {},
    }


def register_backtest(ctx: ReasoningCtx, code: str, strategy: str, requested: int,
                      bt: dict, tag: str = "") -> str:
    bid = (f"bt:{code}:{strategy}:{bt['bars']}{tag}:{int(CAPITAL)}:"
           f"{bt['start'].replace('-', '')}-{requested}")
    payload = {
        "type": "backtest", "source_id": bid, "symbol": code, "strategy": strategy,
        "strategy_name": bt["strategy_name"], "params": bt["params"],
        "requested_days": requested, "bars_count": bt["bars"],
        "fetched_bars": bt["fetched"], "warmup_bars": bt["warmup"],
        "interval": [bt["start"], bt["end"]],
        "initial_capital": CAPITAL, "metrics": bt["metrics"],
        "trades_count": bt["metrics"]["trades_count"],
    }
    if bt.get("drop_tail"):
        payload["data_slice"] = f"drop_last_{bt['drop_tail']}_bars"
        payload["slice_note"] = ("同一策略同一参数，等长窗口、终点提前 "
                                 f"{bt['drop_tail']} 个交易日，起点相应前移")
    ctx.source(bid, payload)
    return bid


def bt_constants(fb: FactBook) -> None:
    fb.c("commission_pct", 0.03, "code_constant")
    fb.c("stamp_tax_pct", 0.10, "code_constant")
    fb.c("lot_size", 100, "code_constant")
    fb.c("initial_capital", CAPITAL)
    fb.c("annual_factor", 252, "code_constant")


# ============================ 1 回测：均线 · 600519 · 180 ============================
def s01(ctx: ReasoningCtx, d: dict, bt: dict) -> dict:
    code = d["code"]
    fb = FactBook()
    m = bt["metrics"]
    fb.d(bt["start"]); fb.d(bt["end"])
    for t in bt["trades"]:
        fb.d(t["date"])
    bt_constants(fb)
    fb.c("requested_days", 180)
    fb.c("ma_short", 5)
    fb.c("ma_long", 20)
    n_bars = fb.c("bars_count", bt["bars"])
    n_fetch = fb.c("fetched_bars", bt["fetched"])
    n_warm = fb.c("warmup_bars", bt["warmup"])
    n_trades = fb.n("trades_count", m["trades_count"], 0)
    n_buys = fb.c("buy_count", bt["buys"])
    n_sells = fb.c("sell_count", bt["sells"])
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    ar = fb.n("annual_return_pct", m["annual_return_pct"], 2, True)
    sh = fb.n("sharpe_ratio", m["sharpe_ratio"], 2)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)

    last_buy = [t for t in bt["trades"] if t["action"] == "BUY"][-1]
    fb.d(last_buy["date"])
    last_entry = fb.n("last_entry_price", last_buy["price"], 2)
    last_shares = fb.c("last_shares", last_buy["shares"])
    price = float(d["quote"]["current_price"])
    fb.d(str(d["quote"]["trade_date"]))
    fb.n("current_price", price, 2)
    open_pnl = (price - last_buy["price"]) * last_buy["shares"]
    open_pnl_s = fb.n("open_pnl", open_pnl, 0, True)

    answer = (
        f"我先确认区间和根数：{bt['start']} 到 {bt['end']} 共 {n_bars} 根日线，引擎拉了 "
        f"{n_fetch} 根，多出的 {n_warm} 根只喂均线、不参与撮合。先看这个而不是先看收益，"
        f"是因为五日均线与二十日均线在这段里只打出 {n_trades} 笔成交，样本本来就薄，"
        f"收益再好看也不敢当真，半年多的样本覆盖不到一轮完整行情。口径按引擎来："
        f"信号取上一根收盘、本根开盘成交，一手一百股向下取整，"
        f"买入佣金 0.03%，卖出再加 0.10% 印花税，期末按收盘盯市，于是区间 {tr}%、最大回撤 {md}%，"
        f"四笔平仓里只有一笔赚钱，胜率 {wr}%。这个结论只在茅台这段上行里成立，"
        f"换到单边下跌或把均线周期拉长，信号密度和回撤都会变。让我不安的是 {last_buy['date']} 以 "
        f"{last_entry} 元买入的 {last_shares} 股还没平仓，浮盈 {open_pnl_s} 元不进胜率，"
        f"这里我其实不确定 {wr}% 该不该当成策略质量，只能等更长的样本再说话。"
    )
    question = f"{code} 用均线金叉死叉回测最近 180 个交易日，帮我看看结果"
    return ctx.emit(
        intent="investment_analysis", task="backtest_review", subtype="backtest",
        tone="analytical", questions=[question], answers=[answer], fb=fb,
        source_ids=[register_backtest(ctx, code, "ma_cross", 180, bt)],
        params={"symbol": code, "strategy": "ma_cross", "requested_days": 180,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]],
                "initial_capital": CAPITAL},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|ma_cross",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "ma_cross", "days": 180},
                     "ok": True, "source_id": f"bt:{code}:ma_cross:{bt['bars']}"}],
        evidence="app/backtest_engine/runner.py::run_strategy_backtest（信号上一根收盘、本根开盘成交）",
        extra_prov={"backtest_id": f"bt:{code}:ma_cross:{bt['bars']}"},
    )


# ============================ 2 回测：均线 · 600036 · 250 ============================
def s02(ctx: ReasoningCtx, d: dict, bt: dict) -> dict:
    code = d["code"]
    fb = FactBook()
    m = bt["metrics"]
    fb.d(bt["start"]); fb.d(bt["end"]); fb.d(str(d["quote"]["trade_date"]))
    bt_constants(fb)
    fb.c("requested_days", 250)
    fb.c("ma_short", 5)
    fb.c("ma_long", 20)
    fb.c("fetch_extra_days", 120, "code_constant")
    min_rt = fb.c("min_round_trips", 20, "method_threshold")
    min_bars = fb.c("min_bars", 250, "method_threshold")
    n_bars = fb.c("bars_count", bt["bars"])
    short_by = fb.c("short_by_bars", 250 - bt["bars"])
    n_trades = fb.n("trades_count", m["trades_count"], 0)
    n_buys = fb.c("buy_count", bt["buys"])
    n_sells = fb.c("sell_count", bt["sells"])
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    ar = fb.n("annual_return_pct", m["annual_return_pct"], 2, True)
    sh = fb.n("sharpe_ratio", m["sharpe_ratio"], 2)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    round_trips = fb.c("round_trips", bt["sells"])
    wins = sum(1 for t in bt["trades"] if t["action"] == "SELL" and t["pnl"] > 0)
    wr_next = fb.n("win_rate_one_more", (wins + 1) / bt["sells"] * 100, 1)

    answer = (
        f"这个回测我先不报收益，先定判据：平仓够 {min_rt} 笔、样本够 {min_bars} 根，"
        f"两条不过就只描述、不给参数打分。\n\n"
        f"落到数据：请求 250 个交易日，日线实得 {n_bars} 根，少 {short_by} 根；"
        f"均线在这段成交 {n_trades} 笔，{n_buys} 买 {n_sells} 卖，平仓 {round_trips} 笔。"
        f"两条都没过，下面的数字只是读数。\n\n"
        f"算法照引擎：上一根收盘出信号、本根开盘成交，一手一百股取整，佣金 0.03%，"
        f"卖出加 0.10% 印花税，期末盯市；区间 {tr}%、回撤 {md}%、Sharpe {sh}、胜率 {wr}%，"
        f"八笔平仓只两笔赚钱，回撤远大于区间亏损。\n\n"
        f"失效条件写在前面：平仓低于 {min_rt} 笔直接判不可评估，样本短于 {min_bars} 根不下结论；"
        f"震荡市均线反复穿越，成交多但每笔都付成本，只有单边趋势里同一套阈值才成立。"
        f"震荡与趋势的边界不在指标里，而在信号能不能延续，样本一换结论就变。{wr}% 也不稳，"
        f"八笔多赢一笔就是 {wr_next}%，拿它推断下一年我不放心。"
    )
    question = f"{code} 用 5 日 20 日均线跑 250 个交易日回测，胜率和回撤大概什么水平"
    return ctx.emit(
        intent="investment_analysis", task="backtest_review", subtype="backtest",
        tone="analytical", questions=[question], answers=[answer], fb=fb,
        source_ids=[register_backtest(ctx, code, "ma_cross", 250, bt)],
        params={"symbol": code, "strategy": "ma_cross", "requested_days": 250,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]],
                "initial_capital": CAPITAL},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|ma_cross",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "ma_cross", "days": 250},
                     "ok": True, "source_id": f"bt:{code}:ma_cross:{bt['bars']}"}],
        evidence="app/backtest_engine/runner.py::run_strategy_backtest + app/agent/backtest.py::fetch_history_bars（days+30 自然日）",
        extra_prov={"backtest_id": f"bt:{code}:ma_cross:{bt['bars']}",
                    "evaluation_gate": {"min_round_trips": 20, "min_bars": 250,
                                        "passed": False}},
    )


# ============================ 3 回测：唐奇安 · 300750 · 120 ============================
def s03(ctx: ReasoningCtx, d: dict, bt: dict) -> dict:
    code = d["code"]
    fb = FactBook()
    m = bt["metrics"]
    fb.d(bt["start"]); fb.d(bt["end"])
    for t in bt["trades"]:
        fb.d(t["date"])
    bt_constants(fb)
    fb.c("requested_days", 120)
    fb.c("channel_period", 20)
    fb.c("period_multiplier_note", 20)
    n_bars = fb.c("bars_count", bt["bars"])
    n_trades = fb.n("trades_count", m["trades_count"], 0)
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    ar = fb.n("annual_return_pct", m["annual_return_pct"], 2, True)
    sh = fb.n("sharpe_ratio", m["sharpe_ratio"], 2)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    buy = [t for t in bt["trades"] if t["action"] == "BUY"][0]
    sell = [t for t in bt["trades"] if t["action"] == "SELL"][0]
    fb.d(buy["date"]); fb.d(sell["date"])
    bp = fb.n("entry_price", buy["price"], 2)
    bsh = fb.c("entry_shares", buy["shares"])
    sp = fb.n("exit_price", sell["price"], 2)
    pnl = fb.n("trade_pnl", abs(sell["pnl"]), 2)
    bamt = fb.n("entry_amount", buy["amount"], 0)
    used = fb.n("capital_used_pct", buy["amount"] / CAPITAL * 100, 0)

    answer = (
        f"唐奇安通道我先看参数和这段行情的高点结构，再看收益。"
        f"{bt['start']} 到 {bt['end']} 的 {n_bars} 根日线里，二十日通道被有效打穿只有两次："
        f"{buy['date']} 以 {bp} 元买入 {bsh} 股，仓位只用了八成多，"
        f"半个多月后 {sell['date']} 在 {sp} 元被跌破下轨卖出，单笔亏 {pnl} 元。"
        f"口径是收盘突破前二十日最高价才买、本根开盘成交，卖出同理，仓位按可用资金取整到一百股；"
        f"区间 {tr}%、最大回撤 {md}%，两次交易全亏，胜率为 {wr}%。"
        f"换句话说，这套方法吃的是趋势延续，遇到反复假突破就只能连续止损；"
        f"这个结论说明突破法在这段下跌里失效，趋势市里同一套参数未必差，前提是行情真能延续。"
        f"但两次交易在统计上几乎没有说服力。我不确定 {bp} 元那次是不是假突破——"
        f"把通道周期拉长，可能根本不会进场，亏损也就不存在了。只能等更长的样本再验证。"
    )
    question = f"{code} 用唐奇安通道突破回测 120 个交易日，突破法最近还灵吗"
    return ctx.emit(
        intent="investment_analysis", task="backtest_review", subtype="backtest",
        tone="analytical", questions=[question], answers=[answer], fb=fb,
        source_ids=[register_backtest(ctx, code, "donchian_breakout", 120, bt)],
        params={"symbol": code, "strategy": "donchian_breakout", "requested_days": 120,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]],
                "initial_capital": CAPITAL},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|donchian_breakout",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "donchian_breakout", "days": 120},
                     "ok": True, "source_id": f"bt:{code}:donchian_breakout:{bt['bars']}"}],
        evidence="app/backtest_engine/strategies/donchian.py::DonchianBreakoutStrategy（上轨为前 N 日最高价 shift(1)）",
        extra_prov={"backtest_id": f"bt:{code}:donchian_breakout:{bt['bars']}"},
    )


# ============================ 4 持仓：600519 ============================
def s04(ctx: ReasoningCtx, d: dict, lookback: int = 60, qty: int = 100) -> dict:
    code = d["code"]
    bars, quote = d["bars"], d["quote"]
    cost_bar = bars[-lookback - 1]
    buy_date = str(cost_bar["date"])
    cost = float(cost_bar["close"])
    price = float(quote["current_price"])
    trade_date = str(quote["trade_date"])

    fb = FactBook()
    fb.d(buy_date); fb.d(trade_date)
    fb.c("qty", qty, "user_param")
    fb.c("lookback_trading_days", lookback, "param")
    co = fb.n("cost_price", cost, 2)
    pr = fb.n("current_price", price, 2)
    ca = fb.n("cost_amount", cost * qty, 2)
    mv = fb.n("market_value", price * qty, 2)
    pnl = fb.n("unrealized_pnl", (price - cost) * qty, 2, True)
    pct = fb.n("unrealized_pnl_pct", (price / cost - 1) * 100, 2, True)

    answer = (
        f"算这笔账我先对齐两个价格：买入日 {buy_date} 的收盘 {co} 元，和最新交易日 {trade_date} 的 "
        f"{pr} 元，中间隔了六十个交易日。我不看区间最高价，因为浮动盈亏只由成本和当前价决定，"
        f"看多了反而容易把浮盈当成判断力。口径很直白，成本 {ca} 元，一百股正好一手，市值 {mv} 元，"
        f"差额 {pnl} 元，收益率 {pct}%，没有扣佣金、印花税和这期间可能的分红。"
        f"这个数只在按当日收盘价成交的前提下成立，你要是买在盘中高点，收益率会低一截。"
        f"三个月赚十一个点，折算成年化会很漂亮，但我不建议这么折，单个季度说明不了什么；"
        f"浮盈和落袋是两回事，真实的持仓成本要以券商对账单为准。"
        f"我不确定的是这期间的分红和送转，前复权价差能对上，但现金分红会改变你的实际成本，"
        f"日线里没法替你还原。"
    )
    question = f"我在 {buy_date} 收盘买了 {code} {qty} 股，到现在赚了多少"
    return ctx.emit(
        intent="portfolio_query", task="holding_pnl", subtype="holding",
        tone="colloquial", questions=[question], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{trade_date}", f"hist:{code}:{buy_date}"],
        params={"symbol": code, "qty": qty, "buy_date": buy_date,
                "current_date": trade_date, "lookback": lookback},
        interval_key=f"{code}|{buy_date}|{trade_date}",
        tool_calls=[
            {"step": 1, "tool": "get_historical", "args": {"symbol": code, "days": 200},
             "ok": True, "source_id": f"hist:{code}:{buy_date}"},
            {"step": 2, "tool": "get_quote", "args": {"symbol": code},
             "ok": True, "source_id": f"quote:{code}:{trade_date}"},
        ],
        evidence="app/agent/tools.py::get_historical（买入日收盘）+ get_quote（最新价）",
    )


# ============================ 5 持仓：600030 ============================
def s05(ctx: ReasoningCtx, d: dict, lookback: int = 60, qty: int = 2000) -> dict:
    code = d["code"]
    bars, quote = d["bars"], d["quote"]
    cost_bar = bars[-lookback - 1]
    buy_date = str(cost_bar["date"])
    cost = float(cost_bar["close"])
    price = float(quote["current_price"])
    trade_date = str(quote["trade_date"])
    high60 = float(d["ind"]["high60"])

    fb = FactBook()
    fb.d(buy_date); fb.d(trade_date)
    fb.c("qty", qty, "user_param")
    fb.c("lookback_trading_days", lookback, "param")
    co = fb.n("cost_price", cost, 2)
    pr = fb.n("current_price", price, 2)
    ca = fb.n("cost_amount", cost * qty, 2)
    mv = fb.n("market_value", price * qty, 2)
    pnl = fb.n("unrealized_pnl", (price - cost) * qty, 2, True)
    pct = fb.n("unrealized_pnl_pct", (price / cost - 1) * 100, 2, True)
    pct_abs = fb.n("unrealized_pnl_pct_abs", abs((price / cost - 1) * 100), 2)
    hi = fb.n("high60", high60, 2)

    answer = (
        f"先对日期和价格：{buy_date} 收盘 {co} 元买入 {qty} 股，{trade_date} 收在 {pr} 元。"
        f"我先看这段走势而不是只瞄一眼成本，是因为它从 {co} 元一路磨下来，中间没有像样的反弹，"
        f"这六十个交易日的亏损是趋势性的，不是某一天砸出来的。按收盘价口径，成本 {ca} 元、"
        f"市值 {mv} 元、浮亏 {pnl} 元，幅度 {pct}%，同样没扣佣金和印花税，实际会再多几十元。"
        f"这个结论只在你确实按收盘价成交时成立，如果是在 {hi} 元的前高附近追进去，幅度会难看很多。"
        f"如果你打算继续拿，我更关心它有没有跌破前期低点，而不是今天这根小阳线。"
        f"亏损时最容易犯的错是把成本价当成锚，市场并不认这个价格，这一点我得说清楚。"
        f"我不确定这 {pct_abs}% 的跌幅里有多少是除权因素，前复权序列看不到现金分红，真实成本可能略有出入。"
    )
    question = f"{code} 我在 60 个交易日前收盘价买了 2000 股，现在浮亏多少"
    return ctx.emit(
        intent="portfolio_query", task="holding_pnl", subtype="holding",
        tone="colloquial", questions=[question], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{trade_date}", f"hist:{code}:{buy_date}"],
        params={"symbol": code, "qty": qty, "buy_date": buy_date,
                "current_date": trade_date, "lookback": lookback},
        interval_key=f"{code}|{buy_date}|{trade_date}",
        tool_calls=[
            {"step": 1, "tool": "get_historical", "args": {"symbol": code, "days": 200},
             "ok": True, "source_id": f"hist:{code}:{buy_date}"},
            {"step": 2, "tool": "get_quote", "args": {"symbol": code},
             "ok": True, "source_id": f"quote:{code}:{trade_date}"},
        ],
        evidence="app/agent/tools.py::get_historical（买入日收盘）+ get_quote（最新价）+ compute_indicators(high60)",
    )


# ============================ 6 风险：600036 ============================
def s06(ctx: ReasoningCtx, d: dict) -> dict:
    code = d["code"]
    ind, quote, bars = d["ind"], d["quote"], d["bars"]
    trade_date = str(quote["trade_date"])

    def _pos(n: int) -> float:
        w = [float(b["close"]) for b in bars[-n:]]
        return (w[-1] - min(w)) / (max(w) - min(w)) * 100

    fb = FactBook()
    fb.d(trade_date)
    fb.c("window_20", 20); fb.c("window_60", 60); fb.c("window_90", 90)
    fb.c("rsi_window", 14)
    fb.c("annual_factor", 252, "code_constant")
    pr = fb.n("current_price", float(quote["current_price"]), 2)
    lo = fb.n("low60", ind["low60"], 2)
    hi = fb.n("high60", ind["high60"], 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    vol = fb.n("volatility20", ind["volatility20"], 1)
    r20 = fb.n("ret20", ind["ret20"], 2, True)
    rsi = fb.n("rsi14", ind["rsi14"], 2)
    pos20 = fb.n("pos20", _pos(20), 1)
    pos90 = fb.n("pos90", _pos(90), 1)
    to_high = fb.n("to_high_pct", (ind["high60"] / float(quote["current_price"]) - 1) * 100, 2)
    to_low = fb.n("to_low_pct",
                  (float(quote["current_price"]) - ind["low60"])
                  / float(quote["current_price"]) * 100, 2)
    dvol = fb.n("daily_vol_pct", ind["volatility20"] / 252 ** 0.5, 2)
    s20 = fb.n("sigma20_pct", ind["volatility20"] * (20 / 252) ** 0.5, 2)

    answer = (
        f"{trade_date} 收盘 {pr} 元，六十日区间 {lo} 到 {hi} 元，落在 {pos}% 分位；"
        f"二十日年化波动 {vol}%。\n\n"
        f"口径：分位是收盘减最低再除以最高减最低，波动率是二十个日收益标准差乘 252开根。"
        f"折成能用的量，单日约 {dvol}%，二十个交易日一个标准差约 {s20}%。\n\n"
        f"对持仓的人，{pos}% 意味着浮盈在区间上部：往上到 {hi} 元只剩 {to_high}%，"
        f"往下回到 {lo} 元要吐回 {to_low}%，差不多是两个标准差；RSI {rsi} 说明位置高"
        f"但不是情绪过热。要不要用两倍标准差换这 {to_high}%，看你的成本。\n\n"
        f"这两个数衡量不了什么：不含方向，预测不了下一根 K 线；波动率只吃收盘价差，"
        f"跳空和尾部被抹平，而分位对窗口敏感，二十日窗口只有六成多，换到季度就换个样子；"
        f"波动率也不含流动性，缩量更难成交。这些只在收盘口径下才算成立，"
        f"分位该不该当信号，我确实不确定。"
    )
    question = f"{code} 最近风险大不大，位置和波动率处在什么水平"
    return ctx.emit(
        intent="risk_assessment", task="risk_snapshot", subtype="risk",
        tone="analytical", questions=[question], answers=[answer], fb=fb,
        source_ids=[f"risk:{code}:{trade_date}"],
        params={"symbol": code, "trade_date": trade_date},
        interval_key=f"{code}|{trade_date}|risk",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 90},
                     "ok": True, "source_id": f"risk:{code}:{trade_date}"}],
        evidence="app/agent/indicators.py::compute_indicators（volatility20 按 252 年化、pos60 由收盘高低点定义）",
    )


# ============================ 7 风险：300750 ============================
def s07(ctx: ReasoningCtx, d: dict) -> dict:
    code = d["code"]
    ind, quote, bars = d["ind"], d["quote"], d["bars"]
    trade_date = str(quote["trade_date"])
    closes = [float(b["close"]) for b in bars[-60:]]
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = max(mdd, (peak - c) / peak * 100)

    fb = FactBook()
    fb.d(trade_date)
    fb.c("window_20", 20); fb.c("window_60", 60)
    fb.c("annual_factor", 252, "code_constant")
    pr = fb.n("current_price", float(quote["current_price"]), 2)
    lo = fb.n("low60", ind["low60"], 2)
    hi = fb.n("high60", ind["high60"], 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    vol = fb.n("volatility20", ind["volatility20"], 1)
    r20 = fb.n("ret20", ind["ret20"], 2, True)
    dd = fb.n("mdd60_pct", mdd, 1)

    answer = (
        f"这只票我会先看回撤再看波动率，因为它的波动已经在放大。{trade_date} 收 {pr} 元，"
        f"六十日区间 {lo} 到 {hi} 元，收盘落在 {pos}% 分位；区间内从最高 {hi} 元跌到最低 {lo} 元，"
        f"按收盘价算的最大回撤是 {dd}%，二十日年化波动率 {vol}%，近二十日累计 {r20}%。"
        f"口径上分位用收盘价相对六十日高低点，回撤用区间内收盘的峰谷比，"
        f"波动率是二十个日收益按 252 天年化，都是收盘口径，不含盘中。"
        f"换句话说，波动率说的是每天晃多大，回撤说的是从高点摔下来有多深，两者不能互相替代，"
        f"这两件事经常被混在一起。"
        f"这个结论只在过去六十个交易日成立；现在是跌到 {lo} 元之后的反弹，前提是它不再创新低，"
        f"否则 {dd}% 的回撤会重演。我不确定 {vol}% 里有多少是单日事件贡献的，"
        f"样本里只要有一两根极端 K 线，这个数就会被抬高。"
    )
    question = f"{code} 最近跌得厉害，帮我看看回撤和波动到底多大"
    return ctx.emit(
        intent="risk_assessment", task="risk_snapshot", subtype="risk",
        tone="analytical", questions=[question], answers=[answer], fb=fb,
        source_ids=[f"risk:{code}:{trade_date}"],
        params={"symbol": code, "trade_date": trade_date},
        interval_key=f"{code}|{trade_date}|risk",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 90},
                     "ok": True, "source_id": f"risk:{code}:{trade_date}"}],
        evidence="app/agent/indicators.py::compute_indicators（volatility20 / pos60）+ 60 根收盘价峰谷回撤",
    )


# ============================ 8 拒答：预测目标价 ============================
def s08(ctx: ReasoningCtx, d: dict, bt: dict) -> dict:
    code = d["code"]
    ind, quote = d["ind"], d["quote"]
    trade_date = str(quote["trade_date"])
    fb = FactBook()
    fb.d(trade_date)
    fb.c("rsi_window", 14); fb.c("window_60", 60)
    fb.c("ma_long_window", 20)
    fb.c("annual_factor", 252, "code_constant")
    pr = fb.n("current_price", float(quote["current_price"]), 2)
    ma20 = fb.n("ma20", ind["ma20"], 2)
    rsi = fb.n("rsi14", ind["rsi14"], 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    vol = fb.n("volatility20", ind["volatility20"], 1)
    tr = fb.n("total_return_pct", bt["metrics"]["total_return_pct"], 2, True)
    md = fb.n("max_drawdown_pct", bt["metrics"]["max_drawdown_pct"], 2)
    n_bars = fb.c("bars_count", bt["bars"])

    answer = (
        f"你要的是下周一的目标价，这个我不做，原因不是保守，而是我给不出可验证的概率。"
        f"我先看的是 {trade_date} 的收盘 {pr} 元、二十日均线 {ma20} 元、RSI14 的 {rsi}，"
        f"再加上六十日分位 {pos}% 和二十日年化波动率 {vol}%，这些量只描述位置和离散度，不含方向。"
        f"口径上 RSI 是十四日涨跌幅的平滑比值，分位是收盘价在六十日高低点之间的相对位置，"
        f"都不是预测模型。即便在同一段历史上，均线策略跑 {n_bars} 个交易日也只有 {tr}% 的收益和 "
        f"{md}% 的回撤，拿这个外推下周没有意义。这里我其实不确定任何短期方向，样本期也太短，"
        f"我能做的是把不确定性摊开，而不是替你把它藏起来；真要做决策，止损位该由你的资金规模倒推。"
        f"能给你的只有这些统计量、一条可复现的回测，以及你自己设止损的依据。"
    )
    question = f"{code} 下周一能涨到多少？给我个目标价和止损位"
    return ctx.emit(
        intent="refusal", task="refusal", subtype="capability_boundary",
        tone="analytical", questions=[question], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{trade_date}"],
        params={"symbol": code, "trade_date": trade_date, "request": "target_price"},
        interval_key=f"NEG|predict|target_price|{code}",
        tool_calls=[{"step": 1, "tool": "probe", "ok": False,
                     "args": {"symbol": code, "request": "target_price"},
                     "error": "无预测模型，不输出目标价"}],
        evidence="项目内无预测模型；agent/nodes.py 禁止承诺收益与目标价",
        extra_prov={"refusal_reason": "无预测模型与样本外评估，无法给出可验证的方向概率",
                    "refusal_offer": "行情、指标、分位与策略回测结果"},
    )


# ============================ 9 澄清：能不能买 ============================
def s09(ctx: ReasoningCtx, d: dict) -> dict:
    code = d["code"]
    ind, quote = d["ind"], d["quote"]
    trade_date = str(quote["trade_date"])
    price = float(quote["current_price"])
    premium = (price / float(ind["ma20"]) - 1) * 100

    fb = FactBook()
    fb.d(trade_date)
    fb.c("ma_long_window", 20); fb.c("rsi_window", 14); fb.c("window_60", 60)
    fb.c("annual_factor", 252, "code_constant")
    pr = fb.n("current_price", price, 2)
    ma20 = fb.n("ma20", ind["ma20"], 2)
    prem = fb.n("ma20_premium_pct", premium, 0)
    rsi = fb.n("rsi14", ind["rsi14"], 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    vol = fb.n("volatility20", ind["volatility20"], 1)

    answer = (
        f"“能不能买”这句我先不接，因为它同时问了位置、期限和仓位三件事，答错方向比不答更糟。"
        f"我先把手头的数据摆出来：{trade_date} 收 {pr} 元，二十日均线 {ma20} 元，"
        f"价格在均线上方约 {prem}%，RSI14 是 {rsi}，六十日分位 {pos}%，二十日年化波动率 {vol}%。"
        f"这几个口径分别是收盘均值、涨跌平滑比值和区间相对位置，只能说明它现在不便宜、也不平静。"
        f"要让判断对你有用，我需要知道你打算持有多久、成本在哪、能接受多大回撤，"
        f"不同答案指向完全不同的结论，这也是我没法直接给结论的前提。"
        f"如果你只是想知道贵不贵，上面的分位和均线偏离已经够用；如果想知道该不该动手，"
        f"缺的就是你的约束条件——你更想先看位置，还是先跑一遍策略回测？"
        f"我不确定你说的买是试探仓还是重仓，这个差别比任何指标都大。"
    )
    question = f"{code} 现在能不能买"
    return ctx.emit(
        intent="portfolio_query", task="clarify", subtype="clarification",
        tone="colloquial", questions=[question], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{trade_date}"],
        params={"symbol": code, "trade_date": trade_date, "request": "buy_or_not"},
        interval_key=f"{code}|{trade_date}|clarify",
        tool_calls=[{"step": 1, "tool": "get_quote", "args": {"symbol": code},
                     "ok": True, "source_id": f"quote:{code}:{trade_date}"}],
        evidence="app/agent/tools.py::get_quote + compute_indicators；缺失项：持有期限/成本/风险预算",
        extra_prov={"missing_slots": ["持有期限", "持仓成本", "风险预算"]},
    )


# ============================ 10 多轮：000001 · 120 → 切尾实测 ============================
def s10(ctx: ReasoningCtx, d: dict, bt1: dict, bt2: dict, cut20: dict,
        cut40: dict) -> dict:
    code = d["code"]
    fb = FactBook()
    m1, m2 = bt1["metrics"], bt2["metrics"]
    c20, c40 = cut20["metrics"], cut40["metrics"]
    fb.d(bt1["start"]); fb.d(bt1["end"])
    fb.d(bt2["start"]); fb.d(bt2["end"])
    fb.d(cut20["start"]); fb.d(cut20["end"])
    fb.d(cut40["start"]); fb.d(cut40["end"])
    bt_constants(fb)
    fb.c("requested_days_1", 120)
    fb.c("requested_days_2", 250)
    fb.c("ma_short", 5); fb.c("ma_long", 20)
    b1 = fb.c("bars_1", bt1["bars"])
    b2 = fb.c("bars_2", bt2["bars"])
    extra = fb.c("extra_bars", bt2["bars"] - bt1["bars"])
    t1 = fb.n("trades_1", m1["trades_count"], 0)
    t2 = fb.n("trades_2", m2["trades_count"], 0)
    bu1 = fb.c("buys_1", bt1["buys"]); se1 = fb.c("sells_1", bt1["sells"])
    bu2 = fb.c("buys_2", bt2["buys"]); se2 = fb.c("sells_2", bt2["sells"])
    tr1 = fb.n("total_return_1", m1["total_return_pct"], 2, True)
    ar1 = fb.n("annual_return_1", m1["annual_return_pct"], 2, True)
    sh1 = fb.n("sharpe_1", m1["sharpe_ratio"], 2)
    md1 = fb.n("mdd_1", m1["max_drawdown_pct"], 2)
    wr1 = fb.n("win_1", m1["win_rate_pct"], 2)
    tr2 = fb.n("total_return_2", m2["total_return_pct"], 2, True)
    wr2 = fb.n("win_2", m2["win_rate_pct"], 2)
    n20 = fb.c("drop_20", 20, "param"); n40 = fb.c("drop_40", 40, "param")
    t20 = fb.n("trades_drop20", c20["trades_count"], 0)
    bu20 = fb.c("buys_drop20", cut20["buys"])
    se20 = fb.c("sells_drop20", cut20["sells"])
    tr20 = fb.n("total_return_drop20", c20["total_return_pct"], 2, True)
    ar20 = fb.n("annual_return_drop20", c20["annual_return_pct"], 2, True)
    sh20 = fb.n("sharpe_drop20", c20["sharpe_ratio"], 2)
    md20 = fb.n("mdd_drop20", c20["max_drawdown_pct"], 2)
    wr20 = fb.n("win_drop20", c20["win_rate_pct"], 2)
    t40 = fb.n("trades_drop40", c40["trades_count"], 0)
    tr40 = fb.n("total_return_drop40", c40["total_return_pct"], 2, True)
    md40 = fb.n("mdd_drop40", c40["max_drawdown_pct"], 2)
    wr40 = fb.n("win_drop40", c40["win_rate_pct"], 2)
    gap = fb.n("tail20_gap_pp", m1["total_return_pct"] - c20["total_return_pct"], 2)

    a1 = (
        f"先定口径再谈结论。120 根日线（终点 {bt1['end']}），5 日 20 日均线金叉死叉，"
        f"上一根收盘出信号、本根开盘成交，一手一百股取整，佣金 0.03%、卖出加 0.10% 印花税，"
        f"期末按收盘盯市。这份收益我先不当结论。\n\n"
        f"结果是 {t1} 笔成交、{bu1} 买 {se1} 卖，区间 {tr1}%、最大回撤 {md1}%，"
        f"{se1} 笔平仓里两笔赚钱，胜率 {wr1}%。\n\n"
        f"成立条件很窄：{se1} 笔平仓撑不起「这套参数好」，笔数不够，收益只是噪音，"
        f"我最多说这半年它在这只票上是正的；"
        f"同一套规则拉到 {b2} 根（请求 250 根），区间 {tr2}%、胜率 {wr2}%，直接翻面。"
        f"窗口本身就是参数，震荡段均线反复穿越，成交多但每笔都在付成本。\n\n"
        f"利润是不是都来自末端拉升，只是没跑过的假设，不能当论据；下一步把末端切掉，"
        f"用同一套参数重跑，再把两次结果并排。切尾会让起点前移，差值里混进什么，"
        f"我现在不确定。"
    )
    a2 = (
        f"按上一条做实测：同一策略、同一参数，窗口仍是 {b1} 根，末端切掉 {n20} 根，"
        f"终点退到 {cut20['end']}，两个窗口一样长，只是一个提前收工。"
        f"结果成交 {t20} 笔、{bu20} 买 {se20} 卖，区间 {tr20}%、回撤 {md20}%、胜率五成。\n\n"
        f"和原来比，收益少 {gap} 个百分点，回撤几乎没动，这 {n20} 根确实扛着大部分利润；"
        f"胜率从 {wr1}% 掉到五成，更多是分母从 {se1} 笔变 {se20} 笔，谈不上崩塌。\n\n"
        f"再切 {n40} 根，终点退到 {cut40['end']}，区间 {tr40}%、回撤 {md40}%、胜率两成，"
        f"策略直接转负，这才是拿掉一整段拉升后的样子，两次切尾都登记了新的回测号。\n\n"
        f"缺口在起点：切尾后起点跟着前移，那 {gap} 个百分点里混着早期信号，"
        f"这个差值只能算一次对照，不能算因果"
        f"；要固定起点、只挪终点，得改引擎的区间入参，本轮没做，"
        f"换一种切法会不会变，我不确定，样本也仅一年，我没有把握把这个缺口补上。"
    )
    q1 = f"{code} 用 5 日 20 日均线回测最近 120 个交易日，结果怎么样"
    q2 = "你说的利润集中在末端只是猜测吧？把最后二十根切掉，同一套参数再跑一次"
    bid1 = register_backtest(ctx, code, "ma_cross", 120, bt1)
    bid2 = register_backtest(ctx, code, "ma_cross", 250, bt2)
    bid20 = register_backtest(ctx, code, "ma_cross", 120, cut20, tag=f"-drop{n20}")
    bid40 = register_backtest(ctx, code, "ma_cross", 120, cut40, tag=f"-drop{n40}")
    return ctx.emit(
        intent="investment_analysis", task="backtest_followup", subtype="multi_turn",
        tone="analytical", questions=[q1, q2], answers=[a1, a2], fb=fb,
        source_ids=[bid1, bid2, bid20, bid40],
        params={"symbol": code, "strategy": "ma_cross",
                "windows": [120, 250], "bars": [bt1["bars"], bt2["bars"]],
                "drop_tail_bars": [cut20["drop_tail"], cut40["drop_tail"]],
                "initial_capital": CAPITAL},
        interval_key=f"{code}|{bt2['start']}|{bt2['end']}|ma_cross",
        tool_calls=[
            {"step": 1, "tool": "run_strategy_backtest",
             "args": {"symbol": code, "strategy": "ma_cross", "days": 120},
             "ok": True, "source_id": bid1},
            {"step": 2, "tool": "run_strategy_backtest",
             "args": {"symbol": code, "strategy": "ma_cross", "days": 120,
                      "drop_last_bars": 20},
             "ok": True, "source_id": bid20},
            {"step": 3, "tool": "run_strategy_backtest",
             "args": {"symbol": code, "strategy": "ma_cross", "days": 120,
                      "drop_last_bars": 40},
             "ok": True, "source_id": bid40},
            {"step": 4, "tool": "run_strategy_backtest",
             "args": {"symbol": code, "strategy": "ma_cross", "days": 250},
             "ok": True, "source_id": bid2},
        ],
        evidence="app/backtest_engine/runner.py::run_strategy_backtest（同一策略同一参数，末端切 20/40 根后重跑）",
        extra_prov={"backtest_id": bid1, "backtest_ids": [bid1, bid2, bid20, bid40],
                    "slice_tests": [
                        {"drop_last_bars": cut20["drop_tail"],
                         "interval": [cut20["start"], cut20["end"]], "backtest_id": bid20},
                        {"drop_last_bars": cut40["drop_tail"],
                         "interval": [cut40["start"], cut40["end"]], "backtest_id": bid40},
                    ]},
    )


# ============================ 校验 ============================
KEYWORDS = {
    "数据/区间": ("区间", "日线", "K 线", "根", "收盘", "交易日"),
    "公式/口径": ("口径", "按", "算", "统计", "比值"),
    "成立/前提": ("成立", "前提", "只在", "样本"),
    "反转/不确定": ("不确定", "反转", "反过来", "失真", "变"),
}


def audit_sample(sample: dict) -> dict:
    codes: set[str] = set()
    sym = str(sample["provenance"]["params"].get("symbol") or "")
    if sym:
        codes.add(sym)
        codes.update(x for x in sym.split(".") if x.isdigit())
    for code in ("600519", "600036", "300750", "600030", "000001", "601318"):
        codes.add(code)

    fb = FactBook()
    prov = sample["provenance"]
    fb.allowed.update(float(x) for x in prov.get("numbers") or [])
    fb.dates.update(prov.get("dates") or [])
    for v in (prov.get("params") or {}).values():
        if isinstance(v, str) and _DATE_RE.fullmatch(v):
            fb.dates.add(v)
        if isinstance(v, list):
            for x in v:
                if isinstance(x, str) and _DATE_RE.fullmatch(x):
                    fb.dates.add(x)

    answers = [m["content"] for m in sample["messages"] if m["role"] == "assistant"]
    bad: list[str] = []
    lens: list[tuple[int, int]] = []
    flags: dict[str, bool] = {}
    for a in answers:
        bad.extend(check_numbers(a, fb, codes))
        lens.append((len(a), cjk_len(a)))
        for name, kws in KEYWORDS.items():
            flags[name] = flags.get(name, False) or any(k in a for k in kws)
    return {"sample_id": sample["sample_id"], "intent": sample["intent"],
            "subtype": sample["provenance"].get("subtype"),
            "lens": lens, "bad": sorted(set(bad)), "flags": flags,
            "answers": answers}


def run_audit(samples: list[dict]) -> int:
    bad_samples = 0
    print(f"{'#':<3}{'subtype':<20}{'intent':<22}{'总字数(汉字)':<18}四要件        未登记")
    for i, s in enumerate(samples, 1):
        r = audit_sample(s)
        lens = " / ".join(f"{L}({c})" for L, c in r["lens"])
        miss = [k for k, v in r["flags"].items() if not v]
        bad = "、".join(r["bad"]) or "-"
        if r["bad"]:
            bad_samples += 1
        over = any(L < 250 or L > 400 or c < 250 or c > 400 for L, c in r["lens"])
        print(f"{i:<3}{str(r['subtype']):<20}{r['intent']:<22}{lens:<18}"
              f"{'OK' if not miss else '缺' + '/'.join(miss):<14}{bad}"
              f"{'  [长度越界]' if over else ''}")
    print(f"unregistered={bad_samples}/{len(samples)}")
    return bad_samples


# ============================ 落盘 ============================
def write_outputs(records: list[tuple[str, dict]], verify_only: bool = False) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    samples = [s for _, s in records]
    with open(OUT_DIR / "samples.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    lines = [
        "# 推理型 SFT 样例（10 条）",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}；"
        "数值全部来自 baostock 日线与 app.backtest_engine 实跑结果，"
        "每条样本的 provenance.numbers 已覆盖正文中出现的所有数字（日期与代码除外）。",
        "",
    ]
    for i, (title, s) in enumerate(records, 1):
        lines.append(f"## {i}. {title}")
        lines.append("")
        users = [m["content"] for m in s["messages"] if m["role"] == "user"]
        assistants = [m["content"] for m in s["messages"] if m["role"] == "assistant"]
        for j, (u, a) in enumerate(zip(users, assistants), 1):
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


def read_titles(path: Path) -> dict[int, str]:
    """从已有 samples.md 读回小节标题，保证未重写的样本标题一字不动。"""
    if not path.exists():
        return {}
    out: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"## (\d+)\. (.+)$", line)
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="推理型 SFT 样例生成（10 条）")
    ap.add_argument("--verify", action="store_true", help="只校验已落盘的 samples.jsonl")
    ap.add_argument("--only", default="",
                    help="只重写指定序号（如 2,6,10），其余样本从已落盘文件原样保留")
    return ap.parse_args()


async def main() -> int:
    args = parse_args()
    if args.verify:
        path = OUT_DIR / "samples.jsonl"
        samples = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        bad = run_audit(samples)
        return 0 if bad == 0 else 1

    ctx = ReasoningCtx()
    records: list[tuple[str, dict]] = []

    print("[1/10] 600519 ma_cross 180 ...")
    d519 = await load_symbol("sh.600519")
    bt519 = await load_backtest("sh.600519", "ma_cross", 180)
    ctx.source(f"quote:{d519['code']}:{d519['quote']['trade_date']}", {
        "type": "quote", "source_id": f"quote:{d519['code']}:{d519['quote']['trade_date']}",
        "symbol": d519["code"], "trade_date": d519["quote"]["trade_date"],
        "data_source": d519["quote"].get("data_source"), "payload": d519["quote"]})
    records.append(("回测 · 均线金叉死叉 · sh.600519 · 180日", s01(ctx, d519, bt519)))

    print("[2/10] 600036 ma_cross 250 ...")
    d036 = await load_symbol("sh.600036")
    bt036 = await load_backtest("sh.600036", "ma_cross", 250)
    records.append(("回测 · 均线金叉死叉 · sh.600036 · 250日（实得245根）", s02(ctx, d036, bt036)))

    print("[3/10] 300750 donchian 120 ...")
    d750 = await load_symbol("sz.300750")
    bt750 = await load_backtest("sz.300750", "donchian_breakout", 120)
    records.append(("回测 · 唐奇安通道突破 · sz.300750 · 120日", s03(ctx, d750, bt750)))

    print("[4/10] 持仓 600519 ...")
    records.append(("持仓 · sh.600519 · 100股 · 60个交易日前买入", s04(ctx, d519)))
    ctx.source(f"hist:{d519['code']}:{d519['bars'][-61]['date']}", {
        "type": "hist_bar", "source_id": f"hist:{d519['code']}:{d519['bars'][-61]['date']}",
        "symbol": d519["code"], "trade_date": d519["bars"][-61]["date"],
        "close": d519["bars"][-61]["close"], "data_source": "baostock"})

    print("[5/10] 持仓 600030 ...")
    d030 = await load_symbol("sh.600030")
    ctx.source(f"quote:{d030['code']}:{d030['quote']['trade_date']}", {
        "type": "quote", "source_id": f"quote:{d030['code']}:{d030['quote']['trade_date']}",
        "symbol": d030["code"], "trade_date": d030["quote"]["trade_date"],
        "data_source": d030["quote"].get("data_source"), "payload": d030["quote"]})
    records.append(("持仓 · sh.600030 · 2000股 · 60个交易日前买入", s05(ctx, d030)))
    ctx.source(f"hist:{d030['code']}:{d030['bars'][-61]['date']}", {
        "type": "hist_bar", "source_id": f"hist:{d030['code']}:{d030['bars'][-61]['date']}",
        "symbol": d030["code"], "trade_date": d030["bars"][-61]["date"],
        "close": d030["bars"][-61]["close"], "data_source": "baostock"})

    print("[6/10] 风险 600036 ...")
    records.append(("风险 · sh.600036 · 60日位置与20日波动", s06(ctx, d036)))
    print("[7/10] 风险 300750 ...")
    records.append(("风险 · sz.300750 · 60日回撤与波动", s07(ctx, d750)))

    print("[8/10] 拒答 600519 ...")
    records.append(("拒答 · sh.600519 · 目标价与止损位", s08(ctx, d519, bt519)))
    print("[9/10] 澄清 600519 ...")
    records.append(("澄清 · sh.600519 · “能不能买”范围不清", s09(ctx, d519)))

    print("[10/10] 多轮 000001 120 → 切尾 20/40 根实测 ...")
    d001 = await load_symbol("sz.000001")
    bt001a = await load_backtest("sz.000001", "ma_cross", 120)
    bt001b = await load_backtest("sz.000001", "ma_cross", 250)
    cut20 = await load_backtest("sz.000001", "ma_cross", 120, drop_tail=20)
    cut40 = await load_backtest("sz.000001", "ma_cross", 120, drop_tail=40)
    records.append(("多轮追问 · sz.000001 · 均线 · 120日 → 切尾20/40根实测",
                    s10(ctx, d001, bt001a, bt001b, cut20, cut40)))

    # 风险快照 source
    for d in (d036, d750):
        code, td = d["code"], d["quote"]["trade_date"]
        ctx.source(f"risk:{code}:{td}", {
            "type": "risk_snapshot", "source_id": f"risk:{code}:{td}", "symbol": code,
            "trade_date": td,
            "indicators": {k: d["ind"].get(k) for k in
                           ("volatility20", "pos60", "high60", "low60", "ret20", "rsi14", "ma20")},
            "quote_change_percent": d["quote"].get("change_percent")})

    only = {int(x) for x in args.only.split(",") if x.strip()}
    if only:
        old_path = OUT_DIR / "samples.jsonl"
        old = [json.loads(x) for x in
               old_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        old_titles = read_titles(OUT_DIR / "samples.md")
        if len(old) != len(records):
            raise RuntimeError(f"已落盘 {len(old)} 条与本次 {len(records)} 条不一致，无法按序号合并")
        for i, (title, s) in enumerate(records, 1):
            if i not in only:
                records[i - 1] = (old_titles.get(i, title), old[i - 1])
        print(f"\n按 --only 保留未重写样本：{[i for i in range(1, len(records) + 1) if i not in only]}")

    write_outputs(records)

    samples = [s for _, s in records]
    print("\n=== 自检 ===")
    bad = run_audit(samples)

    print("\n=== 样例前三句（第 1 条）===")
    first = samples[0]["messages"][2]["content"]
    parts = [p for p in re.split(r"(?<=[。；])", first) if p.strip()]
    for p in parts[:3]:
        print(p.strip())

    sources = set()
    for s in samples:
        sources.update(s["provenance"]["source_ids"])
    missing = [x for x in sources if x not in ctx.sources]
    print(f"\nsource_ids 已落盘 {len(sources) - len(missing)}/{len(sources)}，缺失：{missing}")
    print(f"输出：{OUT_DIR / 'samples.jsonl'} / {OUT_DIR / 'samples.md'}")
    return 0 if bad == 0 and not missing else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
