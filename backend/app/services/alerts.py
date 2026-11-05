"""到价预警监控：后台轮询 + 触发记录 + 可选邮件推送。

设计取舍：
  - 轮询而非 WebSocket —— A 股日线级提醒（分钟级延迟足够），实现简单、无长连接负担。
  - 只在交易时段检查 —— 非交易时段行情不变，空转没意义。
  - 触发后停用（一次性）—— 避免同一条件反复轰炸。
"""
from __future__ import annotations

import asyncio
import smtplib
from datetime import datetime, time
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict, List

from loguru import logger
from sqlalchemy import select

from ..agent.tools import get_quote
from ..config import get_settings
from ..database import async_session
from ..models.alert import PriceAlert

settings = get_settings()

# A 股交易时段（含集合竞价与收盘缓冲）
_TRADING_WINDOWS = ((time(9, 15), time(11, 30)), (time(12, 55), time(15, 5)))

_monitor_task: asyncio.Task | None = None


def is_trading_now() -> bool:
    """粗略判断是否处于交易时段（周一到周五 + 时段内；不含节假日）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return any(start <= now.time() <= end for start, end in _TRADING_WINDOWS)


def _hit(price: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return price >= threshold
    if operator == "<=":
        return price <= threshold
    if operator == ">":
        return price > threshold
    if operator == "<":
        return price < threshold
    return False


def _comparable_value(alert: PriceAlert, quote: Dict[str, Any], price: float):
    """把预警类型换算成可比较的数值 + 阈值。

    price  → 现价 / 价格阈值
    change → 相对昨收涨跌幅(%) / change_pct
    cost   → 相对成本价涨跌幅(%) / change_pct（止盈止损）
    无法计算时返回 (None, None)。
    """
    alert_type = alert.alert_type or "price"
    if alert_type == "change":
        prev_close = float(quote.get("previous_close") or 0)
        if prev_close > 0:
            value = (price - prev_close) / prev_close * 100
        else:
            value = float(quote.get("change_percent") or 0)
        return value, alert.change_pct
    if alert_type == "cost":
        base = float(alert.reference_price or 0)
        if base <= 0:
            return None, None
        return (price - base) / base * 100, alert.change_pct
    return price, alert.threshold


def condition_text(alert: PriceAlert) -> str:
    """预警条件的可读文案（列表展示 + 邮件正文共用）。"""
    alert_type = alert.alert_type or "price"
    if alert_type == "change":
        return f"涨跌幅 {alert.operator} {float(alert.change_pct or 0):+.2f}%"
    if alert_type == "cost":
        return (
            f"成本 {float(alert.reference_price or 0):.2f} 涨跌幅 "
            f"{alert.operator} {float(alert.change_pct or 0):+.2f}%"
        )
    return f"价格 {alert.operator} {float(alert.threshold or 0):.2f}"


def _send_email(subject: str, body: str, receiver: str) -> bool:
    """同步发信（在线程里调用）。未配置 SMTP 时直接返回 False。"""
    if not (settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD):
        return False
    sender = settings.SMTP_FROM or settings.SMTP_USER
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = Header(sender, "utf-8")
        msg["To"] = Header(receiver, "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        port = settings.SMTP_PORT
        if port == 465:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, port, timeout=15)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, port, timeout=15)
            server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(sender, [receiver], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        logger.warning(f"[alert] 邮件发送失败（{receiver}）：{e}")
        return False


async def check_all_alerts(user_id: str | None = None) -> Dict[str, Any]:
    """跑一轮检查。返回本轮统计，供 API 手动触发时复用。

    传 user_id 时只检查该用户的预警（手动检查接口用）；
    不传时检查全部用户（后台轮询用）。
    """
    triggered: List[Dict[str, Any]] = []
    checked = 0

    async with async_session() as db:
        stmt = select(PriceAlert).where(PriceAlert.enabled.is_(True))
        if user_id:
            # 手动检查只作用于当前用户，避免触碰/泄露他人预警
            stmt = stmt.where(PriceAlert.user_id == user_id)
        rows = (await db.execute(stmt)).scalars().all()

        for alert in rows:
            quote = await get_quote(alert.symbol)
            if "error" in quote:
                continue
            price = float(quote.get("current_price") or 0)
            if price <= 0:
                continue

            checked += 1
            alert.last_price = price
            alert.last_checked_at = datetime.now()
            if not alert.name and quote.get("name"):
                alert.name = quote["name"]

            value, limit = _comparable_value(alert, quote, price)
            if value is None or limit is None:
                continue
            if not _hit(value, alert.operator, float(limit)):
                continue

            alert.enabled = False
            alert.triggered_at = datetime.now()
            alert.triggered_price = price
            triggered.append({
                "id": alert.id,
                "symbol": alert.symbol,
                "name": alert.name or alert.symbol,
                "alert_type": alert.alert_type or "price",
                "operator": alert.operator,
                "threshold": alert.threshold,
                "change_pct": alert.change_pct,
                "condition": condition_text(alert),
                "value": round(float(value), 2),
                "price": price,
                "user_id": alert.user_id,
            })

        await db.commit()

    # 邮件在事务提交后再发，避免发送失败回滚业务数据
    for t in triggered:
        body = (
            f"衡策 · 到价提醒\n"
            f"股票：{t['name']}（{t['symbol']}）\n"
            f"当前价：{t['price']:.2f}\n"
            f"触发条件：{t['condition']}\n"
            f"触发时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
        )
        async with async_session() as db:
            from ..models.user import User

            user = await db.get(User, t["user_id"])
            if user and user.email:
                await asyncio.to_thread(
                    _send_email, f"[衡策] 到价提醒：{t['name']}", body, user.email
                )

    if triggered:
        logger.info(f"[alert] 本轮触发 {len(triggered)} 条")

    return {"checked": checked, "triggered": triggered}


async def _loop():
    logger.info(
        f"[alert] 监控已启动：每 {settings.ALERT_POLL_INTERVAL_SECONDS}s 检查一次"
        f"{'（仅交易时段）' if settings.ALERT_TRADING_HOURS_ONLY else '（全天）'}"
    )
    while True:
        try:
            if not settings.ALERT_TRADING_HOURS_ONLY or is_trading_now():
                await check_all_alerts()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[alert] 轮询异常：{e}")
        await asyncio.sleep(max(settings.ALERT_POLL_INTERVAL_SECONDS, 10))


def start_monitor() -> None:
    """在 FastAPI lifespan 里启动；重复调用无副作用。"""
    global _monitor_task
    if not settings.ALERT_ENABLED:
        return
    if _monitor_task and not _monitor_task.done():
        return
    _monitor_task = asyncio.create_task(_loop())


def stop_monitor() -> None:
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
    _monitor_task = None
