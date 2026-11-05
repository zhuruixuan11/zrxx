"""到价预警模型。"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _new_id() -> str:
    return uuid.uuid4().hex


class PriceAlert(Base):
    """用户设定的单标的到价提醒。一次性：触发后 enabled=False，保留记录供追溯。"""
    __tablename__ = "price_alerts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 预警类型：price 绝对价格 / change 相对昨收涨跌幅 / cost 相对成本价涨跌幅（止盈止损）
    alert_type: Mapped[str] = mapped_column(String(10), nullable=False, default="price")
    # >= / <= / > / <
    operator: Mapped[str] = mapped_column(String(2), nullable=False, default=">=")
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # cost 类型需要成本价基准
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # change / cost 类型的涨跌幅阈值（带符号，%）
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # 监控最新状态：每轮轮询回写，供前端展示现价
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    triggered_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 预警时间是给用户看的，用本地时间（其余表沿用 utcnow 的入库约定）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
