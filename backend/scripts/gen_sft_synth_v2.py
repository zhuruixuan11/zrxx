#!/usr/bin/env python
"""衡策 SFT 数据合成 v2：复用 v1 的 FactBook / 闸门 / provenance，重做问法与配比。

与 v1 的差异（都是可核对的）
--------------------------
1. 四类意图各 12 条问句模板，模板带 tone 标签（colloquial / formal / brief / typo /
   multi_need），并叠加子主题（看盘：快照 / 技术面 / 区间；风险：波动 / 回撤 / 位置；
   持仓：盈亏 / 占比 / 成本），答案渲染的字段随子主题变化。
2. 同一标的按真实日线截出 3 个历史交易日快照（不是只换代码），每个快照的数字不同。
3. 多轮样本：看盘→风险、风险→位置、持仓→成本、双标的组合→单只追问，共 2 轮。
   多轮样本的 FactBook 是两轮合并的（numbers / dates 取并集），source_ids 也是并集。
4. 澄清意图 clarification：问题歧义，先反问，不取数、不给数字。
5. 拒答按 data_insufficient / out_of_capability / prediction_refusal 三子类分别计数。

数值仍只来自仓库接口（baostock 日线、回测引擎），答案里每个数字都要先过 FactBook。

用法：
  cd backend
  python scripts/gen_sft_synth_v2.py
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
for _p in (BACKEND_ROOT, BACKEND_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from loguru import logger  # noqa: E402

import gen_sft_synth as v1  # noqa: E402
import report_diversity as diversity  # noqa: E402
from app.agent.indicators import compute_indicators  # noqa: E402
from app.agent.tools import get_historical, normalize_a_share  # noqa: E402
from app.backtest_engine import run_strategy_backtest  # noqa: E402
from app.backtest_engine.runner import STRATEGY_META  # noqa: E402

FactBook = v1.FactBook
DISCLAIMER = v1.DISCLAIMER
SYSTEM_PROMPT = v1.SYSTEM_PROMPT
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

SHORT_DISCLAIMER = "以上仅供学习参考，不构成投资建议。"
REFUSAL_MARKERS = ("无法完成", "无法提供", "不支持", "数据不足", "查不到", "不能提供")
CLARIFICATION_INTENT = "clarification"

DEFAULT_SYMBOLS = (
    "600519,000001,300750,601318,000858,002594,600036,601899,300059,600030,"
    "601398,601288,600900,601088,600028,600887,000568,002304,600276,300760,"
    "002415,000333,000651,601888,600309,600585,601668,000002,601012,002475,"
    "300124,600050,601728,600941,601988,601166,000725,600690,601601,600048"
)
DEFAULT_STRATEGIES = "ma_cross,macd,donchian_breakout,boll_reversion"

# 快照偏移：0=最新交易日，20/40=往前推 20/40 个交易日的真实日线截断点
SNAPSHOT_OFFSETS = (0, 20, 40)
# 持仓成本回看：买入日 = 快照日往前推 N 个交易日的收盘
COST_LOOKBACKS = (40, 60, 80)


# ============================ 问句模板 ============================
# 12 条/意图；tone: k=口语 f=正式 m=极简 t=错别字/倒装 x=多重需求
TONE_ALIAS = {
    "colloquial": "colloquial", "formal": "formal", "brief": "brief",
    "typo": "typo", "multi_need": "multi_need",
}

MARKET_TEMPLATES = [
    ("{code} 这票最近咋样", "colloquial"),
    ("看下 {code} 现在什么情况", "brief"),
    ("请提供 {code} 的行情与技术指标", "formal"),
    ("{code} 行情", "brief"),
    ("帮我瞅一眼 {code}，价格和指标都说下", "colloquial"),
    ("{code} 最近表现咋样，涨跌多少", "colloquial"),
    ("请分析 {code} 当前的技术面与区间表现", "formal"),
    ("{code} 技术面和涨跌幅", "brief"),
    ("{code} 现在啥价，近 5 日和 20 日涨跌多少", "multi_need"),
    ("{code} 今儿走势如何，量能咋样", "colloquial"),
    ("麻烦看下 {code}，把价格、均线和 20 日涨跌幅列一下", "formal"),
    ("{code} 均线现在啥状态，还有那个RSI", "typo"),
]

RISK_TEMPLATES = [
    ("{code} 最近风险大吗", "colloquial"),
    ("帮我看下 {code} 的波动风险", "colloquial"),
    ("请评估 {code} 的波动与回撤风险", "formal"),
    ("{code} 风险", "brief"),
    ("这票 {code} 拿得住吗，波动和回撤说说", "colloquial"),
    ("{code} 的波动率、回撤、位置分别是多少", "multi_need"),
    ("请给出 {code} 的风险画像：波动率、最大回撤、60 日位置", "formal"),
    ("{code} 波动回撤位置", "brief"),
    ("{code} 最近是不是波动变大了，回撤呢", "typo"),
    ("我想知道 {code} 现在风险算高还是低，用数据说", "formal"),
    ("{code} 这位置安不安全，前面那波回撤多少", "colloquial"),
    ("{code} 风险咋样啊，最近老上下跳", "typo"),
]

PORTFOLIO_TEMPLATES = [
    ("我在 {buy_date} 收盘买了 {code} {qty} 股，现在赚了多少", "colloquial"),
    ("{code} {qty} 股，成本按 {buy_date} 收盘算，帮我算下盈亏", "brief"),
    ("请根据 {buy_date} 收盘价与最新价，计算 {code} {qty} 股的浮动盈亏", "formal"),
    ("{code} 我{buy_date}收盘进的{qty}股，现在咋样了", "typo"),
    ("我分两笔买的 {code}，一笔 {buy_date} 收盘 {qty1} 股、一笔 {buy_date2} 收盘 {qty2} 股，现在两笔各占多少", "colloquial"),
    ("{code} 两笔持仓的占比，分别是 {qty1} 股和 {qty2} 股", "brief"),
    ("请拆分 {code} 两笔持仓（{buy_date} 收盘 {qty1} 股、{buy_date2} 收盘 {qty2} 股）的市值占比与综合成本", "formal"),
    ("{code} 两笔仓位占比分别多少，帮我拆下", "typo"),
    ("{code} 我成本是 {buy_date} 收盘价，比均线高还是低", "colloquial"),
    ("{code} 成本对 MA20 和 MA60", "brief"),
    ("请对比 {code} 的持仓成本价与 MA20、MA60 的位置关系", "formal"),
    ("{code} 成本、现价、均线放一起比一下，我这笔是赚是亏、贵不贵", "multi_need"),
]

BACKTEST_TEMPLATES = [
    ("用「{sname}」回测 {code} 最近 {n} 个交易日", "colloquial"),
    ("{code}「{sname}」{n} 日回测", "brief"),
    ("请对 {code} 运行「{sname}」策略回测，区间取最近 {n} 个交易日", "formal"),
    ("{code} 拿「{sname}」跑 {n} 天回测，看看收益回撤", "colloquial"),
    ("{code} 回测「{sname}」{n} 天", "brief"),
    ("帮我验证「{sname}」在 {code} 上最近 {n} 个交易日的表现", "formal"),
    ("{code} 用「{sname}」跑一下，{n} 个交易日，胜率多少", "colloquial"),
    ("{n} 个交易日、「{sname}」、{code}，回测", "typo"),
    ("回测 {code} {n} 天 策略{sname}", "typo"),
    ("{code}「{sname}」{n} 日，收益、回撤、胜率、夏普都给我", "multi_need"),
    ("麻烦跑下 {code} 的「{sname}」回测，{n} 个交易日，期末权益也报一下", "formal"),
    ("{code} 这票最近 {n} 天用「{sname}」做会咋样", "colloquial"),
]

# 子主题 → 答案里重点渲染的字段
MARKET_SUBTOPICS = ("snapshot", "technical", "range")
RISK_SUBTOPICS = ("volatility", "drawdown", "position")
PORTFOLIO_SUBTOPICS = ("pnl", "share", "cost")
BACKTEST_SUBTOPICS = ("metrics", "drawdown", "trades")

# 多轮的第二轮问句（都不带代码，靠上一轮上下文）
MT_MARKET_RISK_Q = [
    "那它最近风险大吗",
    "这只票波动和回撤呢",
    "风险呢，用数据说下",
    "顺便说下它现在的位置和波动",
]
MT_RISK_POSITION_Q = [
    "那它在 60 日区间里处在什么位置",
    "位置高不高，给我个数",
    "再说下现在的分位和区间上下沿",
]
MT_PORTFOLIO_COST_Q = [
    "按这个成本，跟均线比算高吗",
    "这笔成本相对 MA20 和 MA60 呢",
    "那我这个买价放在均线里算什么水平",
]
MT_PAIR_OPEN_Q = [
    ("我 {d1} 收盘买了 {a} {q} 股、{d2} 收盘买了 {b} {q} 股，现在整体赚了多少", "colloquial"),
    ("帮我看下两笔持仓：{a} 是 {d1} 收盘买的 {q} 股，{b} 是 {d2} 收盘买的 {q} 股，合计盈亏多少", "formal"),
    ("手里有两只：{a}（{d1} 收盘，{q} 股）和 {b}（{d2} 收盘，{q} 股），现在总共赚了没", "typo"),
]
MT_PAIR_FOLLOW_Q = [
    "其中 {a} 呢，单独算一下",
    "那 {a} 这只单独看呢",
    "{a} 那笔单独给我",
]

# 澄清：8 种反问文案，每种配不同的歧义问句（问句里出现的数字不影响闸门，闸门只看答案）
CLARIFY_STYLES = [
    {
        "name": "整体_or_单只",
        "q": ["帮我看看", "看看我的持仓", "整体情况怎么样", "帮我看下这个"],
        "a": "你是指整体组合还是单只标的？如果看整体，我需要你说明包含哪几笔持仓；"
             "如果看单只，请给出六位代码和想看的维度（行情、风险或盈亏）。",
    },
    {
        "name": "维度不明",
        "q": ["分析一下", "帮我分析", "给点分析", "随便分析下"],
        "a": "你希望分析哪一面：行情快照、技术面、波动风险，还是持仓盈亏？"
             "确认一个维度我就按对应数据口径给出结果，不会混着答。",
    },
    {
        "name": "指代不清",
        "q": ["这票行不行", "这个票怎么样", "那个票现在多少", "这只有没有风险"],
        "a": "你指的是哪一只标的？「这个」在当前对话里没有对应代码，请补充六位代码，"
             "我再按你指定的标的取数回答。",
    },
    {
        "name": "缺成本",
        "q": ["能帮我算下收益吗", "持仓帮我算一下", "我赚了多少", "帮我算算盈亏"],
        "a": "算收益需要三样东西：标的代码、买入日期、持股数量。你现在只说了要算收益，"
             "这三项分别是什么？补齐后我才能用真实收盘价和最新价计算。",
    },
    {
        "name": "缺标的与策略",
        "q": ["帮我跑个回测", "回测一下", "测测这个策略", "跑个策略看看"],
        "a": "回测需要先确定标的和策略。请给出六位 A 股代码，以及想用的策略名"
             "（均线、MACD、唐奇安、布林带等）——你想先用哪一个？",
    },
    {
        "name": "多标的",
        "q": ["帮我看看 600519 和 000001", "这两只都分析下", "对比一下这两只票", "两只都看"],
        "a": "一次只处理一个标的，避免把两只票的数据混在一起。我给出第一只的行情与指标后，"
             "再看另一只——你想先看哪一只？",
    },
    {
        "name": "要建议",
        "q": ["最近咋操作", "现在能买吗", "该加仓还是减仓", "值不值得拿"],
        "a": "我不输出买卖建议。可以选一个可验证的方向：行情与指标、波动与回撤、"
             "或某个策略的历史回测结果。你想选哪一个？",
    },
    {
        "name": "时间范围不明",
        "q": ["最近行情怎么样", "这段时间表现如何", "近期走势", "最近怎么样"],
        "a": "「最近」需要落到具体区间：近五个交易日、近二十个交易日，还是近六十个交易日的区间表现？"
             "另外也请补充标的代码，我按你选的区间取数。",
    },
]

# 能力边界（out_of_capability）：每类带 2 条问法，按标的轮换
OOC_SPECS = [
    {
        "key": "tick",
        "q": ["能看到 {code} 的逐笔委托和十档盘口吗", "{code} 的十档挂单能拉出来吗"],
        "why": "当前数据源只有日线与实时快照，没有逐笔成交与十档盘口",
        "missing": "交易所逐笔委托与逐笔成交行情（需付费授权）",
        "offer": "日线 OHLCV、最新价与涨跌幅、成交量，以及基于日线的技术指标",
        "next": "用日线行情和指标帮你做盘面分析",
        "params": {"request": "tick_level2"},
        "evidence": "app/agent/tools.py 数据源仅 baostock / 新浪 / 东财快照 / yfinance",
    },
    {
        "key": "minute_bar",
        "q": ["帮我用 5 分钟 K 线回测 {code} 的均线策略", "{code} 能不能做 30 分钟级别的回测"],
        "why": "回测引擎只按日线撮合，信号基于上一根日 K、本根开盘成交",
        "missing": "分钟级历史行情与对应的高频撮合、滑点模型",
        "offer": "日线级别的策略回测，以及日线行情快照与指标",
        "next": "把策略改成日线周期再跑",
        "params": {"frequency": "5m"},
        "evidence": "app/backtest_engine/runner.py 仅接受日线 bars",
    },
    {
        "key": "institutional",
        "q": ["{code} 最近一个季度机构持仓明细是多少", "{code} 的十大股东和基金持仓能查吗"],
        "why": "系统没有接入股东名册与机构持仓数据",
        "missing": "定期报告披露的十大股东 / 机构持仓明细数据源",
        "offer": "价格、成交量、技术指标与策略回测结果",
        "next": "用行情和回测帮你判断当前的价格位置",
        "params": {"request": "institutional_holding"},
        "evidence": "app/agent/tools.py 未提供股东 / 机构持仓接口",
    },
    {
        "key": "fund_flow",
        "q": ["{code} 今天的北向资金净流入多少", "{code} 的主力资金流向帮我拉一下"],
        "why": "当前数据源不包含资金流向与北向持股明细",
        "missing": "逐日资金流向 / 北向持股数据源",
        "offer": "价格、成交量与量比（5 日均量 / 20 日均量）等日线量价指标",
        "next": "用量价指标帮你观察成交活跃度",
        "params": {"request": "fund_flow"},
        "evidence": "app/agent/tools.py 只有 OHLCV 与快照字段",
    },
    {
        "key": "news",
        "q": ["{code} 最近有什么利好消息，帮我看下新闻", "{code} 的公告和研报能汇总一下吗"],
        "why": "系统没有接入新闻、公告与研报语料，无法做消息面解读",
        "missing": "新闻 / 公告 / 研报的文本数据源及对应检索",
        "offer": "日线行情、技术指标与策略回测结果（纯量价视角）",
        "next": "从量价和波动角度帮你做客观描述",
        "params": {"request": "news_sentiment"},
        "evidence": "项目未接入 RAG 新闻语料",
    },
    {
        "key": "options",
        "q": ["{code} 有没有对应的期权，隐含波动率多少", "帮我用期权对冲 {code} 的持仓"],
        "why": "系统只覆盖 A 股现货日线，没有期权链与隐含波动率数据",
        "missing": "期权合约、隐含波动率与希腊值数据源",
        "offer": "A 股现货的行情、波动率与日线回测",
        "next": "用现货的波动率指标评估风险",
        "params": {"request": "options_chain"},
        "evidence": "app/agent/tools.py 无期权接口",
    },
    {
        "key": "futures",
        "q": ["用均线策略回测下 {code} 对应的股指期货", "帮我看看 {code} 相关期货的持仓量"],
        "why": "系统未接入期货行情与合约规则",
        "missing": "期货合约行情、保证金与换月规则",
        "offer": "A 股现货的日线行情与单标的回测",
        "next": "改用现货标的做同样的策略回测",
        "params": {"request": "futures"},
        "evidence": "回测引擎只支持 A 股现货",
    },
    {
        "key": "macro",
        "q": ["结合最新 CPI 和社融数据，解读一下 {code} 的基本面", "{code} 的估值分位数和行业景气度是多少"],
        "why": "系统没有接入宏观、行业景气与估值分位数据",
        "missing": "宏观指标、行业景气度与估值历史分位数据源",
        "offer": "个股日线行情、技术指标与策略回测",
        "next": "从量价角度帮你描述当前状态",
        "params": {"request": "macro_fundamental"},
        "evidence": "项目未接入财报与宏观数据库",
    },
]

# 预测 / 目标价：拒答但给出真实的历史统计量
PREDICTION_SPECS = [
    ("{code} 明天会涨吗？能到多少？", "direction"),
    ("给我一个 {code} 的目标价，止损位设在哪？", "target_price"),
    ("{code} 下周走势你怎么看，我该买还是卖", "advice"),
    ("{code} 能涨到 100 元吗", "target_price"),
    ("帮我预测下 {code} 明天开盘价", "direction"),
    ("{code} 后面还有几个涨停", "direction"),
]


# ============================ 样本构建 ============================
class Ctx2(v1.Ctx):
    """支持多轮与 subtype / tone。"""

    def emit(self, *, intent: str, task: str, turns: list[tuple[str, str, str]],
             fb: FactBook, source_ids: list[str], params: dict,
             interval_key: str, tool_calls: list[dict], evidence: str = "",
             subtype: str = "", tone: str = "", extra: dict | None = None) -> None:
        """turns = [(user_question, data_block, assistant_answer), ...]"""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for q, data_block, ans in turns:
            content = q if not data_block else f"{q}\n\n<data>\n{data_block}\n</data>"
            messages.append({"role": "user", "content": content})
            messages.append({"role": "assistant", "content": ans})
        prov = {
            "sample_id": "",
            "intent": intent,
            "task": task,
            "subtype": subtype,
            "tone": tone,
            "turns": len(turns),
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
        prov.update(extra or {})
        self.samples.append({
            "sample_id": self.next_id(),
            "intent": intent,
            "task": task,
            "subtype": subtype,
            "tone": tone,
            "messages": messages,
            "tool_trace": tool_calls,
            "provenance": prov,
        })


def _snapshot(bars: list, offset: int):
    """从真实日线里截出「往前 offset 个交易日」的收盘快照。"""
    if offset < 0 or len(bars) - offset < 80:
        return None
    sub = bars[: len(bars) - offset]
    bar, prev = sub[-1], sub[-2]
    price, prev_close = float(bar["close"]), float(prev["close"])
    if price <= 0 or prev_close <= 0:
        return None
    quote = {
        "trade_date": str(bar["date"]),
        "current_price": price,
        "previous_close": prev_close,
        "open": float(bar.get("open") or price),
        "high": float(bar.get("high") or price),
        "low": float(bar.get("low") or price),
        "volume": int(float(bar.get("volume") or 0)),
        "change": price - prev_close,
        "change_percent": (price / prev_close - 1) * 100,
    }
    ind = compute_indicators(sub)
    if not ind or ind.get("ma20") is None or ind.get("rsi14") is None:
        return None
    return sub, quote, ind


def _drawdown60(bars: list) -> tuple[float, str, str] | None:
    """近 60 根日线的最大回撤（峰→谷），返回百分比与峰谷日期。"""
    sub = bars[-60:]
    if len(sub) < 20:
        return None
    peak_i, best, best_pair = 0, 0.0, (0, 0)
    for i in range(1, len(sub)):
        if float(sub[i]["close"]) > float(sub[peak_i]["close"]):
            peak_i = i
        dd = (float(sub[peak_i]["close"]) - float(sub[i]["close"])) / float(sub[peak_i]["close"]) * 100
        if dd > best:
            best, best_pair = dd, (peak_i, i)
    return best, str(sub[best_pair[0]]["date"]), str(sub[best_pair[1]]["date"])


def _market_data_block(code: str, q: dict, ind: dict, fb: FactBook) -> tuple[str, dict]:
    """行情 + 指标全字段数据块（答案按子主题取子集渲染）。"""
    trade_date = str(q["trade_date"])
    fb.d(trade_date)
    fb.c("ma_short_window", 5, "indicator_window")
    fb.c("ma_long_window", 20, "indicator_window")
    fb.c("rsi_window", 14, "indicator_window")
    fb.c("window_60", 60, "indicator_window")
    vals = {
        "price": fb.n("current_price", q["current_price"], 2),
        "prev": fb.n("previous_close", q["previous_close"], 2),
        "chg": fb.n("change", q["change"], 2, signed=True),
        "pct": fb.n("change_percent", q["change_percent"], 2, signed=True),
        "high": fb.n("high", q["high"], 2),
        "low": fb.n("low", q["low"], 2),
        "vol": fb.n("volume", q["volume"], 0),
        "ma5": fb.n("ma5", ind["ma5"], 2),
        "ma20": fb.n("ma20", ind["ma20"], 2),
        "rsi": fb.n("rsi14", ind["rsi14"], 2),
        "ret5": fb.n("ret5", ind["ret5"], 2, signed=True),
        "ret20": fb.n("ret20", ind["ret20"], 2, signed=True),
        "hi60": fb.n("high60", ind["high60"], 2),
        "lo60": fb.n("low60", ind["low60"], 2),
        "pos60": fb.n("pos60", ind["pos60"], 1),
    }
    if ind.get("ma60") is not None:
        vals["ma60"] = fb.n("ma60", ind["ma60"], 2)
    if ind.get("vol_ratio_5_20") is not None:
        vals["vr"] = fb.n("vol_ratio_5_20", ind["vol_ratio_5_20"], 2)
    lines = [
        f"标的：{code}", f"交易日：{trade_date}",
        f"最新价：{vals['price']} 元", f"昨收：{vals['prev']} 元",
        f"涨跌：{vals['chg']} 元（{vals['pct']}%）",
        f"最高 / 最低：{vals['high']} / {vals['low']} 元",
        f"成交量：{vals['vol']} 股",
        f"MA5：{vals['ma5']} 元", f"MA20：{vals['ma20']} 元",
        f"RSI14：{vals['rsi']}",
        f"近 5 日涨跌幅：{vals['ret5']}%", f"近 20 日涨跌幅：{vals['ret20']}%",
        f"60 日区间：{vals['lo60']} ~ {vals['hi60']} 元，当前分位 {vals['pos60']}%",
    ]
    if "ma60" in vals:
        lines.append(f"MA60：{vals['ma60']} 元")
    if "vr" in vals:
        lines.append(f"5 日均量 / 20 日均量：{vals['vr']}")
    lines.append("数据源：baostock 前复权日线（该交易日收盘快照）")
    return "\n".join(lines), vals


# ============================ 生成：看盘 ============================
def gen_market_v2(ctx: Ctx2, code: str, snap, *, template: str, tone: str,
                  subtopic: str, offset: int) -> str | None:
    sub, q, ind = snap
    trade_date = str(q["trade_date"])
    fb = FactBook()
    block, v = _market_data_block(code, q, ind, fb)
    sid = f"quote:{code}:{trade_date}"
    ctx.source(sid, {"type": "quote", "source_id": sid, "symbol": code,
                     "trade_date": trade_date, "data_source": "baostock 日线快照",
                     "payload": {"current_price": q["current_price"],
                                 "previous_close": q["previous_close"],
                                 "high": q["high"], "low": q["low"],
                                 "volume": q["volume"],
                                 "change_percent": round(q["change_percent"], 4)}})

    above = "之上" if q["current_price"] >= max(ind["ma5"], ind["ma20"]) else (
        "之下" if q["current_price"] <= min(ind["ma5"], ind["ma20"]) else "之间")
    if subtopic == "snapshot":
        body = [
            f"{code} 在 {trade_date} 收于 {v['price']} 元，较前一交易日收盘 {v['prev']} 元"
            f"变动 {v['chg']} 元（{v['pct']}%）；当日最高 {v['high']} 元、最低 {v['low']} 元，"
            f"成交量 {v['vol']} 股。",
            f"该交易日所属区间为 60 日 {v['lo60']} ~ {v['hi60']} 元，当前分位 {v['pos60']}%。",
            "以上是这一交易日收盘后的行情事实，不含方向判断。" + SHORT_DISCLAIMER,
        ]
    elif subtopic == "technical":
        body = [
            f"{code} 在 {trade_date} 收于 {v['price']} 元；MA5 为 {v['ma5']} 元、MA20 为 {v['ma20']} 元，"
            f"收盘价位于两条均线{above}，RSI14 为 {v['rsi']}。",
            f"量能方面，5 日均量与 20 日均量之比为 {v['vr']}，近 5 日涨跌幅 {v['ret5']}%。",
            "以上只陈述指标读数与相对位置，不构成买卖判断。" + SHORT_DISCLAIMER,
        ]
    elif subtopic == "range":
        body = [
            f"{code} 近 5 个交易日涨跌幅为 {v['ret5']}%，近 20 个交易日为 {v['ret20']}%。",
            f"以 {trade_date} 为终点，60 个交易日的价格区间是 {v['lo60']} ~ {v['hi60']} 元，"
            f"收盘价 {v['price']} 元处于区间 {v['pos60']}% 分位。",
            "区间统计只描述已经发生的价格分布，不预测下一段区间。" + SHORT_DISCLAIMER,
        ]
    else:
        raise ValueError(f"unknown market subtopic {subtopic}")

    ctx.emit(
        intent="market_query", task=f"quote_{subtopic}",
        turns=[(template.format(code=code), block, "\n".join(body))],
        fb=fb, source_ids=[sid],
        params={"symbol": code, "symbols": [code], "trade_date": trade_date,
                "snapshot_offset": offset},
        interval_key=f"{code}|{trade_date}|market_{subtopic}",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 340}, "ok": True, "source_id": sid}],
        evidence="app/agent/tools.py::get_historical + app/agent/indicators.py::compute_indicators",
        subtype=subtopic, tone=tone,
    )
    return None


# ============================ 生成：风险 ============================
def gen_risk_v2(ctx: Ctx2, code: str, snap, *, template: str, tone: str,
                subtopic: str, offset: int) -> str | None:
    sub, q, ind = snap
    trade_date = str(q["trade_date"])
    dd = _drawdown60(sub)
    if dd is None:
        return f"drawdown_missing:{code}"
    mdd, peak_d, trough_d = dd
    fb = FactBook()
    fb.d(trade_date)
    fb.c("high_vol_threshold_pct", 5, "code_rule:agent/nodes.py")
    fb.c("mid_vol_threshold_pct", 2, "code_rule:agent/nodes.py")
    fb.c("window_20", 20, "code_constant")
    fb.c("window_60", 60, "code_constant")
    pct_abs = abs(float(q["change_percent"]))
    level = "高波动" if pct_abs > 5 else ("中等波动" if pct_abs > 2 else "相对稳定")
    vals = {
        "price": fb.n("current_price", q["current_price"], 2),
        "pct": fb.n("change_percent", q["change_percent"], 2, signed=True),
        "vol20": fb.n("volatility20", ind["volatility20"], 1),
        "hi60": fb.n("high60", ind["high60"], 2),
        "lo60": fb.n("low60", ind["low60"], 2),
        "pos60": fb.n("pos60", ind["pos60"], 1),
        "mdd": fb.n("max_drawdown_60d_pct", mdd, 2),
    }
    fb.d(peak_d)
    fb.d(trough_d)
    if ind.get("ret20") is not None:
        vals["ret20"] = fb.n("ret20", ind["ret20"], 2, signed=True)
    if ind.get("vol_ratio_5_20") is not None:
        vals["vr"] = fb.n("vol_ratio_5_20", ind["vol_ratio_5_20"], 2)
    gap = (ind["high60"] - q["current_price"]) / ind["high60"] * 100 if ind["high60"] else 0.0
    vals["gap_hi"] = fb.n("gap_to_high60_pct", gap, 2)

    block = "\n".join([
        f"标的：{code}", f"交易日：{trade_date}",
        f"当日涨跌幅：{vals['pct']}%",
        f"20 日年化波动率：{vals['vol20']}%",
        f"近 60 个交易日最大回撤：{vals['mdd']}%（峰值日 {peak_d}，谷底日 {trough_d}）",
        f"60 日区间：{vals['lo60']} ~ {vals['hi60']} 元，当前分位 {vals['pos60']}%",
        f"当前价距 60 日高点：{vals['gap_hi']}%",
        "数据源：baostock 前复权日线（该交易日收盘快照）",
    ])
    sid = f"risk:{code}:{trade_date}"
    ctx.source(sid, {"type": "risk_snapshot", "source_id": sid, "symbol": code,
                     "trade_date": trade_date,
                     "indicators": {k: ind.get(k) for k in
                                    ("volatility20", "pos60", "high60", "low60",
                                     "ret20", "rsi14", "ma20", "vol_ratio_5_20")},
                     "max_drawdown_60d_pct": round(mdd, 4),
                     "drawdown_peak_date": peak_d, "drawdown_trough_date": trough_d})

    rule = "按内置分档（单日涨跌幅超过 5% 记高波动、超过 2% 记中等波动），当日属于"
    if subtopic == "volatility":
        body = [
            f"{code} 在 {trade_date} 的 20 日年化波动率为 {vals['vol20']}%，"
            f"5 日均量 / 20 日均量 {vals['vr']}，当日涨跌幅 {vals['pct']}%。",
            f"{rule}「{level}」。近 20 日累计涨跌幅 {vals['ret20']}%。",
            "波动率只描述历史价格离散度，不含方向判断。" + SHORT_DISCLAIMER,
        ]
    elif subtopic == "drawdown":
        body = [
            f"以 {trade_date} 为终点回看 60 个交易日，{code} 的最大回撤为 {vals['mdd']}%，"
            f"峰值出现在 {peak_d}，谷底出现在 {trough_d}。",
            f"当前价 {vals['price']} 元距 60 日高点 {vals['hi60']} 元还有 {vals['gap_hi']}%，"
            f"处于区间 {vals['pos60']}% 分位。",
            "回撤是已发生的峰谷落差，不代表后续还会跌多少。" + SHORT_DISCLAIMER,
        ]
    elif subtopic == "position":
        body = [
            f"{code} 在 {trade_date} 收于 {vals['price']} 元，60 日区间 {vals['lo60']} ~ {vals['hi60']} 元，"
            f"当前分位 {vals['pos60']}%，20 日年化波动率 {vals['vol20']}%。",
            f"{rule}「{level}」；当日涨跌幅 {vals['pct']}%。",
            "位置指标只说明价格在历史区间中的相对高低，不构成择时建议。" + SHORT_DISCLAIMER,
        ]
    else:
        raise ValueError(f"unknown risk subtopic {subtopic}")

    ctx.emit(
        intent="risk_assessment", task=f"risk_{subtopic}",
        turns=[(template.format(code=code), block, "\n".join(body))],
        fb=fb, source_ids=[sid],
        params={"symbol": code, "symbols": [code], "trade_date": trade_date,
                "snapshot_offset": offset},
        interval_key=f"{code}|{trade_date}|risk_{subtopic}",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 340}, "ok": True, "source_id": sid}],
        evidence="app/agent/indicators.py::compute_indicators（volatility20/pos60）+ 60 日峰谷回撤",
        subtype=subtopic, tone=tone,
    )
    return None


# ============================ 生成：持仓 ============================
def _lot(bars: list, lookback: int, qty: int) -> dict | None:
    if len(bars) <= lookback + 1:
        return None
    bar = bars[-lookback - 1]
    cost = float(bar["close"])
    if cost <= 0:
        return None
    return {"buy_date": str(bar["date"]), "cost": cost, "qty": qty,
            "cost_amt": cost * qty, "lookback": lookback}


def gen_portfolio_v2(ctx: Ctx2, code: str, snap, *, template: str, tone: str,
                     subtopic: str, qtys: tuple[int, ...],
                     lookbacks: tuple[int, ...]) -> str | None:
    sub, q, ind = snap
    trade_date = str(q["trade_date"])
    price = float(q["current_price"])
    lots = [_lot(sub, lb, qty) for lb, qty in zip(lookbacks, qtys)]
    if len(lots) != len(lookbacks) or any(l is None for l in lots):
        return f"portfolio_lot_missing:{code}"

    fb = FactBook()
    fb.d(trade_date)
    fb.c("lot_size", 100, "code_constant")
    p = fb.n("current_price", price, 2)
    for i, lot in enumerate(lots, 1):
        fb.d(lot["buy_date"])
        fb.c(f"qty_{i}", lot["qty"], "user_param")
        fb.c(f"lookback_{i}", lot["lookback"], "param")
        lot["cost_s"] = fb.n(f"cost_price_{i}", lot["cost"], 2)
        lot["cost_amt_s"] = fb.n(f"cost_amount_{i}", lot["cost_amt"], 2)
        lot["mv"] = price * lot["qty"]
        lot["mv_s"] = fb.n(f"market_value_{i}", lot["mv"], 2)
        lot["pnl"] = lot["mv"] - lot["cost_amt"]
        lot["pnl_s"] = fb.n(f"unrealized_pnl_{i}", lot["pnl"], 2, signed=True)
        lot["pct"] = lot["pnl"] / lot["cost_amt"] * 100
        lot["pct_s"] = fb.n(f"unrealized_pnl_pct_{i}", lot["pct"], 2, signed=True)

    data_lines = [f"标的：{code}", f"最新交易日：{trade_date}", f"最新价：{p} 元"]
    for i, lot in enumerate(lots, 1):
        data_lines += [
            f"第 {i} 笔买入日：{lot['buy_date']}（当日收盘价 {lot['cost_s']} 元）",
            f"第 {i} 笔数量：{lot['qty']} 股；成本金额 {lot['cost_amt_s']} 元；市值 {lot['mv_s']} 元；"
            f"浮动盈亏 {lot['pnl_s']} 元（{lot['pct_s']}%）",
        ]
    data_lines.append("数据源：baostock 前复权日线（各自交易日收盘）")

    if subtopic == "pnl":
        lot = lots[0]
        body = [
            f"按 {lot['buy_date']} 收盘价 {lot['cost_s']} 元买入 {lot['qty']} 股，"
            f"成本 {lot['cost_amt_s']} 元；"
            f"{trade_date} 收盘价 {p} 元，对应市值 {lot['mv_s']} 元。",
            f"浮动盈亏 {lot['pnl_s']} 元，收益率 {lot['pct_s']}%。",
            "计算只含价格变动，未计佣金、印花税与分红。" + SHORT_DISCLAIMER,
        ]
        subtype = "pnl"
    elif subtopic == "share":
        mv_total = sum(l["mv"] for l in lots)
        cost_total = sum(l["cost_amt"] for l in lots)
        pnl_total = mv_total - cost_total
        pct_total = pnl_total / cost_total * 100
        w = [l["mv"] / mv_total * 100 for l in lots]
        fb.n("market_value_total", mv_total, 2)
        fb.n("cost_amount_total", cost_total, 2)
        fb.n("unrealized_pnl_total", pnl_total, 2, signed=True)
        fb.n("unrealized_pnl_total_pct", pct_total, 2, signed=True)
        w_s = [fb.n(f"weight_{i}", x, 2) for i, x in enumerate(w, 1)]
        body = [
            f"两笔 {code} 分别是 {lots[0]['qty']} 股和 {lots[1]['qty']} 股："
            f"{lots[0]['buy_date']} 收盘成本 {lots[0]['cost_s']} 元，"
            f"市值 {lots[0]['mv_s']} 元，占这只票持仓市值 {w_s[0]}%；"
            f"{lots[1]['buy_date']} 收盘成本 {lots[1]['cost_s']} 元，市值 {lots[1]['mv_s']} 元，"
            f"占比 {w_s[1]}%。",
            f"两笔合计成本 {fb.n('cost_echo', cost_total, 2)} 元、市值 "
            f"{fb.n('mv_echo', mv_total, 2)} 元，浮动盈亏 {fb.n('pnl_echo', pnl_total, 2, signed=True)} 元"
            f"（{fb.n('pct_echo', pct_total, 2, signed=True)}%）。",
            "占比按两笔持仓的当前市值计算，不含其他标的与现金。" + SHORT_DISCLAIMER,
        ]
        subtype = "share"
    elif subtopic == "cost":
        lot = lots[0]
        fb.c("ma_window_20", 20, "indicator_window")
        fb.c("ma_window_60", 60, "indicator_window")
        ma20, ma60 = ind["ma20"], ind.get("ma60")
        d20 = (lot["cost"] - ma20) / ma20 * 100 if ma20 else 0.0
        body = [
            f"你的成本价 {lot['cost_s']} 元是 {lot['buy_date']} 的收盘价，当前价 {p} 元；"
            f"MA20 为 {fb.n('ma20', ma20, 2)} 元，MA60 为 "
            f"{fb.n('ma60', ma60, 2) if ma60 is not None else '- '} 元。",
            f"成本相对 MA20 {'高' if d20 >= 0 else '低'} {fb.n('cost_vs_ma20_pct', abs(d20), 2)}%；"
            f"当前价相对成本 {fb.n('price_vs_cost_pct', abs((price - lot['cost']) / lot['cost'] * 100), 2)}%"
            f"（{'浮盈' if price >= lot['cost'] else '浮亏'}）。",
            "均线是历史收盘的算术平均，不等于你的持仓成本线，只作参照。" + SHORT_DISCLAIMER,
        ]
        subtype = "cost"
    else:
        raise ValueError(f"unknown portfolio subtopic {subtopic}")

    source_ids = [f"quote:{code}:{trade_date}"]
    for lot in lots:
        sid_h = f"hist:{code}:{lot['buy_date']}"
        ctx.source(sid_h, {"type": "hist_bar", "source_id": sid_h, "symbol": code,
                           "trade_date": lot["buy_date"], "close": lot["cost"],
                           "data_source": "baostock"})
        source_ids.append(sid_h)
    sid_q = source_ids[0]
    ctx.source(sid_q, {"type": "quote", "source_id": sid_q, "symbol": code,
                       "trade_date": trade_date, "data_source": "baostock 日线快照",
                       "payload": {"current_price": price}})

    fills = {"buy_date": lots[0]["buy_date"], "buy_date2": lots[-1]["buy_date"],
             "code": code, "qty": lots[0]["qty"], "qty1": lots[0]["qty"],
             "qty2": lots[-1]["qty"]}
    ctx.emit(
        intent="portfolio_query", task=f"holding_{subtype}",
        turns=[(template.format(**fills), "\n".join(data_lines), "\n".join(body))],
        fb=fb, source_ids=source_ids,
        params={"symbol": code, "symbols": [code], "qty": lots[0]["qty"],
                "qtys": [l["qty"] for l in lots],
                "buy_dates": [l["buy_date"] for l in lots],
                "current_date": trade_date},
        interval_key=f"{code}|{'+'.join(l['buy_date'] for l in lots)}|{trade_date}"
                     f"|portfolio_{subtype}",
        tool_calls=[
            {"step": 1, "tool": "get_historical",
             "args": {"symbol": code, "days": 340}, "ok": True, "source_id": source_ids[1]},
            {"step": 2, "tool": "get_quote", "args": {"symbol": code},
             "ok": True, "source_id": sid_q},
        ],
        evidence="app/agent/tools.py::get_historical（买入日收盘）+ 该交易日快照收盘",
        subtype=subtype, tone=tone,
    )
    return None


# ============================ 生成：回测 ============================
def gen_backtest_v2(ctx: Ctx2, code: str, strategy: str, requested_days: int,
                    capital: float, result: dict, *, template: str, tone: str,
                    subtopic: str) -> str | None:
    metrics = result.get("metrics") or {}
    curve = result.get("equity_curve") or []
    if not metrics or not curve:
        return f"backtest_empty:{code}:{strategy}"
    if result.get("warning"):
        return f"backtest_lot_blocked:{code}:{strategy}:{requested_days}"
    if int(metrics.get("trades_count") or 0) == 0:
        return f"no_trades:{code}:{strategy}:{requested_days}"

    start_date, end_date = str(curve[0]["date"]), str(curve[-1]["date"])
    bars_count = int(result.get("bars_count") or len(curve))
    sname = STRATEGY_META.get(strategy, {}).get("name", strategy)
    params = result.get("params") or {}
    bh = hashlib.md5(json.dumps(
        {"symbol": code, "strategy": strategy, "days": bars_count, "capital": capital,
         "params": params, "end": end_date},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
    bid = f"bt:{code}:{strategy}:{bars_count}:{int(capital)}:{bh}"
    ctx.source(bid, {"type": "backtest", "source_id": bid, "symbol": code,
                     "strategy": strategy, "strategy_name": sname, "params": params,
                     "requested_days": requested_days, "bars_count": bars_count,
                     "interval": [start_date, end_date], "initial_capital": capital,
                     "metrics": metrics, "trades_count": metrics.get("trades_count")})

    fb = FactBook()
    fb.d(start_date)
    fb.d(end_date)
    for tok in v1._NUM_RE.findall(sname):
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

    block = "\n".join([
        f"标的：{code}", f"策略：{sname}（{strategy}）",
        f"实际回测区间：{start_date} ~ {end_date}（{bars_count} 个交易日）",
        f"参数：{json.dumps(params, ensure_ascii=False) if params else '策略默认参数'}",
        f"初始资金：{ic} 元", "费用：佣金 0.03%、卖出印花税 0.10%",
        "成交假设：信号基于上一根已收盘 K 线，本根开盘成交",
        "回测结果：",
        f"  区间总收益：{tr}%", f"  年化收益：{ar}%", f"  Sharpe：{sh}",
        f"  最大回撤：{md}%", f"  胜率：{wr}%（{tc} 笔成交）",
        f"  期末权益：{fe} 元", "数据源：baostock 前复权日线",
    ])
    head = (f"我用回测引擎在 {code} 上跑了「{sname}」，实际区间 {start_date} 至 {end_date}，"
            f"共 {bars_count} 个交易日，初始资金 {ic} 元。")
    if subtopic == "metrics":
        body = [head,
                f"区间总收益 {tr}%，年化 {ar}%，Sharpe {sh}。",
                "结果按固定参数、次日开盘成交计算，未计滑点与停牌。" + SHORT_DISCLAIMER]
    elif subtopic == "drawdown":
        body = [head,
                f"最大回撤 {md}%，Sharpe {sh}，区间总收益 {tr}%。回撤衡量的是权益曲线从峰到谷的幅度。",
                "历史回撤不代表未来风险上限。" + SHORT_DISCLAIMER]
    else:
        body = [head,
                f"胜率 {wr}%，共 {tc} 笔成交，期末权益 {fe} 元，区间总收益 {tr}%。",
                "成交笔数来自引擎逐日撮合记录，未计滑点。" + SHORT_DISCLAIMER]

    ctx.emit(
        intent="investment_analysis", task=f"backtest_{subtopic}",
        turns=[(template.format(code=code, sname=sname, n=bars_count), block,
                "\n".join(body))],
        fb=fb, source_ids=[bid],
        params={"symbol": code, "symbols": [code], "strategy": strategy,
                "bars_count": bars_count, "interval": [start_date, end_date],
                "initial_capital": capital, "strategy_params": params},
        interval_key=f"{code}|{start_date}|{end_date}",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": strategy,
                              "initial_capital": capital}, "ok": True, "source_id": bid}],
        evidence="app/backtest_engine/runner.py::run_strategy_backtest",
        subtype=subtopic, tone=tone,
    )
    return None


# ============================ 多轮 ============================
def gen_mt_market_risk(ctx: Ctx2, code: str, snap, *, q1: str, q2: str,
                       risk_subtopic: str, tone: str) -> str | None:
    sub, q, ind = snap
    trade_date = str(q["trade_date"])
    dd = _drawdown60(sub)
    if dd is None:
        return f"mt_drawdown_missing:{code}"
    mdd, peak_d, trough_d = dd
    fb = FactBook()
    block1, v = _market_data_block(code, q, ind, fb)
    ans1 = "\n".join([
        f"{code} 在 {trade_date} 收于 {v['price']} 元，较前一交易日 {v['prev']} 元变动 {v['chg']} 元"
        f"（{v['pct']}%）；最高 {v['high']} 元、最低 {v['low']} 元，成交量 {v['vol']} 股。",
        f"MA5 为 {v['ma5']} 元、MA20 为 {v['ma20']} 元，RSI14 为 {v['rsi']}。",
        "以上是这一交易日的行情事实。" + SHORT_DISCLAIMER,
    ])
    fb.c("high_vol_threshold_pct", 5, "code_rule:agent/nodes.py")
    fb.c("mid_vol_threshold_pct", 2, "code_rule:agent/nodes.py")
    fb.c("window_20", 20, "code_constant")
    fb.c("window_60", 60, "code_constant")
    fb.d(peak_d)
    fb.d(trough_d)
    vol20 = fb.n("volatility20", ind["volatility20"], 1)
    pos60 = fb.n("pos60", ind["pos60"], 1)
    hi60 = fb.n("high60", ind["high60"], 2)
    lo60 = fb.n("low60", ind["low60"], 2)
    mdd_s = fb.n("max_drawdown_60d_pct", mdd, 2)
    ret20 = fb.n("ret20", ind["ret20"], 2, True) if ind.get("ret20") is not None else None
    level = ("高波动" if abs(q["change_percent"]) > 5
             else ("中等波动" if abs(q["change_percent"]) > 2 else "相对稳定"))
    block2 = "\n".join([
        f"标的：{code}", f"交易日：{trade_date}",
        f"20 日年化波动率：{vol20}%",
        f"近 60 个交易日最大回撤：{mdd_s}%（峰值日 {peak_d}，谷底日 {trough_d}）",
        f"60 日区间：{lo60} ~ {hi60} 元，当前分位 {pos60}%",
        f"近 20 日涨跌幅：{ret20}%" if ret20 is not None else "近 20 日涨跌幅：数据不足",
    ])
    if risk_subtopic == "volatility" and ret20 is not None:
        ans2 = (f"{code} 的 20 日年化波动率为 {vol20}%，近 20 个交易日累计涨跌幅 {ret20}%；"
                f"按分档规则（超 5% 高波动、超 2% 中等）当日属于「{level}」。"
                "波动率只描述历史离散度，不含方向判断。" + SHORT_DISCLAIMER)
    else:
        ans2 = (f"{code} 近 60 个交易日最大回撤 {mdd_s}%，峰值 {peak_d}、谷底 {trough_d}；"
                f"当前处于 60 日区间 {lo60} ~ {hi60} 元的 {pos60}% 分位。"
                "回撤与分位都是已发生的数据，不构成择时建议。" + SHORT_DISCLAIMER)
    sid_q = f"quote:{code}:{trade_date}"
    sid_r = f"risk:{code}:{trade_date}"
    ctx.source(sid_q, {"type": "quote", "source_id": sid_q, "symbol": code,
                       "trade_date": trade_date, "data_source": "baostock 日线快照",
                       "payload": {"current_price": q["current_price"]}})
    ctx.source(sid_r, {"type": "risk_snapshot", "source_id": sid_r, "symbol": code,
                       "trade_date": trade_date,
                       "indicators": {k: ind.get(k) for k in
                                      ("volatility20", "pos60", "high60", "low60", "ret20")},
                       "max_drawdown_60d_pct": round(mdd, 4)})
    ctx.emit(
        intent="market_query", task="mt_quote_then_risk",
        turns=[(q1.format(code=code), block1, ans1), (q2, block2, ans2)],
        fb=fb, source_ids=[sid_q, sid_r],
        params={"symbol": code, "symbols": [code], "trade_date": trade_date},
        interval_key=f"{code}|{trade_date}|mt_market_risk",
        tool_calls=[
            {"step": 1, "tool": "get_historical", "args": {"symbol": code, "days": 340},
             "ok": True, "source_id": sid_q},
            {"step": 2, "tool": "get_historical", "args": {"symbol": code, "days": 60},
             "ok": True, "source_id": sid_r},
        ],
        evidence="两轮共用同一交易日日线：行情快照 + 波动/回撤指标",
        subtype="mt_quote_then_risk", tone=tone, extra={"mt_type": "market_risk"},
    )
    return None


def gen_mt_risk_position(ctx: Ctx2, code: str, snap, *, q1: str, q2: str,
                         tone: str) -> str | None:
    sub, q, ind = snap
    trade_date = str(q["trade_date"])
    dd = _drawdown60(sub)
    if dd is None:
        return f"mt_risk_drawdown_missing:{code}"
    mdd, peak_d, trough_d = dd
    fb = FactBook()
    fb.d(trade_date)
    fb.d(peak_d)
    fb.d(trough_d)
    fb.c("window_20", 20, "code_constant")
    fb.c("window_60", 60, "code_constant")
    vol20 = fb.n("volatility20", ind["volatility20"], 1)
    mdd_s = fb.n("max_drawdown_60d_pct", mdd, 2)
    pos60 = fb.n("pos60", ind["pos60"], 1)
    hi60 = fb.n("high60", ind["high60"], 2)
    lo60 = fb.n("low60", ind["low60"], 2)
    price = fb.n("current_price", q["current_price"], 2)
    block1 = "\n".join([
        f"标的：{code}", f"交易日：{trade_date}",
        f"20 日年化波动率：{vol20}%",
        f"近 60 个交易日最大回撤：{mdd_s}%（峰值日 {peak_d}，谷底日 {trough_d}）",
    ])
    ans1 = (f"{code} 的 20 日年化波动率 {vol20}%，近 60 个交易日最大回撤 {mdd_s}%，"
            f"峰值 {peak_d}、谷底 {trough_d}。这两项只描述历史波动与回撤幅度。" + SHORT_DISCLAIMER)
    block2 = "\n".join([
        f"标的：{code}", f"交易日：{trade_date}",
        f"60 日区间：{lo60} ~ {hi60} 元",
        f"当前分位：{pos60}%", f"最新价：{price} 元",
    ])
    ans2 = (f"以 {trade_date} 为终点，{code} 的 60 日区间是 {lo60} ~ {hi60} 元，"
            f"最新价 {price} 元对应 {pos60}% 分位；区间上下沿与分位都按收盘价计算。"
            "位置高低不等于后续方向。" + SHORT_DISCLAIMER)
    sid_r = f"risk:{code}:{trade_date}"
    sid_p = f"position:{code}:{trade_date}"
    ctx.source(sid_r, {"type": "risk_snapshot", "source_id": sid_r, "symbol": code,
                       "trade_date": trade_date,
                       "indicators": {k: ind.get(k) for k in
                                      ("volatility20", "high60", "low60", "pos60")},
                       "max_drawdown_60d_pct": round(mdd, 4)})
    ctx.source(sid_p, {"type": "risk_snapshot", "source_id": sid_p, "symbol": code,
                       "trade_date": trade_date,
                       "indicators": {"pos60": ind["pos60"], "high60": ind["high60"],
                                      "low60": ind["low60"]}})
    ctx.emit(
        intent="risk_assessment", task="mt_risk_then_position",
        turns=[(q1.format(code=code), block1, ans1), (q2, block2, ans2)],
        fb=fb, source_ids=[sid_r, sid_p],
        params={"symbol": code, "symbols": [code], "trade_date": trade_date},
        interval_key=f"{code}|{trade_date}|mt_risk_position",
        tool_calls=[
            {"step": 1, "tool": "get_historical", "args": {"symbol": code, "days": 60},
             "ok": True, "source_id": sid_r},
            {"step": 2, "tool": "get_historical", "args": {"symbol": code, "days": 60},
             "ok": True, "source_id": sid_p},
        ],
        evidence="两轮共用近 60 根日线：波动/回撤 + 区间分位",
        subtype="mt_risk_then_position", tone=tone,
        extra={"mt_type": "risk_position"},
    )
    return None


def gen_mt_portfolio_cost(ctx: Ctx2, code: str, snap, *, q1: str, q2: str,
                          qty: int, lookback: int, tone: str) -> str | None:
    sub, q, ind = snap
    lot = _lot(sub, lookback, qty)
    if lot is None:
        return f"mt_portfolio_lot_missing:{code}"
    trade_date = str(q["trade_date"])
    price = float(q["current_price"])
    fb = FactBook()
    fb.d(trade_date)
    fb.d(lot["buy_date"])
    fb.c("qty", qty, "user_param")
    fb.c("lookback", lookback, "param")
    fb.c("ma_window_20", 20, "indicator_window")
    fb.c("ma_window_60", 60, "indicator_window")
    p = fb.n("current_price", price, 2)
    co = fb.n("cost_price", lot["cost"], 2)
    ca = fb.n("cost_amount", lot["cost_amt"], 2)
    mv = fb.n("market_value", price * qty, 2)
    pnl = fb.n("unrealized_pnl", price * qty - lot["cost_amt"], 2, True)
    pct = fb.n("unrealized_pnl_pct",
               (price * qty - lot["cost_amt"]) / lot["cost_amt"] * 100, 2, True)
    block1 = "\n".join([
        f"标的：{code}", f"买入日：{lot['buy_date']}（收盘价 {co} 元）",
        f"数量：{qty} 股", f"成本金额：{ca} 元", f"最新交易日：{trade_date}",
        f"最新价：{p} 元", f"市值：{mv} 元", f"浮动盈亏：{pnl} 元（{pct}%）",
    ])
    ans1 = (f"按 {lot['buy_date']} 收盘价 {co} 元买入 {qty} 股，成本 {ca} 元；"
            f"{trade_date} 收盘 {p} 元，市值 {mv} 元，浮动盈亏 {pnl} 元（{pct}%）。"
            "未计佣金、印花税与分红。" + SHORT_DISCLAIMER)
    ma20 = ind["ma20"]
    ma60 = ind.get("ma60")
    d20 = (lot["cost"] - ma20) / ma20 * 100 if ma20 else 0.0
    d60 = (lot["cost"] - ma60) / ma60 * 100 if ma60 else 0.0
    block2 = "\n".join([
        f"标的：{code}", f"成本价：{co} 元（{lot['buy_date']} 收盘）",
        f"MA20：{fb.n('ma20', ma20, 2)} 元",
        f"MA60：{fb.n('ma60', ma60, 2) if ma60 is not None else '不足'} 元",
        f"最新价：{p} 元",
    ])
    ans2 = (f"你的成本 {co} 元相对 MA20（{fb.n('ma20_echo', ma20, 2)} 元）"
            f"{'高' if d20 >= 0 else '低'} {fb.n('cost_vs_ma20', abs(d20), 2)}%，"
            f"相对 MA60（{fb.n('ma60_echo', ma60, 2) if ma60 is not None else '不足'} 元）"
            f"{'高' if d60 >= 0 else '低'} {fb.n('cost_vs_ma60', abs(d60), 2)}%；"
            f"最新价 {p} 元相对成本{'高' if price >= lot['cost'] else '低'} "
            f"{fb.n('price_vs_cost', abs((price / lot['cost'] - 1) * 100), 2)}%。"
            "均线是历史收盘均值，只作参照。" + SHORT_DISCLAIMER)
    sid_q = f"quote:{code}:{trade_date}"
    sid_h = f"hist:{code}:{lot['buy_date']}"
    ctx.source(sid_q, {"type": "quote", "source_id": sid_q, "symbol": code,
                       "trade_date": trade_date, "data_source": "baostock 日线快照",
                       "payload": {"current_price": price}})
    ctx.source(sid_h, {"type": "hist_bar", "source_id": sid_h, "symbol": code,
                       "trade_date": lot["buy_date"], "close": lot["cost"],
                       "data_source": "baostock"})
    ctx.emit(
        intent="portfolio_query", task="mt_holding_then_cost",
        turns=[(q1.format(code=code, qty=qty, buy_date=lot["buy_date"]), block1, ans1),
               (q2, block2, ans2)],
        fb=fb, source_ids=[sid_q, sid_h],
        params={"symbol": code, "symbols": [code], "qty": qty,
                "buy_dates": [lot["buy_date"]], "current_date": trade_date},
        interval_key=f"{code}|{lot['buy_date']}|{trade_date}|mt_portfolio_cost",
        tool_calls=[
            {"step": 1, "tool": "get_historical", "args": {"symbol": code, "days": 340},
             "ok": True, "source_id": sid_h},
            {"step": 2, "tool": "get_quote", "args": {"symbol": code},
             "ok": True, "source_id": sid_q},
        ],
        evidence="两轮共用同一买入日收盘价与最新快照：盈亏 + 成本对均线",
        subtype="mt_holding_then_cost", tone=tone,
        extra={"mt_type": "portfolio_cost"},
    )
    return None


def gen_mt_pair(ctx: Ctx2, code_a: str, snap_a, code_b: str, snap_b, *,
                q1: str, q1_tone: str, q2: str, qty: int,
                lookback_a: int, lookback_b: int) -> str | None:
    sub_a, qa, ind_a = snap_a
    sub_b, qb, ind_b = snap_b
    lot_a = _lot(sub_a, lookback_a, qty)
    lot_b = _lot(sub_b, lookback_b, qty)
    if lot_a is None or lot_b is None:
        return "mt_pair_lot_missing"
    fb = FactBook()
    fb.c("qty", qty, "user_param")
    fb.c("lookback_a", lookback_a, "param")
    fb.c("lookback_b", lookback_b, "param")
    for lot, qq in ((lot_a, qa), (lot_b, qb)):
        fb.d(lot["buy_date"])
        fb.d(str(qq["trade_date"]))
    pa = fb.n("price_a", qa["current_price"], 2)
    pb = fb.n("price_b", qb["current_price"], 2)
    ca = fb.n("cost_a", lot_a["cost"], 2)
    cb = fb.n("cost_b", lot_b["cost"], 2)
    mva = fb.n("mv_a", qa["current_price"] * qty, 2)
    mvb = fb.n("mv_b", qb["current_price"] * qty, 2)
    pnla = fb.n("pnl_a", qa["current_price"] * qty - lot_a["cost_amt"], 2, True)
    pnlb = fb.n("pnl_b", qb["current_price"] * qty - lot_b["cost_amt"], 2, True)
    total_cost = lot_a["cost_amt"] + lot_b["cost_amt"]
    total_mv = qa["current_price"] * qty + qb["current_price"] * qty
    fb.n("cost_total", total_cost, 2)
    fb.n("mv_total", total_mv, 2)
    total_pnl = total_mv - total_cost
    tpn = fb.n("pnl_total", total_pnl, 2, True)
    tpc = fb.n("pnl_total_pct", total_pnl / total_cost * 100, 2, True)
    block1 = "\n".join([
        f"标的 A：{code_a}", f"A 买入日：{lot_a['buy_date']}（收盘 {ca} 元）",
        f"A 最新价：{pa} 元；市值 {mva} 元；浮动盈亏 {pnla} 元",
        f"标的 B：{code_b}", f"B 买入日：{lot_b['buy_date']}（收盘 {cb} 元）",
        f"B 最新价：{pb} 元；市值 {mvb} 元；浮动盈亏 {pnlb} 元",
        f"两笔数量各 {qty} 股", "数据源：baostock 前复权日线",
    ])
    ans1 = (f"两笔持仓各 {qty} 股：{code_a} 成本 {ca} 元、市值 {mva} 元、"
            f"浮动盈亏 {pnla} 元；{code_b} 成本 {cb} 元、市值 {mvb} 元、浮动盈亏 {pnlb} 元。"
            f"合计市值 {fb.n('mv_total_echo', total_mv, 2)} 元，浮动盈亏 {tpn} 元（{tpc}%）。"
            "未计佣金、印花税与分红。" + SHORT_DISCLAIMER)
    block2 = "\n".join([
        f"标的：{code_a}", f"买入日：{lot_a['buy_date']}（收盘 {ca} 元）",
        f"数量：{qty} 股", f"最新价：{pa} 元", f"市值：{mva} 元",
        f"浮动盈亏：{pnla} 元",
    ])
    ans2 = (f"{code_a} 这一笔：{lot_a['buy_date']} 收盘 {ca} 元买入 {qty} 股，"
            f"最新价 {pa} 元，市值 {mva} 元，浮动盈亏 {pnla} 元，"
            f"收益率 {fb.n('pct_a', (qa['current_price'] / lot_a['cost'] - 1) * 100, 2, True)}%。"
            "只算价格变动。" + SHORT_DISCLAIMER)
    sid_a = f"quote:{code_a}:{qa['trade_date']}"
    sid_b = f"quote:{code_b}:{qb['trade_date']}"
    sid_ha = f"hist:{code_a}:{lot_a['buy_date']}"
    sid_hb = f"hist:{code_b}:{lot_b['buy_date']}"
    for sid, code, qq in ((sid_a, code_a, qa), (sid_b, code_b, qb)):
        ctx.source(sid, {"type": "quote", "source_id": sid, "symbol": code,
                         "trade_date": str(qq["trade_date"]),
                         "data_source": "baostock 日线快照",
                         "payload": {"current_price": qq["current_price"]}})
    for sid, code, lot in ((sid_ha, code_a, lot_a), (sid_hb, code_b, lot_b)):
        ctx.source(sid, {"type": "hist_bar", "source_id": sid, "symbol": code,
                         "trade_date": lot["buy_date"], "close": lot["cost"],
                         "data_source": "baostock"})
    ctx.emit(
        intent="portfolio_query", task="mt_pair_then_one_leg",
        turns=[(q1.format(a=code_a, b=code_b, d1=lot_a["buy_date"],
                          d2=lot_b["buy_date"], q=qty), block1, ans1),
               (q2.format(a=code_a), block2, ans2)],
        fb=fb, source_ids=[sid_a, sid_b, sid_ha, sid_hb],
        params={"symbol": code_a, "symbols": [code_a, code_b], "qty": qty,
                "buy_dates": [lot_a["buy_date"], lot_b["buy_date"]]},
        interval_key=f"{code_a}+{code_b}|{lot_a['buy_date']}|{lot_b['buy_date']}|mt_pair",
        tool_calls=[
            {"step": 1, "tool": "get_historical", "args": {"symbol": f"{code_a},{code_b}",
                                                           "days": 340},
             "ok": True, "source_id": sid_ha},
            {"step": 2, "tool": "get_quote", "args": {"symbol": f"{code_a},{code_b}"},
             "ok": True, "source_id": sid_a},
        ],
        evidence="两轮：先合并两笔真实持仓，再按上下文拆出单只",
        subtype="mt_pair_then_one_leg", tone=q1_tone,
        extra={"mt_type": "pair_portfolio"},
    )
    return None


# ============================ 拒答 ============================
def _register_literals(fb: FactBook, text: str) -> None:
    """把拒答文案里的字面数字（阈值、分钟数等常量）登记，避免闸门误杀。"""
    for tok in v1._NUM_RE.findall(text):
        try:
            fb.c(f"literal_{tok}", float(tok.replace(",", "")), "code_constant")
        except ValueError:
            continue


def _refusal_v2(ctx: Ctx2, *, question: str, why: str, missing: str, offer: str,
                next_action: str, source_ids: list[str], params: dict,
                interval_key: str, evidence: str, subtype: str, tone: str,
                fb: FactBook | None = None, extra: dict | None = None) -> None:
    fb = fb or FactBook()
    answer = (
        f"这个请求我无法完成：{why}。\n"
        f"缺的是：{missing}。\n"
        f"我能提供的是：{offer}。\n"
        f"如果你愿意，我可以现在就{next_action}。\n"
        f"{SHORT_DISCLAIMER}"
    )
    _register_literals(fb, answer)
    ctx.emit(
        intent="refusal", task=f"refusal_{subtype}",
        turns=[(question, "", answer)],
        fb=fb, source_ids=source_ids, params=params,
        interval_key=interval_key,
        tool_calls=[{"step": 1, "tool": "probe", "ok": False,
                     "args": params, "error": why}],
        evidence=evidence, subtype=subtype, tone=tone, extra=extra,
    )


async def gen_refusals_v2(ctx: Ctx2, bars_by_symbol: dict, ooc_symbols: list[str],
                          pred_symbols: list[str]) -> list[str]:
    skipped: list[str] = []

    # --- 数据不足：真实拿不存在的代码去取数，失败才写 ---
    for bad_code in ("999999", "123456", "999998"):
        try:
            await run_strategy_backtest(bad_code, "ma_cross", days=120)
            skipped.append(f"bad_symbol_not_failed:{bad_code}")
        except Exception as e:
            _refusal_v2(
                ctx,
                question=f"帮我回测 {bad_code} 最近 120 个交易日",
                why=f"回测引擎取不到 {bad_code} 的历史日线（{type(e).__name__}: {str(e)[:80]}）",
                missing=f"{bad_code} 在 A 股日线数据源里的行情记录",
                offer=f"用真实存在的六位 A 股代码（如 600519）做行情快照与单标的日线回测，"
                      f"{bad_code} 这类代码无法给出任何数字",
                next_action="换一个有真实日线的标的重跑",
                source_ids=[], params={"symbol": bad_code, "strategy": "ma_cross", "days": 120},
                interval_key=f"NEG|data_insufficient|{bad_code}",
                evidence="app/agent/backtest.py::fetch_history_bars 取数失败分支",
                subtype="data_insufficient", tone="brief",
            )

    # --- 能力边界：美股 / 多标的策略，都是真实调用失败 ---
    try:
        await run_strategy_backtest("AAPL", "ma_cross", days=120)
        skipped.append("us_backtest_not_failed:AAPL")
    except Exception as e:
        _refusal_v2(
            ctx,
            question="用均线策略回测一下 AAPL 最近 120 个交易日",
            why=f"回测引擎只接受 A 股代码，无法识别 AAPL（{type(e).__name__}: {str(e)[:60]}）",
            missing="美股日线在 A 股回测引擎中的支持",
            offer="A 股标的的日线回测；美股只能查行情快照，不能进这套撮合引擎",
            next_action="用 A 股代码跑回测，或只查 AAPL 的行情",
            source_ids=[], params={"symbol": "AAPL", "strategy": "ma_cross"},
            interval_key="NEG|out_of_capability|AAPL",
            evidence="app/agent/tools.py::normalize_a_share 仅识别 A 股；backtest_engine 依赖该函数",
            subtype="out_of_capability", tone="formal",
        )

    lot_code = ooc_symbols[0] if ooc_symbols else "600519"
    try:
        await run_strategy_backtest(lot_code, "cross_section_momentum", days=120)
        skipped.append("multi_asset_not_failed")
    except Exception as e:
        _refusal_v2(
            ctx,
            question=f"用截面动量策略回测一下 {lot_code}",
            why=f"该策略要求多标的截面数据，当前引擎是单标的模式（{str(e)[:50]}）",
            missing="同一交易日多只标的的截面行情与统一调仓账本",
            offer=f"单标的的趋势、震荡、突破类策略回测（如 {lot_code} 的均线/MACD/唐奇安）",
            next_action="换一个单标的策略，或一次只给一个标的",
            source_ids=[], params={"symbol": lot_code, "strategy": "cross_section_momentum"},
            interval_key=f"NEG|out_of_capability|multi_asset|{lot_code}",
            evidence="app/backtest_engine/runner.py 对 requires_multi_asset 抛 ValueError",
            subtype="out_of_capability", tone="formal",
        )

    # --- 能力边界：8 类数据/能力缺口，按标的轮换问法 ---
    for kind_i, spec in enumerate(OOC_SPECS):
        for t_i, sym in enumerate(ooc_symbols):
            code = sym
            q = spec["q"][(kind_i + t_i) % len(spec["q"])].format(code=code)
            _refusal_v2(
                ctx, question=q, why=spec["why"], missing=spec["missing"],
                offer=f"对 {code}：{spec['offer']}",
                next_action=spec["next"], source_ids=[],
                params={"symbol": sym, **spec["params"]},
                interval_key=f"NEG|out_of_capability|{spec['key']}|{code}",
                evidence=spec["evidence"], subtype="out_of_capability", tone="colloquial",
            )

    # --- 预测 / 目标价：拒答，但给出该标的真实的历史统计 ---
    for t_i, sym in enumerate(pred_symbols):
        code = sym
        snap = bars_by_symbol.get(sym)
        if not snap:
            skipped.append(f"prediction_no_bars:{sym}")
            continue
        _, q, ind = snap
        if ind.get("ret20") is None or ind.get("volatility20") is None:
            skipped.append(f"prediction_no_ind:{sym}")
            continue
        for p_i in (t_i % 3, (t_i % 3) + 3):
            question_t, request = PREDICTION_SPECS[p_i]
            why = {
                "direction": "系统不做下一个交易日或下周的涨跌方向判断，没有可验证的预测模型",
                "target_price": "系统不给目标价、止损位或点位区间，这类数字需要估值模型与样本外检验，当前没有",
                "advice": "系统不给买 / 卖 / 加减仓的操作建议，只提供可核对的行情与回测事实",
            }[request]
            fb = FactBook()
            fb.c("window_20", 20, "code_constant")
            ret20 = fb.n("ret20_pct", ind["ret20"], 2, True)
            vol20 = fb.n("volatility20_pct", ind["volatility20"], 1)
            stat = f"近 20 个交易日累计涨跌幅 {ret20}%、20 日年化波动率 {vol20}%"
            _refusal_v2(
                ctx, question=question_t.format(code=code),
                why=why,
                missing="可验证的预测模型与对应的样本外评估结果（当前没有）",
                offer=f"{code} 的历史统计：{stat}，以及日线行情与策略回测结果",
                next_action="用行情、指标和回测帮你评估当前价格位置与波动风险",
                source_ids=[], params={"symbol": sym, "request": request},
                interval_key=f"NEG|prediction_refusal|{request}|{code}|{p_i}",
                evidence="项目内无预测模型；agent/nodes.py 明确禁止承诺收益与精确目标价",
                subtype="prediction_refusal", tone="colloquial", fb=fb,
                extra={"observed_stat": {"ret20_pct": ind["ret20"],
                                         "volatility20_pct": ind["volatility20"]}},
            )
    return skipped


# ============================ 澄清 ============================
def gen_clarifications(ctx: Ctx2) -> None:
    """问题本身有歧义：先反问，不取数、不给数字。"""
    for s_i, style in enumerate(CLARIFY_STYLES):
        for q_i, question in enumerate(style["q"]):
            fb = FactBook()
            answer = (
                f"{style['a']}\n"
                f"在你把「{question}」里的指代补充清楚之前，我不会给出任何数字，"
                f"避免用错标的或用错口径。{SHORT_DISCLAIMER}"
            )
            ctx.emit(
                intent=CLARIFICATION_INTENT, task="clarify",
                turns=[(question, "", answer)], fb=fb, source_ids=[],
                params={"symbol": "UNKNOWN"},
                interval_key=f"CLARIFY|{style['name']}|{q_i}",
                tool_calls=[{"step": 1, "tool": "clarify", "ok": True,
                             "args": {"reason": style["name"]}}],
                evidence="在线 node_analysis 的澄清分支：缺标的/缺维度/缺成本时先反问",
                subtype=style["name"], tone="formal",
                extra={"ambiguous_reason": style["name"],
                       "clarify_options": ["整体组合", "单只标的"] if s_i == 0 else []},
            )


# ============================ 质量闸门 ============================
def run_gate_v2(ctx: Ctx2, codes: set[str], min_len: int = 60, max_len: int = 1500,
                max_total_chars: int = 4000) -> tuple[list[dict], list[dict]]:
    """与 v1 同一套规则；差异只有两点，且都不放宽数字校验：
    1. 支持 system / user / assistant 交替的多轮结构（v1 只认单轮）；
    2. clarification 意图本来就「不取数」，允许 source_ids 为空。
    """
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
            answers = [c for r, c in zip(roles, contents) if r == "assistant"]
            rejects.append({"sample_id": sid, "intent": intent, "reason": reason,
                            "detail": detail[:200],
                            "answer_excerpt": (answers[-1][:160] if answers else "")})

        ok_schema = (len(msgs) >= 3 and roles[0] == "system" and roles[-1] == "assistant"
                     and len(roles) % 2 == 1
                     and all(r == "user" for r in roles[1::2])
                     and all(r == "assistant" for r in roles[2::2])
                     and all(contents))
        if not ok_schema:
            _rej("schema", f"roles={roles}")
            continue
        answer = "\n".join(c for r, c in zip(roles, contents) if r == "assistant")
        question = "\n".join(c for r, c in zip(roles, contents) if r == "user")
        total_chars = sum(len(c) for c in contents)
        if total_chars > max_total_chars:
            _rej("length_total", f"{total_chars} chars > {max_total_chars}")
            continue
        if not (min_len <= len(answer) <= max_len):
            _rej("length_answer", f"{len(answer)} chars")
            continue

        is_refusal_text = any(m in answer for m in REFUSAL_MARKERS)
        if intent == "refusal":
            if not is_refusal_text or "缺的是" not in answer or "能提供的是" not in answer:
                _rej("refusal_as_normal", "拒答样本缺少「缺什么/能提供什么」")
                continue
        elif intent == CLARIFICATION_INTENT:
            if is_refusal_text:
                _rej("refusal_as_normal", "澄清样本里出现拒答话术")
                continue
            if "你" not in answer or "？" not in answer:
                _rej("clarify_shape", "澄清样本没有反问")
                continue
        elif is_refusal_text:
            _rej("refusal_as_normal", "正常意图样本里出现拒答话术")
            continue

        prov = s.get("provenance") or {}
        source_ids = prov.get("source_ids") or []
        if intent not in ("refusal", CLARIFICATION_INTENT):
            if not source_ids:
                _rej("provenance_missing", "正常样本没有 source_id")
                continue
            if any(x not in ctx.sources for x in source_ids):
                _rej("provenance_broken", f"source 未落盘: {source_ids}")
                continue

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
        bad = v1.check_numbers(answer, fb, codes)
        if bad:
            _rej("unregistered_number", "、".join(sorted(set(bad))[:8]))
            continue

        # 多轮要按各轮问句分别去重，否则两条只是轮次顺序不同的样本会被漏掉
        qk = "\n".join(v1._norm_q(c) for r, c in zip(roles, contents) if r == "user")
        ak = v1._norm_a(answer)
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
def assign_symbol_splits(symbols: list[str], seed: int) -> dict[str, str]:
    order = list(symbols)
    random.Random(seed).shuffle(order)
    n = len(order)
    n_train = int(round(n * 0.70))
    n_val = int(round(n * 0.15))
    out: dict[str, str] = {}
    for i, sym in enumerate(order):
        if i < n_train:
            out[sym] = "train"
        elif i < n_train + n_val:
            out[sym] = "val"
        else:
            out[sym] = "test"
    return out


def build_pairs(symbols: list[str], symbol_split: dict[str, str]) -> list[tuple[str, str]]:
    """只在同一 split 内两两配对，保证配对样本不会把标的泄漏到别的集合。"""
    by_split: dict[str, list[str]] = defaultdict(list)
    for sym in symbols:
        by_split[symbol_split[sym]].append(sym)
    pairs: list[tuple[str, str]] = []
    for split in ("train", "val", "test"):
        group = by_split.get(split, [])
        for i in range(0, len(group) - 1, 2):
            pairs.append((group[i], group[i + 1]))
    return pairs


def _bare_symbol(sym: str) -> str:
    return str(sym).split(".")[-1]


def group_key(sample: dict) -> str:
    params = sample["provenance"].get("params") or {}
    syms = params.get("symbols") or ([params["symbol"]] if params.get("symbol") else ["UNKNOWN"])
    return "|".join(sorted(_bare_symbol(s) for s in syms))


def split_samples(rows: list[dict], symbol_split: dict[str, str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    clarify_i = 0
    for s in rows:
        key = group_key(s)
        first = key.split("|")[0]
        bucket = symbol_split.get(first)
        if bucket is None:
            if s["intent"] == CLARIFICATION_INTENT:
                bucket = ("train", "train", "train", "train", "train", "train",
                          "train", "val", "val", "test")[clarify_i % 10]
                clarify_i += 1
            else:
                bucket = "test"
        out[bucket].append(s)
    return out


def check_leakage_v2(splits: dict[str, list[dict]]) -> dict:
    symbol_map: dict[str, set[str]] = defaultdict(set)
    interval_map: dict[str, set[str]] = defaultdict(set)
    for name, rows in splits.items():
        for s in rows:
            prov = s["provenance"]
            params = prov.get("params") or {}
            syms = params.get("symbols") or [params.get("symbol") or "UNKNOWN"]
            for sym in syms:
                bare = _bare_symbol(sym)
                if bare == "UNKNOWN":  # 澄清样本不绑定标的，不参与泄漏检查
                    continue
                symbol_map[bare].add(name)
            interval_map[str(prov.get("interval_key"))].add(name)
    return {
        "symbols_in_multiple_splits": sorted(k for k, v in symbol_map.items() if len(v) > 1),
        "intervals_in_multiple_splits": sorted(k for k, v in interval_map.items() if len(v) > 1),
        "symbols_checked": len(symbol_map),
        "intervals_checked": len(interval_map),
    }


# ============================ 主流程 ============================
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="衡策 SFT 合成 v2（真实行情/回测 + 多口径问法）")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--strategies", default=DEFAULT_STRATEGIES)
    ap.add_argument("--windows", default="90,150,250")
    ap.add_argument("--capital", type=float, default=200_000.0)
    ap.add_argument("--qty", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--out", default=str(BACKEND_ROOT / "data" / "sft_synth_v2"))
    ap.add_argument("--no-refusals", action="store_true")
    ap.add_argument("--no-clarify", action="store_true")
    ap.add_argument("--no-report", action="store_true", help="跳过 DATA_CARD 生成")
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
    ctx = Ctx2()
    skip_log: list[str] = []
    codes: set[str] = set()

    bars_by_symbol: dict[str, list] = {}
    snaps_by_symbol: dict[str, dict[int, tuple]] = {}
    had_fetch = False
    for sym in symbols:
        norm = normalize_a_share(sym)
        code = norm or sym
        codes.add(code)
        codes.update(part for part in code.split(".") if part.isdigit())
        try:
            hist = await get_historical(sym, 340)
        except Exception as e:
            skip_log.append(f"{sym}:fetch_failed:{type(e).__name__}")
            logger.warning(f"[{sym}] 取数异常：{e}")
            continue
        bars = hist.get("data") or []
        if len(bars) < 120:
            skip_log.append(f"{sym}:bars_too_few:{len(bars)}")
            logger.warning(f"[{sym}] 日线不足 120 根，跳过")
            continue
        had_fetch = True
        bars_by_symbol[sym] = bars
        snaps: dict[int, tuple] = {}
        for off in SNAPSHOT_OFFSETS:
            snap = _snapshot(bars, off)
            if snap is None:
                skip_log.append(f"{sym}:snapshot_missing:{off}")
            else:
                snaps[off] = snap
        snaps_by_symbol[sym] = snaps

    usable = [s for s in symbols if s in bars_by_symbol]
    if not usable:
        logger.error("没有可用标的，退出")
        return 1

    # ---- 看盘：每标的 6 条，4 条单轮（3 个子主题轮换）+ 2 条多轮 ----
    for i, sym in enumerate(usable):
        code = sym
        codes.add(normalize_a_share(sym) or sym)
        snaps = snaps_by_symbol.get(sym) or {}
        for k in range(6):
            off = SNAPSHOT_OFFSETS[k % len(SNAPSHOT_OFFSETS)]
            snap = snaps.get(off)
            if snap is None:
                skip_log.append(f"{sym}:market_no_snapshot:{off}")
                continue
            tpl, tone = MARKET_TEMPLATES[(i * 6 + k) % len(MARKET_TEMPLATES)]
            if k >= 4:
                q2 = MT_MARKET_RISK_Q[(i + k) % len(MT_MARKET_RISK_Q)]
                err = gen_mt_market_risk(ctx, code, snap, q1=tpl, q2=q2,
                                         risk_subtopic=("volatility" if k == 4 else "drawdown"),
                                         tone=tone)
            else:
                err = gen_market_v2(ctx, code, snap, template=tpl, tone=tone,
                                    subtopic=MARKET_SUBTOPICS[(i + k // 3) % 3], offset=off)
            if err:
                skip_log.append(err)

    # ---- 风险：每标的 5 条，3 条单轮 + 2 条多轮 ----
    for i, sym in enumerate(usable):
        code = sym
        codes.add(normalize_a_share(sym) or sym)
        snaps = snaps_by_symbol.get(sym) or {}
        for k in range(5):
            off = SNAPSHOT_OFFSETS[k % 2]
            snap = snaps.get(off)
            if snap is None:
                skip_log.append(f"{sym}:risk_no_snapshot:{off}")
                continue
            tpl, tone = RISK_TEMPLATES[(i * 5 + k) % len(RISK_TEMPLATES)]
            if k >= 3:
                q2 = MT_RISK_POSITION_Q[(i + k) % len(MT_RISK_POSITION_Q)]
                err = gen_mt_risk_position(ctx, code, snap, q1=tpl, q2=q2, tone=tone)
            else:
                err = gen_risk_v2(ctx, code, snap, template=tpl, tone=tone,
                                  subtopic=RISK_SUBTOPICS[(i + k) % 3], offset=off)
            if err:
                skip_log.append(err)

    # ---- 持仓：每标的 3 条单轮 + 1 条多轮 ----
    for i, sym in enumerate(usable):
        code = sym
        codes.add(normalize_a_share(sym) or sym)
        snap = (snaps_by_symbol.get(sym) or {}).get(0)
        if snap is None:
            skip_log.append(f"{sym}:portfolio_no_snapshot")
            continue
        plans = [
            ("pnl", (COST_LOOKBACKS[0],), (args.qty,), (i * 4) % 12),
            ("share", (COST_LOOKBACKS[0], COST_LOOKBACKS[2]),
             (args.qty * 2, args.qty), (i * 4 + 1) % 12),
            ("cost", (COST_LOOKBACKS[1],), (args.qty,), (i * 4 + 2) % 12),
        ]
        for subtopic, lookbacks, qtys, tpl_i in plans:
            tpl, tone = PORTFOLIO_TEMPLATES[tpl_i]
            err = gen_portfolio_v2(ctx, code, snap, template=tpl, tone=tone,
                                   subtopic=subtopic, qtys=qtys, lookbacks=lookbacks)
            if err:
                skip_log.append(err)
        q1_tpl, q1_tone = PORTFOLIO_TEMPLATES[(i * 4 + 3) % 12]
        q2 = MT_PORTFOLIO_COST_Q[i % len(MT_PORTFOLIO_COST_Q)]
        err = gen_mt_portfolio_cost(ctx, code, snap, q1=q1_tpl, q2=q2, qty=args.qty,
                                    lookback=COST_LOOKBACKS[2], tone=q1_tone)
        if err:
            skip_log.append(err)

    # ---- 双标的持仓：只在同一 split 内配对 ----
    symbol_split = assign_symbol_splits(usable, args.seed)
    pairs = build_pairs(usable, symbol_split)
    for p_i, (sym_a, sym_b) in enumerate(pairs):
        code_a = sym_a
        code_b = sym_b
        snap_a = (snaps_by_symbol.get(sym_a) or {}).get(0)
        snap_b = (snaps_by_symbol.get(sym_b) or {}).get(0)
        if snap_a is None or snap_b is None:
            skip_log.append(f"pair_no_snapshot:{sym_a}:{sym_b}")
            continue
        q1, q1_tone = MT_PAIR_OPEN_Q[p_i % len(MT_PAIR_OPEN_Q)]
        q2 = MT_PAIR_FOLLOW_Q[p_i % len(MT_PAIR_FOLLOW_Q)]
        err = gen_mt_pair(ctx, code_a, snap_a, code_b, snap_b, q1=q1, q1_tone=q1_tone,
                          q2=q2, qty=args.qty, lookback_a=COST_LOOKBACKS[0],
                          lookback_b=COST_LOOKBACKS[1])
        if err:
            skip_log.append(err)

    # ---- 回测：每标的 × 策略 × 窗口 1 条 ----
    bt_count = 0
    for i, sym in enumerate(usable):
        code = sym
        codes.add(normalize_a_share(sym) or sym)
        for st_i, st in enumerate(strategies):
            for w_i, days in enumerate(windows):
                try:
                    result = await run_strategy_backtest(
                        sym, st, initial_capital=args.capital, days=days)
                except Exception as e:
                    skip_log.append(f"{sym}:{st}:{days}:{type(e).__name__}")
                    logger.warning(f"[{sym}] {st} {days}d 回测失败：{e}")
                    continue
                tpl_i = (i * 4 + st_i) % len(BACKTEST_TEMPLATES)
                tpl, tone = BACKTEST_TEMPLATES[tpl_i]
                err = gen_backtest_v2(ctx, code, st, days, args.capital, result,
                                      template=tpl, tone=tone,
                                      subtopic=BACKTEST_SUBTOPICS[(i + w_i) % 3])
                if err:
                    skip_log.append(err)
                else:
                    bt_count += 1
    logger.info(f"回测样本 {bt_count} 条")

    if not args.no_refusals:
        ooc_symbols = usable[::4][:10]
        pred_symbols = usable[1::3][:13]
        skip_log.extend(await gen_refusals_v2(
            ctx, {s: (snaps_by_symbol.get(s) or {}).get(0) for s in usable},
            ooc_symbols, pred_symbols))
    if not args.no_clarify:
        gen_clarifications(ctx)

    codes.update({"sh.999999", "999999", "sh.123456", "123456", "sh.999998", "999998"})
    kept, rejects = run_gate_v2(ctx, codes)
    splits = split_samples(kept, symbol_split)
    leakage = check_leakage_v2(splits)

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

    intent_counts = Counter(s["intent"] for s in all_rows)
    tone_counts = Counter(s.get("tone") or "n/a" for s in all_rows)
    subtype_counts = Counter(f"{s['intent']}/{s.get('subtype') or 'n/a'}"
                             for s in all_rows)
    refusal_sub = Counter(s.get("subtype") or "unlabeled"
                          for s in all_rows if s["intent"] == "refusal")
    source_types = Counter()
    for s in all_rows:
        for sid in s["provenance"]["source_ids"]:
            source_types[ctx.sources.get(sid, {}).get("type", "unknown")] += 1
    multi = [s for s in all_rows
             if sum(1 for m in s["messages"] if m["role"] == "user") >= 2]
    total = len(all_rows)

    manifest = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "symbols": symbols, "strategies": strategies, "windows": windows,
            "snapshot_offsets": list(SNAPSHOT_OFFSETS),
            "cost_lookbacks": list(COST_LOOKBACKS),
            "initial_capital": args.capital, "qty": args.qty, "seed": args.seed,
            "refusals": not args.no_refusals, "clarify": not args.no_clarify,
        },
        "counts": {
            "generated": len(ctx.samples), "kept": total,
            "train": len(splits["train"]), "val": len(splits["val"]),
            "test": len(splits["test"]), "rejected": len(rejects),
            "sources": len(ctx.sources), "skipped": len(skip_log),
            "target": 1200, "gap_to_target": max(0, 1200 - total),
        },
        "by_intent": {k: {"count": v, "pct": round(v / total * 100, 2)}
                      for k, v in intent_counts.most_common()},
        "by_tone": dict(tone_counts),
        "by_subtype": dict(subtype_counts),
        "by_refusal_subtype": dict(refusal_sub),
        "by_source_type": dict(source_types),
        "multi_turn": {"count": len(multi),
                       "pct": round(len(multi) / total * 100, 2) if total else 0.0},
        "skipped": skip_log,
        "rejected_by_reason": dict(Counter(r["reason"] for r in rejects)),
        "leakage_check": leakage,
        "split_symbols": dict(Counter(symbol_split.values())),
        "diversity_report": "DATA_CARD.md（由 report_diversity.py 实测生成）",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_report:
        diversity.write_data_card(Path(args.out), v1_dir=BACKEND_ROOT / "data" / "sft_synth")
        diversity.write_data_card(Path(args.out), v1_dir=BACKEND_ROOT / "data" / "sft_synth_v1")

    logger.info("完成：保留 {} / 生成 {}，train/val/test = {}/{}/{}",
                total, len(ctx.samples), len(splits["train"]),
                len(splits["val"]), len(splits["test"]))
    print(json.dumps({
        "out_dir": str(out_dir), "kept": total,
        "train": len(splits["train"]), "val": len(splits["val"]),
        "test": len(splits["test"]),
        "by_intent": dict(intent_counts),
        "multi_turn": manifest["multi_turn"],
        "rejected_by_reason": manifest["rejected_by_reason"],
        "leakage": {"symbols_crossing": len(leakage["symbols_in_multiple_splits"]),
                    "intervals_crossing": len(leakage["intervals_in_multiple_splits"])},
        "had_fetch": had_fetch,
    }, ensure_ascii=False, indent=2))
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
