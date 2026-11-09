"""持仓跟随监控：价格异动 / 自身持仓变动 / 新公告。

复用现有 60s 轮询（services/alerts._loop）与 SMTP 发信，不引入新调度框架。
去重：watch_events.dedup_key 唯一，同一事件只推送一次。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select

from ..agent.tools import get_quote, normalize_a_share
from ..config import get_settings
from ..database import async_session
from ..models.portfolio import Holding, Portfolio
from ..models.watch import WatchEvent, WatchItem
from .alerts import is_trading_now, send_user_email

settings = get_settings()

ANNOUNCEMENT_TTL_SECONDS = 30 * 60  # 公告源 30 分钟缓存，避免频繁打东财
_ann_cache: Dict[str, Tuple[float, List[Dict[str, str]]]] = {}
_name_cache: Dict[str, str] = {}

EVENT_LABELS = {"price": "价格异动", "holding": "持仓变动", "announcement": "新公告"}


def _bare(symbol: str) -> str:
    bs = normalize_a_share(symbol)
    return bs.split(".")[1] if bs else symbol.strip()


def email_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD)


async def _resolve_name(bare: str) -> str:
    """取股票简称：baostock 基础信息优先，失败回退东财。"""
    if bare in _name_cache:
        return _name_cache[bare]

    from .. import baostock_gateway
    from ..agent.tools import normalize_a_share as _norm

    basics = await baostock_gateway.fetch_stock_basics()
    if basics:
        info = basics.get(_norm(bare) or "")
        if info and info.get("name"):
            _name_cache[bare] = str(info["name"]).strip()
            return _name_cache[bare]

    def _fetch() -> str:
        import akshare as ak

        df = ak.stock_individual_info_em(symbol=bare)
        mapping = {str(r["item"]).strip(): r["value"] for _, r in df.iterrows()}
        return str(mapping.get("股票简称") or "").strip()

    try:
        name = await asyncio.wait_for(asyncio.to_thread(_fetch), 12)
    except Exception as e:
        logger.warning(f"[watch] 股票简称获取失败 {bare}: {e}")
        name = ""
    if name:
        _name_cache[bare] = name
    return name


# ---------------- 公告源 ----------------

def _fetch_notice_sync(date_str: str) -> List[Dict[str, str]]:
    import akshare as ak

    df = ak.stock_notice_report(symbol="全部", date=date_str.replace("-", ""))
    rows: List[Dict[str, str]] = []
    for _, r in df.iterrows():
        symbol = str(r.get("代码") or "").strip()
        title = str(r.get("公告标题") or "").strip()
        url = str(r.get("网址") or "").strip()
        if not symbol or not title:
            continue
        raw = url or f"{date_str}|{title}"
        rows.append({
            "symbol": _bare(symbol),
            "name": str(r.get("名称") or "").strip(),
            "title": title,
            "type": str(r.get("公告类型") or "").strip(),
            "date": str(r.get("公告日期") or date_str).strip(),
            "url": url,
            "event_id": hashlib.md5(raw.encode("utf-8")).hexdigest()[:20],
        })
    return rows


async def _latest_announcements() -> List[Dict[str, str]]:
    """拉最近一个交易日（含今天）的全市场公告，30 分钟缓存。"""
    now = datetime.now()
    for offset in range(0, 8):
        day = (now - timedelta(days=offset)).date()
        if day.weekday() >= 5:
            continue
        key = day.isoformat()
        hit = _ann_cache.get(key)
        if hit:
            ts, rows = hit
            if time.time() - ts < ANNOUNCEMENT_TTL_SECONDS:
                if rows:
                    return rows
                continue
        try:
            rows = await asyncio.to_thread(_fetch_notice_sync, key)
        except Exception as e:
            logger.warning(f"[watch] 公告拉取失败 {key}: {e}")
            rows = []
        _ann_cache[key] = (time.time(), rows)
        if rows:
            return rows
    return []


# ---------------- 持仓基线 ----------------

async def _holdings_map(user_ids: List[str]) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {u: {} for u in user_ids}
    if not user_ids:
        return out
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Portfolio.user_id, Holding.symbol, Holding.quantity).join(
                    Holding, Holding.portfolio_id == Portfolio.id
                ).where(Portfolio.user_id.in_(user_ids))
            )
        ).all()
    for user_id, symbol, qty in rows:
        out.setdefault(user_id, {})[_bare(symbol)] = float(qty or 0)
    return out


# ---------------- 主检查链路 ----------------

def _candidate(
    item: WatchItem, event_type: str, dedup_key: str, title: str,
    detail: str, payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    import json

    return {
        "user_id": item.user_id,
        "watch_id": item.id,
        "symbol": item.symbol,
        "event_type": event_type,
        "dedup_key": dedup_key,
        "title": title,
        "detail": detail,
        "payload_json": json.dumps(payload or {}, ensure_ascii=False),
    }


async def check_watch_items(user_id: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """跑一轮关注检查。

    user_id 为空 → 全量（后台轮询）；传入 → 只检查该用户（手动接口）。
    force=True 时价格规则不受交易时段限制（手动检查用）。
    """
    async with async_session() as db:
        stmt = select(WatchItem).where(WatchItem.enabled.is_(True))
        if user_id:
            stmt = stmt.where(WatchItem.user_id == user_id)
        items = (await db.execute(stmt)).scalars().all()

    if not items:
        return {"checked": 0, "created": 0, "by_type": {}, "events": [],
                "email_configured": email_configured()}

    user_ids = sorted({i.user_id for i in items})
    holdings = await _holdings_map(user_ids)
    now = datetime.now()
    candidates: List[Dict[str, Any]] = []
    announcements: Optional[List[Dict[str, str]]] = None
    checked = 0

    for item in items:
        bare = _bare(item.symbol)

        # 1) 价格异动
        if item.watch_price and (force or is_trading_now()):
            try:
                quote = await get_quote(item.symbol)
            except Exception as e:
                logger.warning(f"[watch] 行情获取失败 {item.symbol}: {e}")
                quote = {"error": str(e)}
            if quote and "error" not in quote:
                price = float(quote.get("current_price") or 0)
                prev = float(quote.get("previous_close") or 0)
                if price > 0:
                    checked += 1
                    item.last_price = price
                    item.last_checked_at = now
                    if not item.name and quote.get("name"):
                        item.name = quote["name"]
                    change = (price - prev) / prev * 100 if prev > 0 else float(
                        quote.get("change_percent") or 0
                    )
                    if abs(change) >= float(item.price_threshold_pct or 0):
                        day = now.strftime("%Y-%m-%d")
                        direction = "up" if change > 0 else "down"
                        candidates.append(_candidate(
                            item, "price",
                            f"{item.id}:price:{day}:{direction}",
                            f"{item.name or bare} 价格{'上涨' if change > 0 else '下跌'} {abs(change):.2f}%",
                            f"现价 {price:.2f}，昨收 {prev:.2f}，涨跌幅 {change:+.2f}%，"
                            f"阈值 {float(item.price_threshold_pct):.1f}%",
                            {"price": price, "previous_close": prev, "change_pct": round(change, 3)},
                        ))

        # 2) 自身持仓数量变动
        if item.watch_holding:
            checked += 1
            qty = float(holdings.get(item.user_id, {}).get(bare, 0.0))
            if item.baseline_quantity is None:
                item.baseline_quantity = qty
                item.baseline_at = now
            elif abs(qty - float(item.baseline_quantity)) > 1e-6:
                old = float(item.baseline_quantity)
                candidates.append(_candidate(
                    item, "holding",
                    f"{item.id}:holding:{old:g}->{qty:g}",
                    f"{item.name or bare} 持仓数量变动：{old:g} → {qty:g} 股",
                    f"你组合内 {bare} 的持仓数量由 {old:g} 股变为 {qty:g} 股"
                    f"（{'增持' if qty > old else '减持'} {abs(qty - old):g} 股）",
                    {"old_quantity": old, "new_quantity": qty},
                ))
                item.baseline_quantity = qty
                item.baseline_at = now

        # 3) 新公告（30 分钟节流）
        if item.watch_announcement:
            last = item.last_announcement_at
            if last is None or (now - last).total_seconds() >= ANNOUNCEMENT_TTL_SECONDS:
                if announcements is None:
                    announcements = await _latest_announcements()
                matched = [a for a in announcements if a["symbol"] == bare]
                # 单轮最多 3 条，避免首次关注时一次性刷屏
                for ann in matched[:3]:
                    candidates.append(_candidate(
                        item, "announcement",
                        f"{item.id}:ann:{ann['event_id']}",
                        f"{item.name or bare} 新公告：{ann['title'][:60]}",
                        f"{ann['date']} · {ann['type'] or '公告'}\n{ann['title']}\n{ann['url']}",
                        {"url": ann["url"], "date": ann["date"], "type": ann["type"]},
                    ))
                item.last_announcement_at = now

    # 去重后落库
    created: List[Dict[str, Any]] = []
    if candidates:
        keys = [c["dedup_key"] for c in candidates]
        async with async_session() as db:
            exists = set(
                (await db.execute(
                    select(WatchEvent.dedup_key).where(WatchEvent.dedup_key.in_(keys))
                )).scalars().all()
            )
            fresh = [c for c in candidates if c["dedup_key"] not in exists]
            for c in fresh:
                db.add(WatchEvent(**c))
            for item in items:
                await db.merge(item)
            await db.commit()
        created = fresh
    else:
        async with async_session() as db:
            for item in items:
                await db.merge(item)
            await db.commit()

    # 邮件在落库后发送；未配置 SMTP 时如实标记 notified=False
    if created:
        for ev in created:
            sent = await send_user_email(
                ev["user_id"],
                f"[衡策] 持仓跟随：{ev['title'][:40]}",
                (
                    "衡策 · 持仓跟随提醒\n"
                    f"标的：{ev['symbol']}\n"
                    f"类型：{EVENT_LABELS.get(ev['event_type'], ev['event_type'])}\n"
                    f"详情：{ev['detail']}\n"
                    f"时间：{now:%Y-%m-%d %H:%M:%S}\n"
                    "（本邮件由衡策 60 秒轮询触发）\n"
                ),
            )
            if sent:
                async with async_session() as db:
                    row = (await db.execute(
                        select(WatchEvent).where(WatchEvent.dedup_key == ev["dedup_key"])
                    )).scalar_one_or_none()
                    if row:
                        row.notified = True
                        await db.commit()

    by_type: Dict[str, int] = {}
    for ev in created:
        by_type[ev["event_type"]] = by_type.get(ev["event_type"], 0) + 1
    if created:
        logger.info(f"[watch] 本轮新增事件 {len(created)} 条：{by_type}")

    return {
        "checked": checked,
        "created": len(created),
        "by_type": by_type,
        "events": [{
            "symbol": e["symbol"],
            "event_type": e["event_type"],
            "title": e["title"],
            "detail": e["detail"],
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        } for e in created],
        "email_configured": email_configured(),
    }


async def create_watch_item(
    user_id: str, symbol: str, name: Optional[str], price_threshold_pct: float,
    watch_price: bool, watch_holding: bool, watch_announcement: bool,
) -> WatchItem:
    """新增关注标的，并用当前持仓数量初始化基线。"""
    bs = normalize_a_share(symbol)
    if not bs:
        raise ValueError("股票代码不合法")
    bare = bs.split(".")[1]

    if not name:
        name = await _resolve_name(bare)

    async with async_session() as db:
        exists = (await db.execute(
            select(WatchItem).where(WatchItem.user_id == user_id, WatchItem.symbol == bare)
        )).scalar_one_or_none()
        if exists:
            raise ValueError("该标的已在关注列表中")

        holdings = await _holdings_map([user_id])
        item = WatchItem(
            user_id=user_id, symbol=bare, name=name,
            price_threshold_pct=price_threshold_pct,
            watch_price=watch_price, watch_holding=watch_holding,
            watch_announcement=watch_announcement,
            baseline_quantity=float(holdings.get(user_id, {}).get(bare, 0.0)),
            baseline_at=datetime.now(),
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return item


async def list_watch_items(user_id: str) -> List[WatchItem]:
    async with async_session() as db:
        return (await db.execute(
            select(WatchItem).where(WatchItem.user_id == user_id).order_by(WatchItem.created_at.desc())
        )).scalars().all()


async def list_watch_events(user_id: str, limit: int = 50) -> List[WatchEvent]:
    async with async_session() as db:
        return (await db.execute(
            select(WatchEvent).where(WatchEvent.user_id == user_id)
            .order_by(WatchEvent.created_at.desc()).limit(limit)
        )).scalars().all()
