#!/usr/bin/env python
"""衡策 SFT 数据合成：以真实回测 / 行情结果为唯一数值来源，生成可追溯的训练轨迹。

设计约束（与需求一一对应）
--------------------------
1. 数值唯一来源：本脚本不内置任何行情/收益率，全部调用仓库现有的
   ``app.backtest_engine.run_strategy_backtest`` 与 ``app.agent.tools.get_quote /
   get_historical``，答案里的每个数字都必须在 ``FactBook`` 登记过。
2. 可追溯：每条样本带 ``provenance``（sample_id → source_id → 参数），
   ``source_id`` 指向 ``sources.jsonl`` 里保存的原始结果快照。
3. 轨迹：``tool_trace`` 记录「问题 → 调工具 → 拿结果」这一跳，
   ``messages`` 的 user 侧按线上 ``node_analysis`` 的 ``<data>`` 注入方式组织，
   保证训练/推理同构。
4. 负样本：数据不足 / 超出能力边界 / 要求预测涨跌，分开生成，不硬造。
5. 质量闸门：去重（问题级 + 答案级）、未登记数字、长度异常、拒答错位。
6. 切分：按标的切分 train/val/test，并在 manifest 里给出泄漏自检结果。

用法见同目录 README_sft_synth.md。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from loguru import logger  # noqa: E402

from app.agent.indicators import compute_indicators  # noqa: E402
from app.agent.tools import get_historical, get_quote, normalize_a_share  # noqa: E402
from app.backtest_engine import run_strategy_backtest  # noqa: E402
from app.backtest_engine.runner import STRATEGY_META  # noqa: E402

SYSTEM_PROMPT = (
    "你是衡策，一个 A 股投研助手。回答必须只引用 <data> 中给出的真实数据，"
    "不得编造行情、收益率或目标价，不得预测涨跌；数据不足时明确说明缺什么数据。"
    "所有内容仅供学习参考，不构成投资建议。"
)
DISCLAIMER = "⚠️ 以上内容仅供学习参考，不构成投资建议。投资有风险，入市需谨慎。"

# 可自动生成的四类意图；其余四类（闲聊 / 预警配置 / 选股 / 知识问答）明确跳过：
#   chitchat       —— 无数据可锚定，写了也只是话术
#   alert_config   —— 纯前端配置动作，没有可回填的真实结果
#   screener       —— 全市场扫描，单次成本高且结果不是「单标的结论」
#   knowledge_query—— 依赖 RAG 语料，无数字可校验，容易退化成常识问答
NORMAL_INTENTS = ("market_query", "portfolio_query", "investment_analysis", "risk_assessment")
REFUSAL_INTENT = "refusal"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


# ============================ 数值登记簿 ============================
class FactBook:
    """登记样本里允许出现的每个数字，答案渲染时只从这里取值。"""

    def __init__(self) -> None:
        self.facts: dict[str, dict] = {}
        self.allowed: set[float] = set()
        self.dates: set[str] = set()

    def n(self, key: str, value, digits: int = 2, signed: bool = False,
          kind: str = "metric") -> str:
        """登记并格式化一个指标值。

        同时登记「原始值」和「渲染后的值」：像 -11.0432 渲染成 -11.04 时，
        闸门按文本里实际出现的 -11.04 校验，不会误判成未登记数字。
        """
        v = float(value)
        self.facts[key] = {"value": round(v, 6), "kind": kind}
        self.allowed.add(round(v, 6))
        if digits == 0:
            s = f"{v:+,.0f}" if signed else f"{v:,.0f}"
        else:
            s = f"{v:+,.{digits}f}" if signed else f"{v:,.{digits}f}"
        try:
            self.allowed.add(round(float(s.replace(",", "")), 6))
        except ValueError:
            pass
        return s

    def c(self, key: str, value, kind: str = "code_constant") -> str:
        """登记一个常量（阈值、手数、窗口等），返回原样文本。"""
        self.facts[key] = {"value": value, "kind": kind}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.allowed.add(round(float(value), 6))
        return str(value)

    def d(self, date_str: str) -> str:
        self.dates.add(date_str)
        return date_str


def _mask(text: str, codes: set[str]) -> str:
    out = _DATE_RE.sub(" ", text)
    for code in codes:
        out = out.replace(code, " ")
    return out


def extract_numbers(text: str, codes: set[str]) -> list[tuple[str, float]]:
    found = []
    for m in _NUM_RE.finditer(_mask(text, codes)):
        try:
            found.append((m.group(), float(m.group().replace(",", ""))))
        except ValueError:
            continue
    return found


def check_numbers(answer: str, fb: FactBook, codes: set[str]) -> list[str]:
    """返回未登记数字的原文列表（空 = 通过）。"""
    bad = []
    for raw, val in extract_numbers(answer, codes):
        if round(val, 6) not in fb.allowed:
            bad.append(raw)
    for d in _DATE_RE.findall(answer):
        if d not in fb.dates:
            bad.append(d)
    return bad


# ============================ 样本与上下文 ============================
class Ctx:
    def __init__(self) -> None:
        self.sources: dict[str, dict] = {}
        self.samples: list[dict] = []
        self._seq = 0

    def next_id(self) -> str:
        self._seq += 1
        return f"sft-{self._seq:06d}"

    def add(self, *, intent: str, task: str, question: str, data_block: str,
            answer: str, fb: FactBook, source_ids: list[str], params: dict,
            interval_key: str, tool_calls: list[dict], evidence: str = "") -> None:
        user_content = question if not data_block else f"{question}\n\n<data>\n{data_block}\n</data>"
        self.samples.append({
            "sample_id": self.next_id(),
            "intent": intent,
            "task": task,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": answer},
            ],
            "tool_trace": tool_calls,
            "provenance": {
                "sample_id": "",  # 由 finalize 回填
                "intent": intent,
                "task": task,
                "source_ids": source_ids,
                "params": params,
                "interval_key": interval_key,
                "facts": {k: v["value"] for k, v in fb.facts.items()},
                "fact_kinds": {k: v["kind"] for k, v in fb.facts.items()},
                "numbers": sorted(fb.allowed),
                "dates": sorted(fb.dates),
                "evidence": evidence,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        })

    def source(self, sid: str, payload: dict) -> None:
        self.sources.setdefault(sid, payload)


# ============================ 生成：看盘 ============================
_MARKET_QUESTIONS = [
    "{code} 现在什么价位？",
    "帮我看看 {code} 的行情",
    "{code} 最新走势怎么样？",
]


def gen_market(ctx: Ctx, rng: random.Random, symbol: str, quote: dict,
               ind: dict, bars: list) -> str | None:
    if not quote or quote.get("error"):
        return f"quote_unavailable:{symbol}"
    price = float(quote.get("current_price") or 0)
    if price <= 0:
        return f"quote_invalid:{symbol}"

    code = normalize_a_share(symbol) or symbol
    trade_date = str(quote.get("trade_date") or (bars[-1]["date"] if bars else ""))
    if not _DATE_RE.match(trade_date):
        return f"quote_date_missing:{symbol}"

    prev = float(quote.get("previous_close") or price)
    change = quote.get("change")
    change = float(change) if change is not None else price - prev
    pct = float(quote.get("change_percent") or 0)
    high = float(quote.get("high") or price)
    low = float(quote.get("low") or price)
    vol = int(quote.get("volume") or 0)
    src = str(quote.get("data_source") or "unknown")

    sid = f"quote:{code}:{trade_date}"
    ctx.source(sid, {"type": "quote", "source_id": sid, "symbol": code,
                     "trade_date": trade_date, "data_source": src, "payload": quote})

    fb = FactBook()
    fb.d(trade_date)
    # 指标窗口号会以「MA5 / MA20 / RSI14 / 近 5 日」形式出现在正文里，一并登记
    fb.c("ma_short_window", 5, "indicator_window")
    fb.c("ma_long_window", 20, "indicator_window")
    fb.c("rsi_window", 14, "indicator_window")
    p = fb.n("current_price", price, 2)
    pv = fb.n("previous_close", prev, 2)
    ch = fb.n("change", change, 2, signed=True)
    pc = fb.n("change_percent", pct, 2, signed=True)
    hi = fb.n("high", high, 2)
    lo = fb.n("low", low, 2)
    vo = fb.n("volume", vol, 0)

    data_lines = [
        f"标的：{code}", f"交易日：{trade_date}", f"最新价：{p} 元",
        f"昨收：{pv} 元", f"涨跌：{ch} 元（{pc}%）",
        f"最高 / 最低：{hi} / {lo} 元", f"成交量：{vo} 股", f"数据源：{src}",
    ]

    ta_lines: list[str] = []
    if ind:
        if ind.get("ma5") is not None:
            ta_lines.append(f"MA5：{fb.n('ma5', ind['ma5'], 2)} 元")
        if ind.get("ma20") is not None:
            ta_lines.append(f"MA20：{fb.n('ma20', ind['ma20'], 2)} 元")
        if ind.get("rsi14") is not None:
            ta_lines.append(f"RSI14：{fb.n('rsi14', ind['rsi14'], 2)}")
        if ind.get("ret5") is not None:
            ta_lines.append(f"近 5 日涨跌幅：{fb.n('ret5', ind['ret5'], 2, True)}%")
        if ind.get("ret20") is not None:
            ta_lines.append(f"近 20 日涨跌幅：{fb.n('ret20', ind['ret20'], 2, True)}%")
        if ta_lines:
            data_lines.append("技术指标：")
            data_lines.extend(f"  {x}" for x in ta_lines)

    answer = [
        f"{code} 在 {trade_date} 的最新价是 {p} 元，较昨收 {pv} 元变动 {ch} 元（{pc}%）。"
        f"当日最高 {hi} 元、最低 {lo} 元，成交量 {vo} 股，数据源为 {src}。"
    ]
    if ta_lines:
        ma5, ma20 = ind.get("ma5"), ind.get("ma20")
        pos = ""
        if ma5 is not None and ma20 is not None:
            pos = "，现价在 MA5 与 MA20 之上" if price >= max(ma5, ma20) else (
                "，现价在 MA5 与 MA20 之下" if price <= min(ma5, ma20) else "，现价夹在 MA5 与 MA20 之间")
        tech = f"技术面：{ta_lines[0].replace('：', ' 为 ', 1)}"
        if len(ta_lines) > 1:
            tech += "；" + "；".join(x.replace("：", " 为 ", 1) for x in ta_lines[1:])
        answer.append(tech + pos + "。")
    answer.append("以上仅为行情与指标数据的客观呈现，不预测后续涨跌。")
    answer.append(DISCLAIMER)

    question = rng.choice(_MARKET_QUESTIONS).format(code=code)
    ctx.add(
        intent="market_query", task="quote_snapshot", question=question,
        data_block="\n".join(data_lines), answer="\n".join(answer), fb=fb,
        source_ids=[sid],
        params={"symbol": code, "trade_date": trade_date},
        interval_key=f"{code}|{trade_date}",
        tool_calls=[{"step": 1, "tool": "get_quote", "args": {"symbol": code},
                     "ok": True, "source_id": sid}],
        evidence="app/agent/tools.py::get_quote + app/agent/indicators.py::compute_indicators",
    )
    return None


# ============================ 生成：回测验证 ============================
_BT_QUESTIONS = [
    "用「{sname}」回测一下 {code} 最近 {n} 个交易日，结果怎么样？",
    "帮我验证「{sname}」在 {code} 上的表现，跑 {n} 个交易日。",
    "{code} 用「{sname}」做 {n} 个交易日回测，收益和回撤分别是多少？",
]


def gen_backtest(ctx: Ctx, rng: random.Random, symbol: str, strategy: str,
                 requested_days: int, capital: float, result: dict) -> str | None:
    metrics = result.get("metrics") or {}
    curve = result.get("equity_curve") or []
    if not metrics or not curve:
        return f"backtest_empty:{symbol}:{strategy}"
    if result.get("warning"):
        return f"backtest_lot_blocked:{symbol}:{strategy}:{requested_days}"
    # 0 成交的回测没有可学的策略结论（全是 0.00%），单独计入 skip，不进数据集
    if int(metrics.get("trades_count") or 0) == 0:
        return f"no_trades:{symbol}:{strategy}:{requested_days}"

    code = normalize_a_share(symbol) or symbol
    start_date, end_date = str(curve[0]["date"]), str(curve[-1]["date"])
    bars_count = int(result.get("bars_count") or len(curve))
    sname = STRATEGY_META.get(strategy, {}).get("name", strategy)
    params = result.get("params") or {}

    bh = hashlib.md5(json.dumps(
        {"symbol": code, "strategy": strategy, "days": bars_count,
         "capital": capital, "params": params, "end": end_date},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
    bid = f"bt:{code}:{strategy}:{bars_count}:{int(capital)}:{bh}"

    ctx.source(bid, {
        "type": "backtest", "source_id": bid, "symbol": code, "strategy": strategy,
        "strategy_name": sname, "params": params, "requested_days": requested_days,
        "bars_count": bars_count, "interval": [start_date, end_date],
        "initial_capital": capital, "metrics": metrics,
        "trades_count": metrics.get("trades_count"),
    })

    fb = FactBook()
    fb.d(start_date); fb.d(end_date)
    # 策略显示名里可能带数字（如「RSI + MA200 趋势过滤」），一并登记
    for tok in _NUM_RE.findall(sname):
        try:
            fb.c(f"strategy_name_num_{tok}", float(tok.replace(",", "")), "strategy_name")
        except ValueError:
            continue
    fb.c("bars_count", bars_count)
    fb.c("commission_pct", 0.03, "code_constant")
    fb.c("stamp_tax_pct", 0.10, "code_constant")
    fb.c("initial_capital", capital)
    fb.c("lot_size", 100, "code_constant")

    tr = fb.n("total_return_pct", metrics.get("total_return_pct", 0.0), 2, True)
    ar = fb.n("annual_return_pct", metrics.get("annual_return_pct", 0.0), 2, True)
    sh = fb.n("sharpe_ratio", metrics.get("sharpe_ratio", 0.0), 2)
    md = fb.n("max_drawdown_pct", metrics.get("max_drawdown_pct", 0.0), 2)
    wr = fb.n("win_rate_pct", metrics.get("win_rate_pct", 0.0), 2)
    tc = fb.n("trades_count", int(metrics.get("trades_count") or 0), 0)
    fe = fb.n("final_equity", metrics.get("final_equity", 0.0), 2)
    ic = fb.n("initial_capital_text", capital, 2)

    data_lines = [
        f"标的：{code}", f"策略：{sname}（{strategy}）",
        f"实际回测区间：{start_date} ~ {end_date}（{bars_count} 个交易日）",
        f"参数：{json.dumps(params, ensure_ascii=False) if params else '策略默认参数'}",
        f"初始资金：{ic} 元", f"费用：佣金 0.03%、卖出印花税 0.10%",
        f"成交假设：信号基于上一根已收盘 K 线，本根开盘成交",
        "回测结果：",
        f"  区间总收益：{tr}%", f"  年化收益：{ar}%", f"  Sharpe：{sh}",
        f"  最大回撤：{md}%", f"  胜率：{wr}%（{tc} 笔成交）",
        f"  期末权益：{fe} 元",
        f"数据源：baostock 前复权日线",
    ]

    answer = [
        f"我调用回测引擎在 {code} 上跑了「{sname}」，实际区间 {start_date} 至 {end_date}，"
        f"共 {fb.c('bars_count_echo', bars_count)} 个交易日，初始资金 {ic} 元。",
        f"结果：区间总收益 {tr}%，年化 {ar}%，Sharpe {sh}，最大回撤 {md}%，"
        f"胜率 {wr}%（{tc} 笔成交），期末权益 {fe} 元。",
        f"该结果是固定参数下的历史回测，已按次日开盘成交、佣金 0.03%、卖出印花税 0.10% 计算；"
        f"未计入滑点、停牌与分红再投资。历史表现不代表未来收益。",
        DISCLAIMER,
    ]

    question = rng.choice(_BT_QUESTIONS).format(code=code, sname=sname, n=bars_count)
    ctx.add(
        intent="investment_analysis", task="backtest_review", question=question,
        data_block="\n".join(data_lines), answer="\n".join(answer), fb=fb,
        source_ids=[bid],
        params={"symbol": code, "strategy": strategy, "bars_count": bars_count,
                "interval": [start_date, end_date], "initial_capital": capital,
                "strategy_params": params},
        interval_key=f"{code}|{start_date}|{end_date}",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": strategy,
                              "initial_capital": capital},
                     "ok": True, "source_id": bid}],
        evidence="app/backtest_engine/runner.py::run_strategy_backtest",
    )
    return None


# ============================ 生成：风险提示 ============================
_RISK_QUESTIONS = [
    "{code} 最近风险大吗？波动和位置怎么样？",
    "帮我看下 {code} 的波动风险",
    "{code} 现在的价格位置和波动率是什么水平？",
]


def gen_risk(ctx: Ctx, rng: random.Random, symbol: str, quote: dict,
             ind: dict, bars: list) -> str | None:
    if not quote or quote.get("error") or not ind:
        return f"risk_source_missing:{symbol}"
    vol20 = ind.get("volatility20")
    pos60 = ind.get("pos60")
    if vol20 is None or pos60 is None:
        return f"risk_indicator_missing:{symbol}"

    code = normalize_a_share(symbol) or symbol
    trade_date = str(quote.get("trade_date") or (bars[-1]["date"] if bars else ""))
    if not _DATE_RE.match(trade_date):
        return f"risk_date_missing:{symbol}"

    pct = abs(float(quote.get("change_percent") or 0))
    # 与 app/agent/nodes.py::node_risk 的分档规则一致：>5 高波动，>2 中等，其余相对稳定
    level = "高波动" if pct > 5 else ("中等波动" if pct > 2 else "相对稳定")

    sid = f"risk:{code}:{trade_date}"
    ctx.source(sid, {"type": "risk_snapshot", "source_id": sid, "symbol": code,
                     "trade_date": trade_date,
                     "indicators": {k: ind.get(k) for k in
                                    ("volatility20", "pos60", "high60", "low60",
                                     "ret20", "rsi14", "ma20")},
                     "quote_change_percent": quote.get("change_percent")})

    fb = FactBook()
    fb.d(trade_date)
    fb.c("high_vol_threshold_pct", 5, "code_rule:agent/nodes.py")
    fb.c("mid_vol_threshold_pct", 2, "code_rule:agent/nodes.py")
    fb.c("window_20", 20, "code_constant")
    fb.c("window_60", 60, "code_constant")

    v = fb.n("volatility20", vol20, 1)
    pos = fb.n("pos60", pos60, 1)
    hi = fb.n("high60", ind["high60"], 2)
    lo = fb.n("low60", ind["low60"], 2)
    r20 = fb.n("ret20", ind["ret20"], 2, True) if ind.get("ret20") is not None else None
    rsi = fb.n("rsi14", ind["rsi14"], 2) if ind.get("rsi14") is not None else None

    data_lines = [
        f"标的：{code}", f"交易日：{trade_date}",
        f"当日涨跌幅：{fb.n('change_percent', quote.get('change_percent') or 0.0, 2, True)}%",
        f"20 日年化波动率：{v}%",
        f"60 日区间：{lo} ~ {hi} 元，当前处于该区间 {pos}% 分位",
    ]
    if r20 is not None:
        data_lines.append(f"近 20 日涨跌幅：{r20}%")
    if rsi is not None:
        data_lines.append(f"RSI14：{rsi}")

    answer = [
        f"{code} 在 {trade_date} 的波动特征如下：20 日年化波动率 {v}%，"
        f"60 日价格区间 {lo} 至 {hi} 元，当前处于区间 {pos}% 分位。",
        f"按系统内置的涨跌幅分档规则（单日涨跌幅超过 5% 记为高波动、超过 2% 记为中等波动），"
        f"当日属于「{level}」。",
    ]
    if r20 is not None:
        answer.append(f"近 20 个交易日累计涨跌幅为 {r20}%，可作为波动与趋势的辅助参考。")
    answer += [
        "以上是波动率与价格位置的量化描述，不含方向判断；仓位如何安排取决于你的风险偏好和持有成本。",
        DISCLAIMER,
    ]

    ctx.add(
        intent="risk_assessment", task="risk_snapshot", question=rng.choice(_RISK_QUESTIONS).format(code=code),
        data_block="\n".join(data_lines), answer="\n".join(answer), fb=fb,
        source_ids=[sid],
        params={"symbol": code, "trade_date": trade_date},
        interval_key=f"{code}|{trade_date}",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 90}, "ok": True, "source_id": sid}],
        evidence="app/agent/indicators.py::compute_indicators（volatility20/pos60）+ app/agent/nodes.py::node_risk 分档",
    )
    return None


# ============================ 生成：持仓盈亏 ============================
_PORTFOLIO_QUESTIONS = [
    "我在 {buy_date} 以收盘价买入 {code} {qty} 股，到现在赚了多少？",
    "{buy_date} 收盘价买入 {code} {qty} 股，帮我算下现在的浮动盈亏。",
    "帮我算一下 {code} {qty} 股、{buy_date} 收盘价买入的持仓收益。",
]


def gen_portfolio(ctx: Ctx, rng: random.Random, symbol: str, quote: dict,
                  bars: list, lookback: int, qty: int) -> str | None:
    if not quote or quote.get("error") or len(bars) <= lookback + 1:
        return f"portfolio_source_missing:{symbol}"

    code = normalize_a_share(symbol) or symbol
    trade_date = str(quote.get("trade_date") or bars[-1]["date"])
    price = float(quote.get("current_price") or 0)
    cost_bar = bars[-lookback - 1]
    buy_date = str(cost_bar["date"])
    cost = float(cost_bar["close"])
    if price <= 0 or cost <= 0:
        return f"portfolio_price_invalid:{symbol}"

    cost_amt = cost * qty
    mv = price * qty
    pnl = mv - cost_amt
    pnl_pct = pnl / cost_amt * 100

    sid_q = f"quote:{code}:{trade_date}"
    sid_h = f"hist:{code}:{buy_date}"
    ctx.source(sid_q, {"type": "quote", "source_id": sid_q, "symbol": code,
                       "trade_date": trade_date, "data_source": quote.get("data_source"),
                       "payload": quote})
    ctx.source(sid_h, {"type": "hist_bar", "source_id": sid_h, "symbol": code,
                       "trade_date": buy_date, "close": cost, "data_source": "baostock"})

    fb = FactBook()
    fb.d(trade_date); fb.d(buy_date)
    fb.c("qty", qty, "user_param")
    fb.c("lookback_trading_days", lookback, "param")
    pr = fb.n("current_price", price, 2)
    co = fb.n("cost_price", cost, 2)
    ca = fb.n("cost_amount", cost_amt, 2)
    mv_s = fb.n("market_value", mv, 2)
    pn = fb.n("unrealized_pnl", pnl, 2, True)
    pp = fb.n("unrealized_pnl_pct", pnl_pct, 2, True)

    data_lines = [
        f"标的：{code}", f"买入日：{buy_date}", f"买入价（当日收盘）：{co} 元",
        f"数量：{fb.c('qty_echo', qty)} 股", f"成本金额：{ca} 元",
        f"最新交易日：{trade_date}", f"最新价：{pr} 元", f"市值：{mv_s} 元",
        f"浮动盈亏：{pn} 元（{pp}%）",
    ]
    answer = [
        f"按 {buy_date} 收盘价 {co} 元买入 {qty} 股计算，你的成本是 {ca} 元；"
        f"{trade_date} 最新价 {pr} 元，对应市值 {mv_s} 元。",
        f"当前浮动盈亏 {pn} 元，收益率 {pp}%。",
        "上述计算只含价格变动，未计入佣金、印花税与分红；持仓是否继续持有与你的风险偏好和资金安排有关。",
        DISCLAIMER,
    ]

    ctx.add(
        intent="portfolio_query", task="holding_pnl", question=rng.choice(_PORTFOLIO_QUESTIONS).format(
            code=code, qty=qty, buy_date=buy_date),
        data_block="\n".join(data_lines), answer="\n".join(answer), fb=fb,
        source_ids=[sid_q, sid_h],
        params={"symbol": code, "qty": qty, "buy_date": buy_date,
                "current_date": trade_date, "lookback": lookback},
        interval_key=f"{code}|{buy_date}|{trade_date}",
        tool_calls=[
            {"step": 1, "tool": "get_historical", "args": {"symbol": code, "days": 120},
             "ok": True, "source_id": sid_h},
            {"step": 2, "tool": "get_quote", "args": {"symbol": code},
             "ok": True, "source_id": sid_q},
        ],
        evidence="app/agent/tools.py::get_historical（买入日收盘）+ get_quote（最新价）",
    )
    return None


# ============================ 生成：负样本 ============================
def _refusal(ctx: Ctx, *, question: str, why: str, missing: str, offer: str,
             next_action: str, source_ids: list[str], params: dict,
             interval_key: str, evidence: str, facts: dict | None = None,
             date: str | None = None) -> None:
    fb = FactBook()
    if date:
        fb.d(date)
    for k, v in (facts or {}).items():
        fb.facts[k] = {"value": v, "kind": "observed"}
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            fb.allowed.add(round(float(v), 6))
    answer = (
        f"这个请求我无法完成：{why}。\n"
        f"缺的是：{missing}。\n"
        f"我能提供的是：{offer}。\n"
        f"如果你愿意，我可以现在就{next_action}。\n"
        f"{DISCLAIMER}"
    )
    ctx.add(
        intent=REFUSAL_INTENT, task="refusal", question=question, data_block="",
        answer=answer, fb=fb, source_ids=source_ids, params=params,
        interval_key=interval_key,
        tool_calls=[{"step": 1, "tool": "probe", "ok": False,
                     "args": params, "error": why}],
        evidence=evidence,
    )


async def gen_negatives(ctx: Ctx, symbol_for_lot: str) -> list[str]:
    """负样本：能真实探到错误就用真实错误，探不到就不生成。返回跳过说明。"""
    skipped: list[str] = []

    # --- N1 数据不足：不存在的代码，真实走一遍回测取数 ---
    bad_code = "999999"
    try:
        await run_strategy_backtest(bad_code, "ma_cross", days=120)
        skipped.append("bad_symbol_not_failed:999999")
    except Exception as e:
        _refusal(
            ctx, question=f"帮我回测 {bad_code} 最近 120 个交易日",
            why=f"回测引擎取不到 {bad_code} 的历史日线（{type(e).__name__}: {str(e)[:80]}）",
            missing=f"{bad_code} 的 A 股日线数据",
            offer="按 6 位 A 股代码（如 600519、000001）做行情快照与单标的日线回测",
            next_action="换一个有真实日线的标的重跑",
            source_ids=[], params={"symbol": bad_code, "strategy": "ma_cross", "days": 120},
            interval_key=f"NEG|bad_symbol|{bad_code}",
            evidence="app/agent/backtest.py::fetch_history_bars 取数失败分支",
            facts={"code_length": 6, "days": 120},
        )

    # --- N2 能力边界：美股没法走 A 股回测引擎 ---
    try:
        await run_strategy_backtest("AAPL", "ma_cross", days=120)
        skipped.append("us_backtest_not_failed:AAPL")
    except Exception as e:
        _refusal(
            ctx, question="用均线策略回测一下 AAPL 最近 120 个交易日",
            why=f"回测引擎只接受 A 股代码，无法识别 AAPL（{type(e).__name__}: {str(e)[:60]}）",
            missing="美股日线在 A 股回测引擎中的支持",
            offer="AAPL 的行情快照（yfinance 源），以及 A 股标的的日线回测",
            next_action="用 A 股代码跑回测，或只查 AAPL 的行情",
            source_ids=[], params={"symbol": "AAPL", "strategy": "ma_cross"},
            interval_key="AAPL|policy",
            evidence="app/agent/tools.py::normalize_a_share 仅识别 A 股；backtest_engine 依赖该函数",
        )

    # --- N3 能力边界：截面策略需要多标的 ---
    lot_code = normalize_a_share(symbol_for_lot) or symbol_for_lot
    try:
        await run_strategy_backtest(symbol_for_lot, "cross_section_momentum", days=120)
        skipped.append("multi_asset_not_failed")
    except Exception as e:
        _refusal(
            ctx, question=f"用截面动量策略回测一下 {lot_code}",
            why=f"该策略要求多标的截面数据，当前引擎是单标的模式（{str(e)[:50]}）",
            missing="同一交易日多只标的的截面行情与统一调仓账本",
            offer="单标的的均线、MACD、RSI、唐奇安等趋势/震荡/突破类策略回测",
            next_action="换一个单标的策略，或一次只给一个标的",
            source_ids=[], params={"symbol": lot_code, "strategy": "cross_section_momentum"},
            interval_key=f"NEG|multi_asset|{lot_code}",
            evidence="app/backtest_engine/runner.py 对 requires_multi_asset 抛 ValueError",
        )

    # --- N4 能力边界：逐笔 / 十档盘口没有数据源 ---
    _refusal(
        ctx, question=f"能看到 {lot_code} 的逐笔委托和十档盘口吗？",
        why="当前接入的数据源只有日线与实时快照，没有逐笔成交与十档盘口",
        missing="交易所逐笔委托与逐笔成交行情（需付费授权）",
        offer="日线 OHLCV、最新价与涨跌幅、成交量与成交额，以及基于日线的技术指标",
        next_action="用日线行情和指标帮你做盘面分析",
        source_ids=[], params={"symbol": lot_code, "request": "tick_level2"},
        interval_key=f"NEG|tick|{lot_code}",
        evidence="app/agent/tools.py 数据源注释：baostock / 新浪 / 东财快照 / yfinance",
    )

    # --- N5 能力边界：分钟级回测 ---
    _refusal(
        ctx, question=f"帮我用 5 分钟 K 线回测 {lot_code} 的均线策略",
        why="回测引擎只按日线撮合（信号基于上一根日 K，本根开盘成交）",
        missing="分钟级历史行情与对应的高频撮合/滑点模型",
        offer="日线级别的策略回测，以及日线行情快照与指标",
        next_action="把策略改成日线周期再跑",
        source_ids=[], params={"symbol": lot_code, "frequency": "5m"},
        interval_key=f"NEG|minute_bar|{lot_code}",
        evidence="app/backtest_engine/runner.py::run_strategy_backtest 仅接受日线 bars",
    )

    # --- N6 能力边界：机构持仓明细 ---
    _refusal(
        ctx, question=f"{lot_code} 最近一个季度机构持仓明细是多少？",
        why="系统没有接入股东名册与机构持仓数据",
        missing="定期报告披露的十大股东/机构持仓明细数据源",
        offer="价格、成交量、技术指标与策略回测结果",
        next_action="用行情和回测帮你判断当前的价格位置",
        source_ids=[], params={"symbol": lot_code, "request": "institutional_holding"},
        interval_key=f"NEG|institutional|{lot_code}",
        evidence="app/agent/tools.py 未提供股东/机构持仓接口",
    )

    # --- N7 预测涨跌：拒答但给出可提供的真实统计量（由调用方填 facts） ---
    return skipped


def gen_prediction_refusals(ctx: Ctx, symbol: str, quote: dict, ind: dict) -> None:
    """要求预测涨跌 / 目标价：拒绝预测，只给真实的历史统计。"""
    if not quote or quote.get("error") or not ind:
        return
    code = normalize_a_share(symbol) or symbol
    trade_date = str(quote.get("trade_date") or "")
    if not _DATE_RE.match(trade_date):
        return

    facts: dict[str, float] = {"window_20": 20}
    if ind.get("ret20") is not None:
        facts["ret20_pct"] = float(ind["ret20"])
    if ind.get("volatility20") is not None:
        facts["volatility20_pct"] = float(ind["volatility20"])

    sid = f"quote:{code}:{trade_date}"
    stat_text = ""
    if facts:
        parts = []
        if "ret20_pct" in facts:
            parts.append(f"近 20 个交易日累计涨跌幅 {facts['ret20_pct']:+.2f}%")
        if "volatility20_pct" in facts:
            parts.append(f"20 日年化波动率 {facts['volatility20_pct']:.1f}%")
        stat_text = "、".join(parts)

    _refusal(
        ctx, question=f"{code} 明天会涨吗？能到多少？",
        why="系统不做涨跌方向与目标价的预测，也无法给出概率化的明日涨跌判断",
        missing="可验证的预测模型与对应的样本外评估结果（当前没有）",
        offer=f"{code} 的历史与当前统计：{stat_text}" if stat_text else f"{code} 的行情快照与日线指标",
        next_action="用行情、指标和策略回测帮你评估当前价格位置与波动风险",
        source_ids=[sid], params={"symbol": code, "trade_date": trade_date,
                                  "request": "price_prediction"},
        interval_key=f"NEG|predict|direction|{code}",
        evidence="项目内无预测模型；agent/nodes.py 明确禁止承诺收益与精确目标价",
        facts=facts, date=trade_date,
    )

    _refusal(
        ctx, question=f"给我一个 {code} 的目标价，止损位设在哪？",
        why="目标价与止损位属于投资决策输出，系统只陈列数据与回测结果",
        missing="与你持仓成本、资金规模和风险偏好绑定的决策参数",
        offer=f"{code} 的日线行情、技术指标与策略回测的收益/回撤/胜率",
        next_action="把回测结果和你的风险偏好放在一起做取舍",
        source_ids=[sid], params={"symbol": code, "trade_date": trade_date,
                                  "request": "target_price"},
        interval_key=f"NEG|predict|target_price|{code}",
        evidence="项目定位为决策支持工具，不输出买卖点位",
    )


# ============================ 质量闸门 ============================
def _norm_q(text: str) -> str:
    return _PUNCT_RE.sub("", (text.split("<data>")[0] or "").lower())


def _norm_a(text: str) -> str:
    return _WS_RE.sub("", text)


def run_gate(ctx: Ctx, codes: set[str], min_len: int = 60, max_len: int = 1500,
             max_total_chars: int = 4000) -> tuple[list[dict], list[dict]]:
    kept: list[dict] = []
    rejects: list[dict] = []
    seen_q: dict[str, str] = {}
    seen_a: dict[str, str] = {}

    for s in ctx.samples:
        sid, intent = s["sample_id"], s["intent"]
        msgs = s.get("messages") or []
        roles = [m.get("role") for m in msgs]
        contents = [m.get("content") or "" for m in msgs]

        def _rej(reason: str, detail: str = "") -> None:
            rejects.append({"sample_id": sid, "intent": intent,
                            "reason": reason, "detail": detail[:200],
                            "answer_excerpt": (contents[2][:160] if len(contents) > 2 else "")})

        if roles != ["system", "user", "assistant"] or not all(contents):
            _rej("schema", f"roles={roles}")
            continue
        answer, question = contents[2], contents[1]
        total_chars = sum(len(c) for c in contents)
        if total_chars > max_total_chars:
            _rej("length_total", f"{total_chars} chars > {max_total_chars}")
            continue
        if not (min_len <= len(answer) <= max_len):
            _rej("length_answer", f"{len(answer)} chars")
            continue

        refusal_markers = ("无法完成", "无法提供", "不支持", "数据不足", "查不到", "不能提供")
        is_refusal_text = any(m in answer for m in refusal_markers)
        if intent != REFUSAL_INTENT and is_refusal_text:
            _rej("refusal_as_normal", "正常意图样本里出现拒答话术")
            continue
        if intent == REFUSAL_INTENT and (not is_refusal_text or "缺的是" not in answer
                                         or "能提供的是" not in answer):
            _rej("refusal_as_normal", "拒答样本缺少「缺什么/能提供什么」")
            continue

        prov = s.get("provenance") or {}
        source_ids = prov.get("source_ids") or []
        if intent != REFUSAL_INTENT and not source_ids:
            _rej("provenance_missing", "正常样本没有 source_id")
            continue
        if any(x not in ctx.sources for x in source_ids):
            _rej("provenance_broken", f"source 未落盘: {source_ids}")
            continue

        # 用样本自身的 facts / numbers 还原登记簿（保证「答案里的数字 = 已登记数字」）
        fb = FactBook()
        for k, v in (prov.get("facts") or {}).items():
            fb.facts[k] = {"value": v}
        fb.allowed.update(float(x) for x in (prov.get("numbers") or []))
        fb.dates.update(prov.get("dates") or [])
        for val in (prov.get("params") or {}).values():
            if isinstance(val, str) and _DATE_RE.fullmatch(val):
                fb.dates.add(val)
            if isinstance(val, list):
                for x in val:
                    if isinstance(x, str) and _DATE_RE.fullmatch(x):
                        fb.dates.add(x)
        bad = check_numbers(answer, fb, codes)
        if bad:
            _rej("unregistered_number", "、".join(sorted(set(bad))[:8]))
            continue

        qk, ak = _norm_q(question), _norm_a(answer)
        if qk in seen_q:
            _rej("dup_question", f"与 {seen_q[qk]} 重复")
            continue
        if ak in seen_a:
            _rej("dup_answer", f"与 {seen_a[ak]} 重复")
            continue
        seen_q[qk] = sid
        seen_a[ak] = sid

        prov["sample_id"] = sid
        kept.append(s)
    return kept, rejects


# ============================ 切分 ============================
def split_by_symbol(samples: list[dict], seed: int) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        groups[str(s["provenance"]["params"].get("symbol") or "UNKNOWN")].append(s)

    keys = sorted(groups)
    random.Random(seed).shuffle(keys)
    total = len(samples)
    targets = {"train": 0.70, "val": 0.15, "test": 0.15}
    out: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    assigned = {"train": 0, "val": 0, "test": 0}
    for k in keys:
        order = sorted(assigned, key=lambda x: assigned[x] / max(targets[x] * total, 1))
        bucket = order[0]
        out[bucket].extend(groups[k])
        assigned[bucket] += len(groups[k])
    return out


def check_leakage(splits: dict[str, list[dict]]) -> dict:
    symbol_map: dict[str, set[str]] = defaultdict(set)
    interval_map: dict[str, set[str]] = defaultdict(set)
    for name, rows in splits.items():
        for s in rows:
            prov = s["provenance"]
            sym = str(prov["params"].get("symbol") or "UNKNOWN")
            symbol_map[sym].add(name)
            interval_map[str(prov.get("interval_key"))].add(name)
    return {
        "symbols_in_multiple_splits": sorted(k for k, v in symbol_map.items() if len(v) > 1),
        "intervals_in_multiple_splits": sorted(k for k, v in interval_map.items() if len(v) > 1),
        "symbols": {k: sorted(v) for k, v in symbol_map.items()},
        "intervals_checked": len(interval_map),
    }


# ============================ 主流程 ============================
DEFAULT_SYMBOLS = "600519,000001,300750,601318,000858,002594,600036,601899,300059,600030"
DEFAULT_STRATEGIES = "ma_cross,rsi_ma200,donchian_breakout"
DEFAULT_WINDOWS = "60,120,250"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="衡策 SFT 合成数据生成器（真实回测/行情为唯一数值源）")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS, help="逗号分隔的 A 股代码")
    ap.add_argument("--strategies", default=DEFAULT_STRATEGIES, help="逗号分隔的策略 id")
    ap.add_argument("--windows", default=DEFAULT_WINDOWS, help="逗号分隔的回测天数")
    ap.add_argument("--capital", type=float, default=200_000.0, help="回测初始资金")
    ap.add_argument("--qty", type=int, default=100, help="持仓样本的股数（一手=100）")
    ap.add_argument("--cost-lookback", type=int, default=60, help="持仓成本取多少个交易日前收盘")
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--out", default=str(BACKEND_ROOT / "data" / "sft_synth"))
    ap.add_argument("--no-negatives", action="store_true", help="跳过负样本生成")
    ap.add_argument("--max-samples", type=int, default=0, help=">0 时截断最终样本数")
    return ap.parse_args()


async def main() -> int:
    args = parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    for st in strategies:
        if st not in STRATEGY_META:
            logger.error(f"未知策略 {st}，可用：{', '.join(sorted(STRATEGY_META))}")
            return 2

    rng = random.Random(args.seed)
    ctx = Ctx()
    skip_log: list[str] = []

    for sym in symbols:
        code = normalize_a_share(sym) or sym
        quote, hist = {}, {}
        try:
            quote = await get_quote(sym)
            hist = await get_historical(sym, max(windows) + 90)
        except Exception as e:
            logger.warning(f"[{sym}] 行情获取异常，跳过：{e}")
            skip_log.append(f"{sym}:fetch_failed:{type(e).__name__}")
            continue

        bars = hist.get("data") or []
        if not bars:
            skip_log.append(f"{sym}:no_bars")
            logger.warning(f"[{sym}] 无历史日线，跳过")
            continue
        ind = compute_indicators(bars)

        err = gen_market(ctx, rng, sym, quote, ind, bars)
        if err:
            skip_log.append(err)
        err = gen_risk(ctx, rng, sym, quote, ind, bars)
        if err:
            skip_log.append(err)
        err = gen_portfolio(ctx, rng, sym, quote, bars, args.cost_lookback, args.qty)
        if err:
            skip_log.append(err)
        gen_prediction_refusals(ctx, sym, quote, ind)

        for days in windows:
            for st in strategies:
                try:
                    result = await run_strategy_backtest(
                        sym, st, initial_capital=args.capital, days=days)
                except Exception as e:
                    skip_log.append(f"{sym}:{st}:{days}:{type(e).__name__}")
                    logger.warning(f"[{sym}] {st} {days}d 回测失败：{e}")
                    continue
                err = gen_backtest(ctx, rng, sym, st, days, args.capital, result)
                if err:
                    skip_log.append(err)

    if not args.no_negatives:
        skip_log.extend(await gen_negatives(ctx, symbols[0] if symbols else "600519"))

    # 掩码时既要挡 sh.600519，也要挡裸 6 位 600519（答案里两种写法都可能出现）
    codes: set[str] = set()
    for s in symbols:
        norm = normalize_a_share(s) or s
        codes.add(norm)
        codes.update(part for part in norm.split(".") if part.isdigit())
    codes.update({"sh.999999", "999999"})
    kept, rejects = run_gate(ctx, codes)
    if args.max_samples > 0:
        kept = kept[: args.max_samples]

    splits = split_by_symbol(kept, args.seed)
    leakage = check_leakage(splits)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _write(path: Path, rows: list[dict]) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    all_rows = splits["train"] + splits["val"] + splits["test"]
    _write(out_dir / "train.jsonl", splits["train"])
    _write(out_dir / "val.jsonl", splits["val"])
    _write(out_dir / "test.jsonl", splits["test"])
    _write(out_dir / "sources.jsonl", [
        {"source_id": k, **v} for k, v in sorted(ctx.sources.items())
    ])
    _write(out_dir / "provenance.jsonl", [
        {**s["provenance"], "split": split}
        for split, rows in splits.items() for s in rows
    ])
    _write(out_dir / "reject.jsonl", rejects)

    reject_counts = Counter(r["reason"] for r in rejects)
    intent_counts = Counter(s["intent"] for s in all_rows)
    source_types = Counter()
    for s in all_rows:
        for sid in s["provenance"]["source_ids"]:
            source_types[ctx.sources.get(sid, {}).get("type", "unknown")] += 1

    manifest = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "symbols": symbols, "strategies": strategies, "windows": windows,
            "initial_capital": args.capital, "qty": args.qty,
            "cost_lookback": args.cost_lookback, "seed": args.seed,
            "negatives": not args.no_negatives,
        },
        "counts": {
            "generated": len(ctx.samples), "kept": len(kept),
            "train": len(splits["train"]), "val": len(splits["val"]),
            "test": len(splits["test"]), "rejected": len(rejects),
            "sources": len(ctx.sources),
        },
        "by_intent": dict(intent_counts),
        "by_source_type": dict(source_types),
        "skipped": skip_log,
        "rejected_by_reason": dict(reject_counts),
        "leakage_check": leakage,
        "intents_covered": sorted(set(intent_counts) - {REFUSAL_INTENT}),
        "intents_skipped": {
            "chitchat": "无真实数据可锚定",
            "alert_config": "纯配置动作，无可回填结果",
            "screener": "全市场扫描，单次成本高且非单标的结论",
            "knowledge_query": "依赖 RAG 语料，无数字可校验",
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("完成：保留 {} / 生成 {}，train/val/test = {}/{}/{}",
                len(kept), len(ctx.samples), len(splits["train"]),
                len(splits["val"]), len(splits["test"]))
    print(json.dumps({
        "out_dir": str(out_dir),
        "kept": len(kept),
        "train": len(splits["train"]), "val": len(splits["val"]), "test": len(splits["test"]),
        "rejected": dict(reject_counts),
        "by_intent": dict(intent_counts),
        "leakage": {
            "symbols_crossing": len(leakage["symbols_in_multiple_splits"]),
            "intervals_crossing": len(leakage["intervals_in_multiple_splits"]),
        },
        "skipped": skip_log[:10],
    }, ensure_ascii=False, indent=2))
    return 0 if kept else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
