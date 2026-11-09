"""基准跟随 / 跟踪误差分析。

口径（与前端展示保持一致，禁止混用算术平均与几何口径）：
  - 日收益      r_t = nav_t / nav_{t-1} - 1
  - 累计收益    几何：Π(1 + r_t) - 1
  - 年化收益    几何：(1 + 累计收益) ^ (252 / N) - 1，N 为区间交易日数
  - 累计超额    策略累计收益 - 基准累计收益（两个几何累计收益之差）
  - 年化超额    策略年化收益 - 基准年化收益
  - 跟踪误差    std(r_p - r_b, ddof=1) * √252
  - Beta        cov(r_p, r_b, ddof=1) / var(r_b, ddof=1)
  - Alpha       策略年化收益 - Beta * 基准年化收益（无风险利率取 0）
  - 最大回撤    max(1 - nav_t / cummax(nav_t))

数据来源：
  - 沪深300 / 中证500：baostock 真实指数日线（sh.000300 / sh.000905），
    失败时回退 akshare stock_zh_index_daily_em，返回结果标注实际来源。
  - 自选组合：用户当前持仓按市值权重固定买入持有，区间起点归一。
  - 不生成任何模拟行情。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from loguru import logger
from sqlalchemy import select

from .. import baostock_gateway
from ..agent.tools import get_quote, normalize_a_share
from ..backtest_engine.runner import run_strategy_backtest
from ..database import async_session
from ..models.backtest_run import BacktestRun
from ..models.portfolio import Holding, Portfolio

ANN_FACTOR = 252

BENCHMARKS: Dict[str, Dict[str, Any]] = {
    "hs300": {"name": "沪深300", "code": "sh.000300", "index_code": "000300"},
    "zz500": {"name": "中证500", "code": "sh.000905", "index_code": "000905"},
    "custom": {"name": "自选组合", "code": None, "index_code": None},
}

FORMULAS: Dict[str, str] = {
    "daily_return": "r_t = nav_t / nav_{t-1} - 1",
    "cumulative_return": "Π(1 + r_t) - 1（几何）",
    "annualized_return": "(1 + 累计收益) ^ (252 / N) - 1（几何，N 为区间交易日数）",
    "excess_return": "策略累计收益 - 基准累计收益（两个几何累计收益之差）",
    "annualized_excess": "策略年化收益 - 基准年化收益",
    "tracking_error": "std(r_p - r_b, ddof=1) × √252",
    "beta": "cov(r_p, r_b, ddof=1) / var(r_b, ddof=1)",
    "alpha": "策略年化收益 - Beta × 基准年化收益（无风险利率取 0）",
    "max_drawdown": "max(1 - nav_t / cummax(nav_t))",
}

_INDUSTRY_CACHE: Dict[str, Tuple[float, str]] = {}
_INDUSTRY_TTL = 24 * 3600.0
_INDUSTRY_FAIL_TTL = 300.0  # 失败后 5 分钟内不再打东财接口，避免加重限流


class BenchmarkError(ValueError):
    """业务可预期的失败（参数/数据不足），API 层转 400。"""


# ---------------- 基础工具 ----------------

def _bare_symbol(symbol: str) -> str:
    """sh.600519 / 600519.SH / 600519 → 600519。"""
    bs = normalize_a_share(symbol)
    return bs.split(".")[1] if bs else symbol.strip()


def _max_drawdown(nav: np.ndarray) -> float:
    if len(nav) == 0:
        return 0.0
    peak = np.maximum.accumulate(nav)
    return float(((peak - nav) / peak).max() * 100)


def _metrics(nav_p: np.ndarray, nav_b: np.ndarray) -> Dict[str, Any]:
    """对齐后的净值序列 → 全部指标（口径见模块 docstring）。"""
    if len(nav_p) < 2 or len(nav_b) < 2:
        raise BenchmarkError("对齐后的交易日不足 2 天，无法计算指标")

    r_p = np.diff(nav_p) / nav_p[:-1]
    r_b = np.diff(nav_b) / nav_b[:-1]
    n = len(r_p)
    if n < 1:
        raise BenchmarkError("对齐后的交易日不足，无法计算跟踪误差")

    cum_p = float(nav_p[-1] / nav_p[0] - 1)
    cum_b = float(nav_b[-1] / nav_b[0] - 1)
    ann_p = float((1 + cum_p) ** (ANN_FACTOR / n) - 1)
    ann_b = float((1 + cum_b) ** (ANN_FACTOR / n) - 1)

    active = r_p - r_b
    te = float(np.std(active, ddof=1) * np.sqrt(ANN_FACTOR)) if n > 1 else 0.0
    var_b = float(np.var(r_b, ddof=1)) if n > 1 else 0.0
    cov = float(np.cov(r_p, r_b, ddof=1)[0][1]) if n > 1 else 0.0
    beta = cov / var_b if var_b > 0 else 0.0
    alpha = ann_p - beta * ann_b

    return {
        "strategy": {
            "cumulative_return_pct": round(cum_p * 100, 2),
            "annualized_return_pct": round(ann_p * 100, 2),
            "max_drawdown_pct": round(_max_drawdown(nav_p), 2),
            "final_nav": round(float(nav_p[-1] / nav_p[0]), 4),
        },
        "benchmark": {
            "cumulative_return_pct": round(cum_b * 100, 2),
            "annualized_return_pct": round(ann_b * 100, 2),
            "max_drawdown_pct": round(_max_drawdown(nav_b), 2),
            "final_nav": round(float(nav_b[-1] / nav_b[0]), 4),
        },
        "excess": {
            "cumulative_pct": round((cum_p - cum_b) * 100, 2),
            "annualized_pct": round((ann_p - ann_b) * 100, 2),
            "tracking_error_pct": round(te * 100, 2),
            "beta": round(beta, 4),
            "alpha_pct": round(alpha * 100, 2),
        },
        "trading_days": n,
        "risk_free_rate": 0.0,
    }


def _align_series(
    strat: Sequence[Dict[str, Any]], bench: Sequence[Dict[str, Any]]
) -> Tuple[List[str], np.ndarray, np.ndarray]:
    """按日期交集对齐，并把两端净值都归一到起点 1.0。"""
    smap = {str(b["date"]): float(b["close"]) for b in strat}
    bmap = {str(b["date"]): float(b["close"]) for b in bench}
    dates = sorted(set(smap) & set(bmap))
    if len(dates) < 20:
        raise BenchmarkError(f"策略与基准重合交易日只有 {len(dates)} 天，不足以做跟踪误差分析")
    p = np.array([smap[d] for d in dates], dtype=float)
    b = np.array([bmap[d] for d in dates], dtype=float)
    p = p / p[0]
    b = b / b[0]
    return dates, p, b


# ---------------- 指数行情 ----------------

async def _fetch_index_bars(code: str, days: int) -> Tuple[List[Dict[str, Any]], str]:
    """拉指数日线：baostock 主源 → akshare 兜底。返回 (bars, source)。"""
    end = datetime.now().date()
    start = end - timedelta(days=int(max(days, 30) * 1.8) + 60)
    bars = await baostock_gateway.fetch_bars(
        code, start.isoformat(), end.isoformat(), frequency="d", adjustflag="3", timeout=30
    )
    if bars:
        return bars, "baostock"

    # 兜底：akshare 东方财富指数日线（全历史，本地按日期截取）
    import akshare as ak

    def _fetch():
        symbol = code.replace(".", "")  # sh.000300 → sh000300
        df = ak.stock_zh_index_daily_em(symbol=symbol)
        rows = []
        for _, r in df.iterrows():
            d = str(r["date"])[:10]
            if d >= start.isoformat():
                rows.append({"date": d, "open": float(r["open"]), "high": float(r["high"]),
                             "low": float(r["low"]), "close": float(r["close"]),
                             "volume": float(r.get("volume") or 0)})
        return rows

    try:
        rows = await asyncio.wait_for(asyncio.to_thread(_fetch), 30)
    except Exception as e:
        logger.warning(f"[benchmark] akshare 指数兜底失败 {code}: {e}")
        rows = []
    if not rows:
        raise BenchmarkError(f"基准行情不可用（baostock 与 akshare 均失败）：{code}")
    return rows, "akshare"


# ---------------- 持仓 / 自选组合 ----------------

async def _load_holdings(user_id: str) -> List[Dict[str, Any]]:
    """读取当前持仓，并按市值计算权重。无持仓返回空列表。"""
    async with async_session() as db:
        p = (
            await db.execute(select(Portfolio).where(Portfolio.user_id == user_id))
        ).scalar_one_or_none()
        if not p:
            return []
        rows = (
            await db.execute(select(Holding).where(Holding.portfolio_id == p.id))
        ).scalars().all()

    items: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(4)

    async def _value(h: Holding) -> Dict[str, Any]:
        async with sem:
            try:
                q = await get_quote(h.symbol)
            except Exception:
                q = {}
        price = float(q.get("current_price") or 0) or float(h.avg_cost or 0)
        qty = float(h.quantity or 0)
        return {
            "symbol": h.symbol,
            "name": (q.get("name") if q else None) or h.symbol,
            "quantity": qty,
            "price": price,
            "market_value": price * qty,
        }

    if rows:
        items = await asyncio.gather(*[_value(h) for h in rows])

    total = sum(i["market_value"] for i in items)
    if total <= 0:
        return []
    for i in items:
        i["weight"] = i["market_value"] / total
    return items


async def _portfolio_curve(user_id: str, days: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    """自选组合净值：当前权重固定买入持有，起点归一。

    返回 (bars, holdings, source)。bars 的 close 为组合净值。
    """
    holdings = await _load_holdings(user_id)
    if not holdings:
        raise BenchmarkError("当前组合没有持仓，无法构建自选组合基准")

    from ..agent.backtest import fetch_history_bars

    sem = asyncio.Semaphore(4)

    async def _bars(h: Dict[str, Any]):
        async with sem:
            try:
                return h["symbol"], await fetch_history_bars(h["symbol"], days=days + 40)
            except Exception as e:
                logger.warning(f"[benchmark] 持仓行情失败 {h['symbol']}: {e}")
                return h["symbol"], []

    results = await asyncio.gather(*[_bars(h) for h in holdings])
    by_symbol: Dict[str, Dict[str, float]] = {}
    for sym, bars in results:
        by_symbol[sym] = {str(b["date"]): float(b["close"]) for b in bars}

    # 所有标的都有行情的日期才参与组合净值
    common: Optional[set[str]] = None
    for h in holdings:
        ds = set(by_symbol.get(h["symbol"], {}).keys())
        if not ds:
            raise BenchmarkError(f"持仓 {h['symbol']} 无历史行情，无法构建自选组合基准")
        common = ds if common is None else (common & ds)
    dates = sorted(common or set())
    dates = dates[-days:] if len(dates) > days else dates
    if len(dates) < 20:
        raise BenchmarkError("持仓行情重合交易日不足 20 天，无法构建自选组合基准")

    curve = []
    for d in dates:
        nav = 0.0
        for h in holdings:
            series = by_symbol[h["symbol"]]
            base = series[dates[0]]
            if base <= 0:
                raise BenchmarkError(f"持仓 {h['symbol']} 起点价格异常")
            nav += h["weight"] * (series[d] / base)
        curve.append({"date": d, "close": round(nav, 6)})
    return curve, holdings, "用户持仓（当前市值权重，固定权重买入持有）"


# ---------------- 策略净值 ----------------

async def _strategy_curve(payload: Dict[str, Any], user_id: str) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """返回 (bars, 来源说明, 描述信息)。"""
    source = payload["source"]
    if source == "run":
        run_id = payload.get("run_id")
        if not run_id:
            raise BenchmarkError("source=run 时必须提供 run_id")
        async with async_session() as db:
            run = await db.get(BacktestRun, run_id)
            if not run or run.user_id != user_id:
                raise BenchmarkError("回测记录不存在或不属于当前用户")
            curve = json.loads(run.equity_curve_json or "[]")
            meta = {
                "name": f"{run.strategy_name} · {run.symbol}",
                "symbol": run.symbol,
                "strategy": run.strategy,
                "initial_capital": run.initial_capital,
            }
        if len(curve) < 20:
            raise BenchmarkError("该回测记录没有保存权益曲线，请重新运行并保存后再分析")
        bars = [
            {"date": c["date"], "close": float(c["equity"])}
            for c in curve
            if c.get("equity") is not None
        ]
        return bars, f"回测记录（{run_id[:8]}…，保存时的权益曲线）", meta

    if source == "portfolio":
        bars, holdings, src = await _portfolio_curve(user_id, int(payload.get("days") or 120))
        meta = {
            "name": "我的投资组合（模拟）",
            "symbol": "",
            "strategy": "portfolio",
            "initial_capital": round(sum(h["market_value"] for h in holdings), 2),
        }
        return bars, src, meta

    # source == "strategy"
    symbol = (payload.get("symbol") or "").strip()
    if not symbol:
        raise BenchmarkError("source=strategy 时必须提供 symbol")
    result = await run_strategy_backtest(
        symbol=symbol,
        strategy=payload.get("strategy") or "ma_cross",
        params=payload.get("params") or {},
        initial_capital=float(payload.get("initial_capital") or 200_000),
        days=int(payload.get("days") or 120),
    )
    curve = result.get("equity_curve") or []
    if len(curve) < 20:
        raise BenchmarkError("策略权益曲线不足 20 个交易日，无法分析")
    bars = [{"date": c["date"], "close": float(c["equity"])} for c in curve]
    meta = {
        "name": result.get("strategy_name") or payload.get("strategy"),
        "symbol": symbol,
        "strategy": payload.get("strategy"),
        "initial_capital": float(payload.get("initial_capital") or 200_000),
    }
    return bars, "baostock 前复权日线 + 内置策略回测", meta


# ---------------- 行业暴露 ----------------

async def _industry_of(symbol: str) -> str:
    """行业分类：baostock 申万行业（一次拉全市场，24h 缓存），失败回退东财。"""
    bare = _bare_symbol(symbol)
    hit = _INDUSTRY_CACHE.get(bare)
    if hit:
        ttl = _INDUSTRY_TTL if hit[1] else _INDUSTRY_FAIL_TTL
        if time.time() - hit[0] < ttl:
            return hit[1]

    basics = await baostock_gateway.fetch_stock_basics()
    if basics:
        info = basics.get(normalize_a_share(bare) or "")
        if info and info.get("industry"):
            # baostock 返回形如 "C15酒、饮料和精制茶制造业"，去掉行业代码前缀
            industry = re.sub(r"^[A-Z]\d{1,2}", "", str(info["industry"]).strip()) or str(
                info["industry"]
            ).strip()
            _INDUSTRY_CACHE[bare] = (time.time(), industry)
            return industry

    def _fetch() -> str:
        import akshare as ak

        df = ak.stock_individual_info_em(symbol=bare)
        mapping = {str(r["item"]).strip(): r["value"] for _, r in df.iterrows()}
        return str(mapping.get("行业") or "").strip()

    try:
        industry = ""
        for attempt in range(3):
            try:
                industry = await asyncio.wait_for(asyncio.to_thread(_fetch), 25)
            except Exception as e:
                logger.warning(f"[benchmark] 行业分类获取失败 {bare}（第 {attempt + 1} 次）: {e}")
                if attempt < 2:
                    await asyncio.sleep(2 + attempt * 3)  # 东财限流时不要连打
                continue
            if industry:
                break
    except Exception as e:  # 兜底，绝不影响基准分析主流程
        logger.warning(f"[benchmark] 行业分类异常 {bare}: {e}")
        industry = ""
    # 成功缓存 24h；失败缓存 5min（空串），避免反复触发限流
    _INDUSTRY_CACHE[bare] = (time.time(), industry)
    return industry


async def _industry_weights(holdings: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, float], List[str]]:
    sem = asyncio.Semaphore(2)

    async def _one(h: Dict[str, Any]):
        async with sem:
            return h["weight"], await _industry_of(h["symbol"])

    results = await asyncio.gather(*[_one(h) for h in holdings])
    weights: Dict[str, float] = {}
    missing: List[str] = []
    for (h, (w, industry)) in zip(holdings, results):
        if not industry:
            missing.append(h["symbol"])
            industry = "未知"
        weights[industry] = weights.get(industry, 0.0) + w
    return weights, missing


async def _industry_section(
    strategy_holdings: Optional[Sequence[Dict[str, Any]]],
    benchmark_holdings: Optional[Sequence[Dict[str, Any]]],
    benchmark: str,
) -> Dict[str, Any]:
    """行业暴露与偏离。基准为指数时无法拿到成分股权重 → 只出组合暴露并标注。"""
    if not strategy_holdings:
        return {
            "available": False,
            "benchmark_available": False,
            "items": [],
            "note": "当前分析对象不是持仓组合（策略回测/历史记录），没有行业权重可分解。",
        }

    strat_w, missing_strategy = await _industry_weights(strategy_holdings)
    items: List[Dict[str, Any]] = []
    if benchmark_holdings:
        bench_w, missing_bench = await _industry_weights(benchmark_holdings)
        for ind in sorted(set(strat_w) | set(bench_w), key=lambda x: strat_w.get(x, 0), reverse=True):
            items.append({
                "industry": ind,
                "weight_pct": round(strat_w.get(ind, 0.0) * 100, 2),
                "benchmark_weight_pct": round(bench_w.get(ind, 0.0) * 100, 2),
                "deviation_pct": round((strat_w.get(ind, 0.0) - bench_w.get(ind, 0.0)) * 100, 2),
            })
        note = "行业偏离 = 组合行业权重 - 基准组合行业权重（证监会行业分类，baostock）。"
        missing = missing_strategy + missing_bench
        if missing:
            note += f" 有 {len(missing)} 只持仓行业分类获取失败，已归入「未知」。"
        return {"available": True, "benchmark_available": True, "items": items, "note": note}

    for ind in sorted(strat_w, key=lambda x: strat_w[x], reverse=True):
        items.append({
            "industry": ind,
            "weight_pct": round(strat_w[ind] * 100, 2),
            "benchmark_weight_pct": None,
            "deviation_pct": None,
        })
    note = (
        "基准为指数时，指数成分股的行业权重没有稳定的免费批量数据源，"
        "因此只展示组合行业暴露，不计算行业偏离（不做估算）。"
    )
    if missing_strategy:
        note += f" 另有 {len(missing_strategy)} 只持仓行业分类缺失，已归入「未知」。"
    return {"available": True, "benchmark_available": False, "items": items, "note": note}


# ---------------- 对外入口 ----------------

async def analyze_benchmark(user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """执行一次基准跟随分析。payload 结构见 api/benchmark.py。"""
    source = payload.get("source") or "strategy"
    bench_id = payload.get("benchmark") or "hs300"
    if bench_id not in BENCHMARKS:
        raise BenchmarkError(f"未知基准：{bench_id}")
    if source == "portfolio" and bench_id == "custom":
        raise BenchmarkError("分析对象与基准不能同时为自选组合")

    days = int(payload.get("days") or 120)

    strat_bars, strat_source, strat_meta = await _strategy_curve(payload, user_id)

    strategy_holdings: Optional[List[Dict[str, Any]]] = None
    if source == "portfolio":
        strategy_holdings = await _load_holdings(user_id)
    elif strat_meta.get("symbol"):
        # 单标的策略：行业权重按 100% 计入，才能与自选组合基准做行业偏离
        strategy_holdings = [{"symbol": strat_meta["symbol"], "weight": 1.0}]

    bench_holdings: Optional[List[Dict[str, Any]]] = None
    if bench_id == "custom":
        bench_bars, bench_holdings, bench_source = await _portfolio_curve(user_id, days)
        bench_name = BENCHMARKS["custom"]["name"]
        bench_code = None
    else:
        meta = BENCHMARKS[bench_id]
        bench_bars, bench_source = await _fetch_index_bars(meta["code"], days)
        bench_name = meta["name"]
        bench_code = meta["code"]

    dates, nav_p, nav_b = _align_series(strat_bars, bench_bars)
    metrics = _metrics(nav_p, nav_b)

    series = []
    for d, p, b in zip(dates, nav_p, nav_b):
        series.append({
            "date": d,
            "strategy_nav": round(float(p), 6),
            "benchmark_nav": round(float(b), 6),
            "excess_ratio": round(float(p / b), 6),
            "excess_pct": round(float((p / b - 1) * 100), 4),
        })

    industry = await _industry_section(strategy_holdings, bench_holdings, bench_id)
    if industry.get("available") and source != "portfolio" and len(strategy_holdings or []) == 1:
        industry["note"] += " 策略为单标的，行业权重按 100% 计。"

    notes = [
        "累计收益、年化收益均按几何口径计算，未使用算术平均。",
        "超额收益 = 策略累计收益 - 基准累计收益；曲线中的 excess_pct = 策略净值 / 基准净值 - 1。",
        "Alpha 使用无风险利率 0，属于简化口径。",
        "基准行情为真实历史数据，来源见 data_sources，未使用任何模拟数据。",
    ]
    if source == "portfolio" or bench_id == "custom":
        notes.append("自选组合按当前持仓市值权重固定买入持有，区间起点归一，未考虑调仓与交易成本。")

    return {
        "strategy": strat_meta,
        "benchmark": {
            "id": bench_id, "name": bench_name, "code": bench_code, "source": bench_source,
        },
        "period": {"start": dates[0], "end": dates[-1], "trading_days": len(dates)},
        "series": series,
        "metrics": metrics,
        "industry": industry,
        "data_sources": {
            "strategy": strat_source,
            "benchmark": bench_source,
            "industry": "baostock 证监会行业分类（失败回退东方财富）" if industry.get("available") else "",
        },
        "formulas": FORMULAS,
        "notes": notes,
    }


async def benchmark_options(user_id: str) -> Dict[str, Any]:
    """基准下拉选项 + 当前自选组合可用性。"""
    holdings = await _load_holdings(user_id)
    options = []
    for bid, meta in BENCHMARKS.items():
        item = {
            "id": bid,
            "name": meta["name"],
            "code": meta["code"],
            "available": True,
            "desc": "",
        }
        if bid == "custom":
            item["available"] = bool(holdings)
            item["desc"] = (
                f"按当前 {len(holdings)} 只持仓的市值权重构建"
                if holdings else "当前组合没有持仓，暂不可用"
            )
        else:
            item["desc"] = "baostock 真实指数日线"
        options.append(item)

    return {
        "benchmarks": options,
        "holdings_count": len(holdings),
        "formulas": FORMULAS,
        "data_source_note": (
            "指数行情：baostock（sh.000300 / sh.000905），失败回退 akshare；"
            "自选组合：用户持仓 + baostock 个股日线；行业：东方财富行业分类。"
        ),
    }
