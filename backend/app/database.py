"""数据库连接（SQLAlchemy 2.0 async）。"""
from collections.abc import AsyncIterator

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

_settings = get_settings()

# SQLite 需要特殊参数；PG 不需要
_connect_args = {"check_same_thread": False} if _settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_async_engine(
    _settings.DATABASE_URL,
    connect_args=_connect_args,
    echo=_settings.SQL_ECHO,
)

if _settings.DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """SQLite 开 WAL，降低读写并发时的 database is locked。"""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # 写-写在 WAL 下依然互斥：给一个有限的等待窗口，而不是立刻报 locked
        cur.execute(f"PRAGMA busy_timeout={int(_settings.SQLITE_BUSY_TIMEOUT_MS)}")
        cur.close()

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入：每次请求一个 session。"""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_tables():
    """首次启动自动建表（幂等）+ SQLite 字段迁移（在事务外执行）。"""
    from .models.user import User  # noqa: F401
    from .models.portfolio import Portfolio, Holding, Trade  # noqa: F401
    from .models.chat import ChatHistory  # noqa: F401
    from .models.dataset import Dataset, DatasetSample  # noqa: F401
    from .models.alert import PriceAlert  # noqa: F401
    from .models.backtest_run import BacktestRun  # noqa: F401
    from .models.daily_bar import DailyBar, DailyCacheMeta  # noqa: F401
    from .models.finetune import FineTuneJob  # noqa: F401
    from .models.watch import WatchItem, WatchEvent  # noqa: F401

    # 1) SQLite 字段迁移 — 老库补新列（绕过 aiosqlite 直接 sync 操作）
    try:
        import os as _os, sqlite3 as _sqlite3, asyncio as _asyncio
        db_path = _settings.DATABASE_URL.replace("sqlite+aiosqlite:///", "")
        if not db_path or db_path.startswith("./"):
            db_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", db_path.lstrip("./"))
        db_path = _os.path.abspath(db_path)

        # (表, 列, 列定义) —— 已存在则跳过
        _COLUMNS = [
            ("datasets", "is_published", "BOOLEAN DEFAULT 0"),
            ("users", "investment_horizon", "VARCHAR(20)"),
            ("users", "preferred_sectors", "TEXT"),
            ("users", "avoided_sectors", "TEXT"),
            ("users", "investment_style", "TEXT"),
            ("price_alerts", "alert_type", "VARCHAR(10) DEFAULT 'price'"),
            ("price_alerts", "reference_price", "FLOAT"),
            ("price_alerts", "change_pct", "FLOAT"),
            ("chat_histories", "payload", "TEXT"),
        ]

        def _do_migrate():
            conn = _sqlite3.connect(db_path, timeout=_settings.SQLITE_BUSY_TIMEOUT_MS / 1000)
            for table, column, ddl in _COLUMNS:
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if not cols:
                    continue  # 表还不存在，交给 create_all
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    logger.info(f"SQLite 迁移: {table} 表已补 {column} 列")
            conn.commit()
            conn.close()

        await _asyncio.to_thread(_do_migrate)
    except Exception as e:
        logger.debug("SQLite 迁移跳过（新建库或表不存在）: %s", e)

    # 2) create_all 保持幂等
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("数据库表初始化完成（DATABASE_URL=%s）", _settings.DATABASE_URL)
