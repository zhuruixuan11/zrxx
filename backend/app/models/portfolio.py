"""投资组合 / 持仓 / 交易模型。"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class AssetType(str, enum.Enum):
    STOCK = "stock"
    FUND = "fund"
    BOND = "bond"
    ETF = "etf"
    CRYPTO = "crypto"


class TradeAction(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"


class TradeStatus(str, enum.Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


def _new_id() -> str:
    return uuid.uuid4().hex


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(100), default="我的投资组合")
    initial_capital: Mapped[float] = mapped_column(Numeric(20, 4), default=100_000.0)
    cash_balance: Mapped[float] = mapped_column(Numeric(20, 4), default=100_000.0)
    total_value: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0)
    total_cost: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0)
    total_pnl: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0)
    pnl_percent: Mapped[float] = mapped_column(Numeric(10, 4), default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="portfolios")
    holdings = relationship("Holding", back_populates="portfolio", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="portfolio", cascade="all, delete-orphan")


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    portfolio_id: Mapped[str] = mapped_column(String(32), ForeignKey("portfolios.id"), nullable=False, index=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    asset_type: Mapped[AssetType] = mapped_column(SAEnum(AssetType), default=AssetType.STOCK)

    quantity: Mapped[float] = mapped_column(Numeric(20, 8), default=0.0)
    avg_cost: Mapped[float] = mapped_column(Numeric(20, 8), default=0.0)
    current_price: Mapped[float] = mapped_column(Numeric(20, 8), default=0.0)
    previous_close: Mapped[float] = mapped_column(Numeric(20, 8), default=0.0)

    market_value: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0)
    cost_basis: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0)
    unrealized_pnl_percent: Mapped[float] = mapped_column(Numeric(10, 4), default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    portfolio = relationship("Portfolio", back_populates="holdings")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    portfolio_id: Mapped[str] = mapped_column(String(32), ForeignKey("portfolios.id"), nullable=False, index=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    action: Mapped[TradeAction] = mapped_column(SAEnum(TradeAction), nullable=False)
    status: Mapped[TradeStatus] = mapped_column(SAEnum(TradeStatus), default=TradeStatus.EXECUTED)

    quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)

    commission: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0)
    tax: Mapped[float] = mapped_column(Numeric(20, 4), default=0.0)
    total_amount: Mapped[float] = mapped_column(Numeric(20, 4), nullable=False)

    notes: Mapped[str] = mapped_column(String(500), nullable=True)
    trade_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    portfolio = relationship("Portfolio", back_populates="trades")
