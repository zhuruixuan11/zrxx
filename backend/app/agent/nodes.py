"""LangGraph 节点函数。

优化方案（相对于 PDF 原版的改进）：
1. classify_intent → 低置信度走 clarification 分支（新版 Router 支持）
2. 投资分析节点并行执行 portfolio + market + rag 三个子任务（LangGraph fan-out/fan-in）
3. generate_response 统一汇总所有并行节点产出，给 LLM 一次完整上下文生成回复
4. 所有节点返回 dict 而不是修改 state（LangGraph 1.2 推荐不可变风格）
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict

from loguru import logger

from ..llm.client import get_llm
from .memory import format_for_prompt, get_recent_history
from .rag import retrieve
from .indicators import compute_indicators
from .router import classify_intent_llm, _keyword_classify, pick_clarification_question
from .state import AgentState, INTENT_LIST
from .tools import (
    get_fund_flow_overview,
    get_historical,
    get_macro_overview,
    get_portfolio_summary,
    get_quote,
    normalize_a_share,
)
from .news import fetch_news, format_news_for_prompt
from .profile import get_profile_summary, get_risk_level


# ========== 提取股票代码（支持 A 股纯 6 位 / 带后缀 / 美股 ticker）========
_A_PREFIX = ("600", "601", "603", "605", "688", "000", "001", "002", "003", "300", "301", "430", "4", "8")


def _extract_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()

    def add(sym: str | None):
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)

    # A 股：600519 / 600519.SS / sh600519 都能识别
    for m in re.finditer(r"(\d{6})(?:\.(SS|SZ|SH|BJ|ss|sz|sh|bj))?", text):
        code, suffix = m.group(1), m.group(2)
        if suffix:
            add(normalize_a_share(f"{code}.{suffix.upper().replace('SS', 'SH')}"))
        elif code.startswith(_A_PREFIX):
            add(normalize_a_share(code))
    # 美股：全大写 2-5 位（排除 A 股后缀残留）
    for m in re.finditer(r"\b([A-Z]{2,5})\b", text):
        if m.group(1) not in {"SS", "SZ", "SH", "BJ", "CA", "US"}:
            add(m.group(1))
    return symbols


# ========== 节点 1：意图识别 ==========
async def node_classify(state: AgentState) -> Dict[str, Any]:
    message = state.get("message", "")
    llm = get_llm()
    intent, conf = await classify_intent_llm(message, llm)

    # 置信度 < 0.8 → 进入澄清分支
    if conf < 0.8:
        # 关键词也给一个备选
        fallback_intent, _ = _keyword_classify(message)
        if fallback_intent != intent:
            q = pick_clarification_question(intent, fallback_intent)
            return {"intent": intent, "intent_confidence": conf,
                    "needs_clarification": True, "clarification_question": q}
        return {"intent": intent, "intent_confidence": conf,
                "needs_clarification": True,
                "clarification_question": "您能再说清楚一点吗？我想更好地理解您的需求😊"}

    # 高置信度 → 直接路由
    return {"intent": intent, "intent_confidence": conf, "needs_clarification": False}


# ========== 节点 1.5：澄清处理 ==========
async def node_clarification(state: AgentState) -> Dict[str, Any]:
    """返回澄清问题文本（不调用 LLM 生成节点）。"""
    return {
        "response": state.get("clarification_question", "您能再说清楚一点吗？"),
        "response_type": "text",
    }


# ========== 节点 2a：投资组合查询 ==========
async def node_portfolio(state: AgentState) -> Dict[str, Any]:
    from ..database import async_session
    from .memory import format_for_prompt

    async with async_session() as session:
        user_id = state.get("user_id", "")
        portfolio = await get_portfolio_summary(user_id, session)

    # 格式化给 LLM 的摘要
    p = portfolio
    summary_lines = [
        f"您的投资组合【{p['name']}】：",
        f"  总资产 ¥{p['total_value']:,.2f}，可用现金 ¥{p['cash_balance']:,.2f}",
        f"  累计盈亏 ¥{p['total_pnl']:,.2f}（{p['pnl_percent']:+.2f}%）",
        f"  持仓数量：{p['holdings_count']}",
    ]
    if p["holdings"]:
        summary_lines.append("持仓明细：")
        for h in p["holdings"][:5]:
            pnl_sign = "📈" if h["unrealized_pnl"] >= 0 else "📉"
            summary_lines.append(
                f"  {h['symbol']}: {h['quantity']}股 @ ¥{h['current_price']}"
                f" {pnl_sign}¥{h['unrealized_pnl']:+,.2f}（{h['unrealized_pnl_percent']:+.1f}%）"
            )
    return {"portfolio_data": portfolio, "response": "\n".join(summary_lines), "response_type": "text"}


# ========== 节点 2b：行情查询 ==========
async def node_market(state: AgentState) -> Dict[str, Any]:
    message = state.get("message", "")
    symbols = _extract_symbols(message)
    if not symbols:
        return {"market_data": None,
                "response": "请告诉我您想查询的股票代码。例如：AAPL、600519。",
                "response_type": "text"}

    quote = await get_quote(symbols[0])
    hist = await get_historical(symbols[0])

    if quote.get("error"):
        return {
            "market_data": None,
            "response": f"抱歉，{symbols[0]} 的行情暂时查不到（数据源可能离线）。请稍后再试。",
            "response_type": "text",
        }

    # baostock 返回字段：current_price / previous_close / high / low / data_source
    # 其他源可能叫 day_high / day_low / source → 都兜底取
    lines = [f"📊 {quote.get('symbol', symbols[0])} 行情："]
    lines += [
        f"  最新价：¥{quote['current_price']}",
        f"  涨跌：¥{quote.get('change', 0):+.2f}（{quote.get('change_percent', 0):+.2f}%）",
        f"  昨收：¥{quote.get('previous_close', 0)}",
        f"  开/高/低：¥{quote.get('open', 0)} / ¥{quote.get('high', quote.get('day_high', 0))} / ¥{quote.get('low', quote.get('day_low', 0))}",
        f"  成交量：{int(quote.get('volume', 0)):,}",
        f"  数据源：{quote.get('data_source', quote.get('source', 'unknown'))}",
    ]
    return {"market_data": {"quote": quote, "historical": hist},
            "response": "\n".join(lines), "response_type": "text"}


# ========== 节点 2c：投资分析（并行 fan-out → 汇总给 LLM）========
async def node_analysis(state: AgentState) -> Dict[str, Any]:
    """并行跑 portfolio + market + rag + news，让 LLM 结合新闻做决策建议。"""
    message = state.get("message", "")
    user_id = state.get("user_id", "")

    # 4 路并发：组合 + 行情 + RAG 知识 + 实时财经快讯（画像也顺带取）
    # 每路加独立超时：任一数据源卡住都只降级该路，不拖垮整次回答
    async def _safe(coro, timeout: float, default):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except Exception as e:
            logger.warning(f"[analysis] 数据源超时或失败，已降级：{e}")
            return default

    async with asyncio.TaskGroup() as tg:
        t_portfolio = tg.create_task(
            _safe(_fetch_portfolio(user_id), 12, {"total_value": 0.0, "pnl_percent": 0.0})
        )
        t_profile = tg.create_task(_safe(get_profile_summary(user_id), 5, ""))
        symbols = _extract_symbols(message)
        t_market = tg.create_task(_safe(get_quote(symbols[0]), 8, None)) if symbols else None
        t_rag = tg.create_task(_safe(retrieve(message), 6, {}))
        t_news = tg.create_task(_safe(fetch_news(limit=10), 6, []))
        t_ind = (
            tg.create_task(_safe(_fetch_indicators(symbols[0]), 8, {}))
            if symbols else None
        )
        t_macro = tg.create_task(_safe(get_macro_overview(), 12, {}))
        t_funds = tg.create_task(_safe(get_fund_flow_overview(), 12, {}))

    portfolio = t_portfolio.result()
    profile_summary = t_profile.result()
    quote = t_market.result() if t_market else None
    rag = t_rag.result()
    news_items = t_news.result()
    indicators = t_ind.result() if t_ind else {}
    macro = t_macro.result()
    funds = t_funds.result()

    # 构建 LLM 上下文
    context_parts = [f"用户问题：{message}"]
    if profile_summary:
        context_parts.append(profile_summary)
    context_parts.append(
        f"投资组合概览：总资产 ¥{portfolio['total_value']:,.2f}，"
        f"累计盈亏 {portfolio['pnl_percent']:+.2f}%"
    )
    if quote and "error" not in quote:
        context_parts.append(
            f"行情数据 {quote['symbol']}：现价 ¥{quote['current_price']}"
            f"（{quote['change_percent']:+.2f}%），"
            f"交易日 {quote.get('trade_date', '未知')}，"
            f"数据源 {quote.get('data_source', 'unknown')}"
        )
        val = []
        if quote.get("pe") is not None:
            val.append(f"PE(动) {quote['pe']}")
        if quote.get("pb") is not None:
            val.append(f"PB {quote['pb']}")
        if quote.get("total_mv") is not None:
            val.append(f"总市值 {quote['total_mv']} 亿")
        if quote.get("turnover_rate") is not None:
            val.append(f"换手率 {quote['turnover_rate']}%")
        if quote.get("volume_ratio") is not None:
            val.append(f"量比 {quote['volume_ratio']}")
        if val:
            context_parts.append("估值与量能：" + "，".join(val))
    if indicators:
        parts = []
        for label, key in (("MA5", "ma5"), ("MA10", "ma10"), ("MA20", "ma20"), ("MA60", "ma60")):
            if indicators.get(key) is not None:
                parts.append(f"{label} ¥{indicators[key]}")
        if indicators.get("rsi14") is not None:
            parts.append(f"RSI14 {indicators['rsi14']}")
        if indicators.get("ret5") is not None:
            parts.append(f"近5日 {indicators['ret5']:+.2f}%")
        if indicators.get("ret20") is not None:
            parts.append(f"近20日 {indicators['ret20']:+.2f}%")
        if indicators.get("pos60") is not None:
            parts.append(f"60日区间位置 {indicators['pos60']}%")
        if indicators.get("volatility20") is not None:
            parts.append(f"20日年化波动率 {indicators['volatility20']}%")
        if indicators.get("vol_ratio_5_20") is not None:
            parts.append(f"5/20日量比 {indicators['vol_ratio_5_20']}")
        if parts:
            context_parts.append(
                f"技术指标（{indicators.get('bars', 0)} 根日线，前复权）：" + "，".join(parts)
            )

    # 宏观经济读数（6 小时缓存，数据缺失时跳过、不留空字段）
    if macro:
        m = []
        if macro.get("gdp", {}).get("yoy") is not None:
            m.append(f"GDP 同比 {macro['gdp']['yoy']}%（{macro['gdp'].get('period', '')}）")
        if macro.get("cpi", {}).get("yoy") is not None:
            m.append(f"CPI 同比 {macro['cpi']['yoy']}%（{macro['cpi'].get('period', '')}）")
        if macro.get("m2", {}).get("yoy") is not None:
            m.append(f"M2 同比 {macro['m2']['yoy']}%")
        if macro.get("shibor", {}).get("rate_1w") is not None:
            m.append(f"SHIBOR 1周 {macro['shibor']['rate_1w']}%")
        if m:
            context_parts.append("宏观环境：" + "，".join(m))

    # 市场资金面（30 分钟缓存）
    if funds:
        f = []
        mf = funds.get("market_main_flow")
        if mf and mf.get("net_inflow_yi") is not None:
            pct = f"（占成交 {mf['net_pct']}%）" if mf.get("net_pct") is not None else ""
            f.append(f"大盘主力净流入 {mf['net_inflow_yi']:+} 亿{pct}（{mf.get('date', '')}）")
        lhb = funds.get("lhb_top") or []
        if lhb:
            tops = "、".join(
                f"{x['name']}({x['code']}) 净买 {x['net_buy_yi']} 亿" for x in lhb[:3]
            )
            f.append(f"最新龙虎榜净买居前：{tops}（{lhb[0].get('date', '')}）")
        if f:
            context_parts.append("市场资金面：" + "；".join(f))

    if rag.get("context"):
        context_parts.append(f"参考知识：\n{rag['context']}")

    # 注入真实新闻（重要！让 AI 基于最新资讯给建议）
    news_text = format_news_for_prompt(news_items, max_items=8)
    context_parts.append(news_text)

    # 调用 LLM 生成分析
    llm = get_llm()
    disclaimer = state.get("disclaimer", "")
    prompt = (
        f"你是一名严谨的 A 股投资顾问。下面 <data> 标签内的内容由系统实时获取，"
        f"是本次分析唯一可引用的数据来源。\n\n"
        f"<data>\n{'\n'.join(context_parts)}\n</data>\n\n"
        f"硬性规则（违反任何一条都视为不合格回答）：\n"
        f"1. 只能引用 <data> 中出现的数字、事实和新闻；严禁编造财务指标、目标价、机构预测、行业景气度数据\n"
        f"2. <data> 中没有提供的数据，直接写「数据不足」，不得用常识推测补齐\n"
        f"3. 结论必须至少引用 2 个 <data> 中的具体数据点（如 MA20、RSI14、近20日涨跌幅、PE、组合仓位）\n"
        f"4. 严格区分「数据显示」与「经验判断」，后者必须标注为推测\n"
        f"5. 不承诺收益、不给精确目标价，必须指出主要风险和不确定性\n"
        f"6. 给出 2-3 种可选策略（保守/稳健/积极），每种一句话，并给出可供回测验证的具体参数"
        f"（标的代码、策略类型如双均线/RSI/动量、关键参数、回测区间）\n"
        f"7. 全文控制在 500 字以内，先给结论，再给数据依据，最后附免责声明\n"
    )
    reply = await llm.achat(
        [{"role": "user", "content": prompt}],
        system_prompt=(
            "你是严谨的 A 股投资顾问。所有数字必须来自用户提供的 <data> 区块，"
            "数据缺失就明确说「数据不足」，绝不编造，绝不承诺收益。"
        ),
    )
    if disclaimer and disclaimer not in reply:
        reply += f"\n\n---\n⚠️ {disclaimer}"

    return {
        "portfolio_data": portfolio,
        "market_data": {"quote": quote} if quote else None,
        "rag_sources": rag.get("sources", []),
        "rag_context": rag.get("context", ""),
        "news_count": len(news_items),
        "news_sources": list({n.get("source", "") for n in news_items}),
        "response": reply,
        "response_type": "markdown",
    }


async def _fetch_portfolio(user_id: str) -> dict:
    from ..database import async_session
    async with async_session() as session:
        return await get_portfolio_summary(user_id, session)


async def _fetch_indicators(symbol: str) -> dict:
    """拉 90 日线并计算技术指标（get_historical 自带 5 分钟缓存）。"""
    hist = await get_historical(symbol, 90)
    return compute_indicators(hist.get("data") or [])


# ========== 节点 2d：知识问答（直接返回 RAG 结果，给 LLM 润色）========
async def node_knowledge(state: AgentState) -> Dict[str, Any]:
    message = state.get("message", "")
    rag = await retrieve(message)

    if not rag.get("context"):
        return {"rag_sources": [], "response": "抱歉，我暂时没找到相关的理财知识。"
                "您可以换个说法试试，或者问我行情、组合、投资建议。",
                "response_type": "text"}

    # 给 LLM 润色
    llm = get_llm()
    prompt = (
        f"请用通俗易懂的语言回答用户的问题：{message}\n\n"
        f"参考以下知识库条目：\n{rag['context']}\n\n"
        f"要求：用中文回答，200-400 字，结构清晰，可以加序号或小标题。"
    )
    reply = await llm.achat([{"role": "user", "content": prompt}])
    return {"rag_sources": rag.get("sources", []), "rag_context": rag.get("context", ""),
            "response": reply, "response_type": "markdown"}


# ========== 节点 2e：闲聊 ==========
async def node_chitchat(state: AgentState) -> Dict[str, Any]:
    message = state.get("message", "")
    llm = get_llm()
    history_ctx = state.get("history_context", "")
    prompt = f"{history_ctx}\n\n用户：{message}\n\n作为友好的 衡策 投资助手，简短回复（50-100 字）。"
    reply = await llm.achat([{"role": "user", "content": prompt}])
    return {"response": reply, "response_type": "text"}


# ========== 节点 2f：风险评估 ==========
async def node_risk(state: AgentState) -> Dict[str, Any]:
    message = state.get("message", "")
    user_id = state.get("user_id", "")
    symbols = _extract_symbols(message)
    lines = ["⚠️ 风险评估（简化版，仅供参考）："]
    if symbols:
        quote = await get_quote(symbols[0])
        # 用当前涨跌幅做简单波动提示
        pct = abs(float(quote.get("change_percent", 0)))
        if pct > 5:
            lines.append(f"· {quote['symbol']} 近期波动 {pct:.1f}%，属于高波动资产")
        elif pct > 2:
            lines.append(f"· {quote['symbol']} 波动 {pct:.1f}%，中等风险")
        else:
            lines.append(f"· {quote['symbol']} 波动 {pct:.1f}%，相对稳定")
        lines.append(f"· 数据源：{quote.get('source', 'unknown')}")
    else:
        lines.append("· 请告诉我具体标的（如 AAPL 或 600519.SS），我可以做针对性分析")

    # 仓位建议跟随用户风险偏好（画像里选的档位）
    risk = await get_risk_level(user_id)
    position = {
        "conservative": ("10%", "50%"),
        "moderate": ("20%", "70%"),
        "balanced": ("25%", "80%"),
        "aggressive": ("35%", "90%"),
    }.get(risk, ("20%", "70%"))
    risk_cn = {"conservative": "保守", "moderate": "稳健",
               "balanced": "平衡", "aggressive": "积极"}.get(risk, "稳健")
    lines.append(
        f"\n按你的风险偏好（{risk_cn}）：单笔仓位 ≤ {position[0]}，"
        f"总权益仓位 ≤ {position[1]}，分散 5-10 个标的。"
        f"\n可在「投资画像」页面调整风险偏好，建议会自动跟着变。"
    )
    return {"response": "\n".join(lines), "response_type": "text"}


# ========== 节点 2g：预警配置 ==========
async def node_alert(state: AgentState) -> Dict[str, Any]:
    message = state.get("message", "")
    symbols = _extract_symbols(message)
    return {
        "response": (
            "🔔 到价预警已经上线。\n"
            f"已识别到标的：{symbols or '未明确'}\n\n"
            "打开侧边栏「到价预警」页面即可添加：\n"
            "  · 价格上穿 / 下穿指定价位（≥ ≤ > <）\n"
            "  · 系统每 60 秒在交易时段自动检查一次\n"
            "  · 触发后自动停用，并记录触发价与时间\n"
            "  · 配置 SMTP 后还会同时发邮件到你的注册邮箱"
        ),
        "response_type": "text",
    }


# ========== 节点 2h：策略选股（全市场扫描）========

# 用户说"选出金叉的""筛超卖的""帮我找出放量股票"——需要把自然语言信号 id 关联起来
_SCREENER_KEYWORD_MAP: list[tuple[str, str]] = [
    # 信号关键词 → screener 用的 signal id
    ("金叉", "ma5_cross_ma20"),
    ("多头排列", "ma_bullish"),
    ("均线多头", "ma_bullish"),
    ("超卖", "rsi_oversold"),
    ("超买", "rsi_overbought"),
    ("突破", "breakout_20d"),
    ("新高", "breakout_20d"),
    ("放量", "vol_surge"),
    ("量比", "vol_surge"),
    ("锤子线", "hammer"),
]


def _parse_screener_signal(message: str) -> str:
    """从用户消息里推断要扫描的信号 id。没匹配到就给最友好的默认。"""
    for kw, sid in _SCREENER_KEYWORD_MAP:
        if kw.lower() in message.lower():
            return sid
    return "breakout_20d"  # 默认"突破 20 日高点"——最有参考价值的信号


async def node_screener(state: AgentState) -> Dict[str, Any]:
    message = state.get("message", "")
    signal_id = _parse_screener_signal(message)

    from ..api.market import _STRATEGY_SIGNALS, _init_strategy_signals
    _init_strategy_signals()
    signal_meta = _STRATEGY_SIGNALS.get(signal_id)
    signal_name = signal_meta[0] if signal_meta else signal_id

    try:
        results = await _run_screener_direct(signal_id, top_n=15)

        if not results:
            return {
                "response": (
                    f"全市场扫描完成（信号：{signal_name}），"
                    f"当前没有触发条件的股票。\n"
                    f"可能原因：当前不是交易日、或信号条件较严格。"
                    f"可以试试放宽条件或换一个信号。"
                ),
                "response_type": "text",
            }

        lines = [f"📊 全市场扫描 · 信号：{signal_name}", f"共扫描 5500+ 只，找到 {len(results)} 只：", ""]
        for i, r in enumerate(results[:10], 1):
            lines.append(
                f"{i}. {r['name']}（{r['code']}）· ¥{r['price']:.2f} · "
                f"{r['change_pct']:+.2f}% · 成交额 {_fmt_amount(r.get('amount', 0))}"
            )
        if len(results) > 10:
            lines.append(f"\n... 还有 {len(results) - 10} 只，请到「A 股全景 → 策略选股」查看完整列表")

        lines.append(
            "\n💡 提示：点击任意股票代码可跳转行情页查看 K 线，"
            "选中后可一键进回测验证信号是否靠谱。"
        )
        return {"response": "\n".join(lines), "response_type": "text"}

    except Exception as e:
        logger.warning("screener node 扫描失败：%s", e)
        return {
            "response": f"扫描暂时不可用：{e}",
            "response_type": "text",
        }


async def _run_screener_direct(signal: str, top_n: int = 15) -> list:
    """绕过 limiter 直接跑 screener 逻辑的内部函数（走批量 baostock 查询）。"""
    from ..api.market import (
        _STRATEGY_SIGNALS, _init_strategy_signals,
        _cached_fetch, _fetch_screener_sync,
        _bs_query_batch, _cache_get, _cache_set,
    )
    from ..agent.tools import normalize_a_share
    import pandas as pd

    _init_strategy_signals()
    if signal not in _STRATEGY_SIGNALS:
        return []
    name, desc, need_days, check_fn = _STRATEGY_SIGNALS[signal]

    full_items = await _cached_fetch("screener_full", _fetch_screener_sync)

    bs_groups: list[tuple[str, dict]] = []
    for item in full_items:
        bc = normalize_a_share(item["code"])
        if bc:
            bs_groups.append((bc, item))

    _BATCH_SIZE = 80
    _MAX_SCAN = min(len(bs_groups), 2000)
    hits: list = []

    for batch_start in range(0, _MAX_SCAN, _BATCH_SIZE):
        if len(hits) >= top_n * 3:
            break
        batch = bs_groups[batch_start : batch_start + _BATCH_SIZE]

        uncached = []
        cached_bars: dict[str, list] = {}
        for bc, _ in batch:
            cb = _cache_get(f"screener_hist:{bc}:{need_days}")
            if cb is not None:
                cached_bars[bc] = cb
            else:
                uncached.append(bc)

        if uncached:
            batch_result = await _bs_query_batch(uncached, need_days)
            for bc, bars in batch_result.items():
                _cache_set(f"screener_hist:{bc}:{need_days}", bars)
            cached_bars.update(batch_result)
            await asyncio.sleep(0.1)

        for bc, item in batch:
            if len(hits) >= top_n * 3:
                break
            bars = cached_bars.get(bc)
            if not bars or len(bars) < need_days:
                continue
            try:
                df = pd.DataFrame(bars).sort_values("date").reset_index(drop=True)
                if check_fn(df):
                    hits.append({**item, "signal": signal, "signal_name": name})
            except Exception:
                continue

    hits.sort(key=lambda x: x.get("change_pct", 0), reverse=True)
    return hits[:top_n]


def _fmt_amount(v: float) -> str:
    if v >= 1e8:
        return f"{v/1e8:.1f}亿"
    if v >= 1e4:
        return f"{v/1e4:.0f}万"
    return f"{v:.0f}"


# ========== 路由分发函数（LangGraph conditional_edges 用）========
def route_by_intent(state: AgentState) -> str:
    """根据意图返回下一个节点名（或 'clarification'）。"""
    if state.get("needs_clarification"):
        return "clarification"
    intent = state.get("intent", "chitchat")
    mapping = {
        "portfolio_query": "portfolio",
        "market_query": "market",
        "investment_analysis": "analysis",
        "knowledge_query": "knowledge",
        "chitchat": "chitchat",
        "risk_assessment": "risk",
        "alert_config": "alert",
        "screener": "screener",
    }
    return mapping.get(intent, "chitchat")
