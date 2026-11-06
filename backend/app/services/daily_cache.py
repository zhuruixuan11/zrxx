"""全市场日线本地缓存服务。

解决的问题
----------
策略选股扫描全市场 2000 只股票，每只要拉 21 日 K 线，走 baostock 网关
全程串行限速，实测一次扫描 ~167s。本模块把全市场日线落到 SQLite，
每日收盘后（16:35）后台刷新一次；扫描时直接读本地，秒级返回。

新鲜度判定
----------
``is_fresh()`` 同时满足才算新鲜：
  1. refreshed_at 距现在 < 24h（保证每天最多只有一天偏移）
  2. bar_date（库内最大交易日）距"预期最近交易日"不超过 5 天（容忍长假）
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Sequence

from loguru import logger
from sqlalchemy import bindparam, text

from ..database import async_session
from ..models.daily_bar import DailyCacheMeta

# 只保留最近 120 个自然日（约 80 个交易日），信号最多需要 21 个交易日
_WINDOW_DAYS = 120
_CHUNK_SIZE = 100          # 每批拉取/写入的股票数
_FRESH_MAX_AGE = timedelta(hours=24)
_HOLIDAY_SLACK = timedelta(days=5)
_REFRESH_HM = time(16, 35)  # 每日刷新时间（收盘后，数据已完整）

_refresh_lock = asyncio.Lock()
_scheduler_task: asyncio.Task | None = None


# ---------------- 新鲜度 ----------------

def _expected_bar_date(now: datetime) -> date:
    """预期最近交易日：15:35 前看前一工作日，周末回退到周五。"""
    d = now.date()
    if now.time() < _REFRESH_HM:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


async def _meta_get(db, key: str) -> str | None:
    row = await db.get(DailyCacheMeta, key)
    return row.value if row else None


async def cache_status() -> Dict[str, Any]:
    """缓存状态（新鲜度、覆盖量、数据日期），供调度与排查。"""
    async with async_session() as db:
        refreshed_at = await _meta_get(db, "refreshed_at")
        bar_date = await _meta_get(db, "bar_date")
        symbols = (
            await db.execute(text("SELECT COUNT(DISTINCT symbol) FROM daily_bar_cache"))
        ).scalar() or 0

    fresh = False
    expected = _expected_bar_date(datetime.now())
    if refreshed_at and bar_date:
        try:
            age = datetime.now() - datetime.fromisoformat(refreshed_at)
            lag = expected - date.fromisoformat(bar_date)
            fresh = age < _FRESH_MAX_AGE and lag <= _HOLIDAY_SLACK
        except ValueError:
            pass

    return {
        "fresh": fresh,
        "refreshed_at": refreshed_at,
        "bar_date": bar_date,
        "expected_bar_date": expected.isoformat(),
        "symbols": symbols,
        "refreshing": _refresh_lock.locked(),
    }


async def is_fresh() -> bool:
    return (await cache_status())["fresh"]


# ---------------- 读取 ----------------

async def get_cached_bars(
    codes: Sequence[str], need_days: int, min_date: str
) -> Dict[str, List[Dict[str, Any]]]:
    """批量读本地日线。只返回行数 >= need_days 的代码（不够的留给网关兜底）。

    返回 {bs_code: [bars...]}，bars 按日期升序，结构与网关 fetch_bars 一致。
    """
    if not codes:
        return {}

    stmt = text(
        "SELECT symbol, date, open, high, low, close, volume "
        "FROM daily_bar_cache WHERE symbol IN :codes AND date >= :min_date "
        "ORDER BY symbol, date"
    ).bindparams(bindparam("codes", expanding=True))

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    async with async_session() as db:
        # SQLite 变量上限较老版本为 999，分块查询稳妥
        for i in range(0, len(codes), 500):
            chunk = list(codes[i : i + 500])
            rows = (await db.execute(stmt, {"codes": chunk, "min_date": min_date})).all()
            for symbol, d, o, h, l, c, v in rows:
                grouped.setdefault(symbol, []).append({
                    "date": d, "open": o, "high": h, "low": l, "close": c, "volume": v,
                })

    return {code: bars for code, bars in grouped.items() if len(bars) >= need_days}


# ---------------- 刷新 ----------------

async def refresh_daily_cache() -> Dict[str, Any]:
    """全量刷新：全市场每只拉最近 _WINDOW_DAYS 天日线，批量 upsert。

    已有刷新进行中时直接跳过（调度与手动触发可能撞车）。
    """
    if _refresh_lock.locked():
        logger.info("[daily-cache] 已有刷新进行中，跳过本次触发")
        return {"ok": False, "reason": "already_running"}

    async with _refresh_lock:
        from .. import baostock_gateway
        from ..agent.tools import normalize_a_share
        from ..api.market import _cached_fetch, _fetch_screener_sync

        started = time_now = datetime.now()
        logger.info("[daily-cache] 开始全市场日线刷新")

        items = await _cached_fetch("screener_full", _fetch_screener_sync)
        seen: set[str] = set()
        codes: List[str] = []
        for it in items:
            bc = normalize_a_share(it["code"])
            if bc and bc not in seen:
                seen.add(bc)
                codes.append(bc)

        today = date.today()
        start = (today - timedelta(days=_WINDOW_DAYS)).isoformat()
        end = today.isoformat()

        # 断点续传：已有足够近期数据的股票直接跳过。
        # 首次全量约 1 小时，中途若 baostock 抖动中断，重跑只需补剩下的部分。
        skip_after = (_expected_bar_date(datetime.now()) - timedelta(days=5)).isoformat()
        async with async_session() as db:
            rows = (await db.execute(text(
                "SELECT symbol, MAX(date) AS d, COUNT(*) AS n FROM daily_bar_cache "
                "WHERE date >= :start GROUP BY symbol"
            ), {"start": start})).all()
        ready = {sym for sym, d, n in rows if d and d >= skip_after and n >= 21}
        pending = [c for c in codes if c not in ready]
        logger.info(
            f"[daily-cache] 共 {len(codes)} 只，已完成 {len(ready)} 只，"
            f"待刷新 {len(pending)} 只（{start} ~ {end}）"
        )

        upsert_sql = text(
            "INSERT OR REPLACE INTO daily_bar_cache "
            "(symbol, date, open, high, low, close, volume) "
            "VALUES (:symbol, :date, :open, :high, :low, :close, :volume)"
        )

        total_rows = 0
        done = 0
        aborted = False
        for i in range(0, len(pending), _CHUNK_SIZE):
            chunk = pending[i : i + _CHUNK_SIZE]
            result = None
            # baostock 偶发不可用时给两次补救机会（等熔断冷却结束再试），
            # 全部失败才判定本轮中止，避免一次抖动就前功尽弃
            for attempt in range(3):
                result = await baostock_gateway.fetch_bars_many(
                    chunk, start, end, frequency="d", adjustflag="3", timeout=180,
                )
                if result is not None:
                    break
                if attempt < 2:
                    logger.warning(
                        f"[daily-cache] 第 {attempt + 1} 次拉取失败，60s 后重试 "
                        f"（已 {done}/{len(pending)}）"
                    )
                    await asyncio.sleep(60)
            if result is None:
                # 网关熔断/不可用：保留已写入部分，不更新 meta（下次仍判 stale）
                logger.warning(
                    f"[daily-cache] baostock 持续不可用，本轮中止于 {done}/{len(pending)}"
                )
                aborted = True
                break

            params = [
                {
                    "symbol": code, "date": b["date"], "open": b["open"],
                    "high": b["high"], "low": b["low"], "close": b["close"],
                    "volume": b["volume"],
                }
                for code, bars in result.items() for b in bars
            ]
            if params:
                async with async_session() as db:
                    await db.execute(upsert_sql, params)
                    await db.commit()
                total_rows += len(params)
            done += len(chunk)
            if done % 500 == 0:
                logger.info(f"[daily-cache] 进度 {done}/{len(pending)}，已写入 {total_rows} 行")

        async with async_session() as db:
            # 清掉窗口外的老数据
            await db.execute(
                text("DELETE FROM daily_bar_cache WHERE date < :cutoff"), {"cutoff": start}
            )
            if not aborted:
                bar_date = (
                    await db.execute(text("SELECT MAX(date) FROM daily_bar_cache"))
                ).scalar() or ""
                await db.merge(DailyCacheMeta(
                    key="refreshed_at", value=datetime.now().isoformat(timespec="seconds")
                ))
                await db.merge(DailyCacheMeta(key="bar_date", value=bar_date))
            await db.commit()

        elapsed = (datetime.now() - started).total_seconds()
        logger.info(
            f"[daily-cache] 刷新{'中止' if aborted else '完成'}："
            f"本轮 {done}/{len(pending)} 只（总计 {len(ready) + done}/{len(codes)}），"
            f"{total_rows} 行，耗时 {elapsed:.0f}s"
        )
        return {"ok": not aborted, "scanned": done, "pending": len(pending),
                "rows": total_rows, "elapsed_s": round(elapsed, 1)}


# ---------------- 调度 ----------------

def _next_refresh_dt(now: datetime) -> datetime:
    """下一个工作日 16:35。"""
    target = datetime.combine(now.date(), _REFRESH_HM)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


async def _loop():
    # 启动时若缓存不新鲜（首次部署/停机错过收盘），立即补一轮；
    # 本轮未跑完（baostock 抖动中止）则隔 20 分钟续传，而不是干等到明天 16:35
    try:
        if not await is_fresh():
            logger.info("[daily-cache] 缓存为空或已过期，启动即触发刷新")
            result = await refresh_daily_cache()
            attempts = 0
            while not result.get("ok") and attempts < 6:
                attempts += 1
                logger.info(f"[daily-cache] 未完成，20 分钟后续传（第 {attempts} 次）")
                await asyncio.sleep(1200)
                result = await refresh_daily_cache()
        else:
            logger.info("[daily-cache] 缓存新鲜，等待每日 16:35 定时刷新")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[daily-cache] 启动刷新异常：{e}")

    while True:
        try:
            wait = (_next_refresh_dt(datetime.now()) - datetime.now()).total_seconds()
            await asyncio.sleep(max(wait, 60))
            await refresh_daily_cache()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[daily-cache] 定时刷新异常：{e}")
            await asyncio.sleep(300)


def start_daily_cache_scheduler() -> None:
    """在 FastAPI lifespan 里启动；重复调用无副作用。"""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_loop())


def stop_daily_cache_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
    _scheduler_task = None
