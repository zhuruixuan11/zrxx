"""持仓跟随监控模型：关注标的 + 事件流水（去重）。"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _new_id() -> str:
    return uuid.uuid4().hex


class WatchItem(Base):
    """用户关注标的：价格异动 / 自身持仓变动 / 新公告三类监控。"""
    __tablename__ = "watch_items"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_watch_user_symbol"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 价格异动阈值：|相对昨收涨跌幅| ≥ 阈值（%）
    price_threshold_pct: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    watch_price: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    watch_holding: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    watch_announcement: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 持仓数量基线：用户组合内该标的的当前数量，变化即触发
    baseline_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_announcement_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class WatchEvent(Base):
    """关注事件流水。dedup_key 唯一，保证同一事件只推送一次。"""
    __tablename__ = "watch_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    watch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("watch_items.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    # price / holding / announcement
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
