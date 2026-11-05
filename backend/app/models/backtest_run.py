"""回测历史模型：保存一次回测的指标与权益曲线，供回看与多策略对比。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _new_id() -> str:
    return uuid.uuid4().hex


class BacktestRun(Base):
    """一次回测的快照。JSON 字段用 Text 存，避免 SQLite/PostgreSQL 方言差异。"""
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    strategy: Mapped[str] = mapped_column(String(40), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(60), nullable=False, default="")

    params_json: Mapped[str] = mapped_column(Text, default="{}")
    initial_capital: Mapped[float] = mapped_column(Float, default=0.0)
    days: Mapped[int] = mapped_column(Integer, default=0)

    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    equity_curve_json: Mapped[str] = mapped_column(Text, default="[]")

    # 回测记录展示用本地时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
