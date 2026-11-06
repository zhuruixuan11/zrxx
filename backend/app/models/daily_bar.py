"""全市场日线本地缓存：选股扫描直接读 SQLite，避免每次扫 2000 只都走 baostock。

每日收盘后（约 16:35）由后台任务批量刷新；表结构刻意保持简单，
(symbol, date) 复合主键，INSERT OR REPLACE 幂等写入。
"""
from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class DailyBar(Base):
    __tablename__ = "daily_bar_cache"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True)  # sh.600000
    date: Mapped[str] = mapped_column(String(10), primary_key=True)    # YYYY-MM-DD
    open: Mapped[float] = mapped_column(Float, default=0.0)
    high: Mapped[float] = mapped_column(Float, default=0.0)
    low: Mapped[float] = mapped_column(Float, default=0.0)
    close: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)


class DailyCacheMeta(Base):
    """刷新状态（refreshed_at / bar_date），供新鲜度判断。"""
    __tablename__ = "daily_cache_meta"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(64), default="")
