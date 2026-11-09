"""持仓跟随监控单测：基线初始化 / 数量变动触发 / 去重 / 未配置 SMTP 的如实标记。

用内存 SQLite + monkeypatch 会话工厂，不触网、不写真实库。
"""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.portfolio import Holding, Portfolio
from app.models.watch import WatchEvent, WatchItem
from app.services import watch


def test_holding_change_triggers_once(monkeypatch):
    # 未配置 SMTP：事件照常落库，但 email_configured 必须是 False
    monkeypatch.setattr(watch.settings, "SMTP_HOST", None)
    monkeypatch.setattr(watch.settings, "SMTP_USER", None)
    monkeypatch.setattr(watch.settings, "SMTP_PASSWORD", None)

    async def _run():
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(watch, "async_session", Session)

        # 组合里持有 600519 共 1000 股
        async with Session() as db:
            p = Portfolio(user_id="u1", name="测试组合")
            db.add(p)
            await db.flush()
            db.add(Holding(portfolio_id=p.id, symbol="600519", quantity=1000))
            db.add(WatchItem(
                user_id="u1", symbol="600519", name="贵州茅台",
                price_threshold_pct=5.0,
                watch_price=False, watch_holding=True, watch_announcement=False,
            ))
            await db.commit()

        # 第 1 轮：还没有基线，只初始化，不产生事件
        first = await watch.check_watch_items(user_id="u1")
        assert first["created"] == 0
        async with Session() as db:
            item = (await db.execute(select(WatchItem))).scalar_one()
            assert float(item.baseline_quantity) == 1000.0

        # 用户加仓到 1200 股 → 触发一条持仓变动
        async with Session() as db:
            h = (await db.execute(select(Holding))).scalar_one()
            h.quantity = 1200
            await db.commit()

        second = await watch.check_watch_items(user_id="u1")
        assert second["created"] == 1
        assert second["by_type"] == {"holding": 1}
        assert second["email_configured"] is False
        ev = second["events"][0]
        assert ev["event_type"] == "holding"
        assert "1000" in ev["title"] and "1200" in ev["title"]
        assert "增持 200 股" in ev["detail"]

        # 第 3 轮：数量没再变 → 不重复推送（去重 + 基线已更新）
        third = await watch.check_watch_items(user_id="u1")
        assert third["created"] == 0
        async with Session() as db:
            rows = (await db.execute(select(WatchEvent))).scalars().all()
            assert len(rows) == 1
            assert rows[0].notified is False

        await engine.dispose()

    asyncio.run(_run())


def test_no_watch_items_returns_zero_and_flags_email(monkeypatch):
    monkeypatch.setattr(watch.settings, "SMTP_HOST", None)
    monkeypatch.setattr(watch.settings, "SMTP_USER", None)
    monkeypatch.setattr(watch.settings, "SMTP_PASSWORD", None)

    async def _run():
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(watch, "async_session", Session)

        result = await watch.check_watch_items()
        assert result == {"checked": 0, "created": 0, "by_type": {}, "events": [],
                          "email_configured": False}
        await engine.dispose()

    asyncio.run(_run())


# ---------------- 去重键：同日不重复、跨日可再次触发 ----------------

class _FakeClock:
    """可控时钟：watch 里的 datetime.now() 指向它。"""

    current = None

    @classmethod
    def now(cls):
        return cls.current


def test_price_event_is_deduped_within_day_and_fires_again_next_day(monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(watch.settings, "SMTP_HOST", None)
    monkeypatch.setattr(watch.settings, "SMTP_USER", None)
    monkeypatch.setattr(watch.settings, "SMTP_PASSWORD", None)
    monkeypatch.setattr(watch, "is_trading_now", lambda: True)
    monkeypatch.setattr(watch, "datetime", _FakeClock)

    async def _quote(symbol):
        return {"name": "贵州茅台", "current_price": 1050.0, "previous_close": 1000.0}

    monkeypatch.setattr(watch, "get_quote", _quote)

    async def _run():
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(watch, "async_session", Session)

        async with Session() as db:
            db.add(WatchItem(
                user_id="u1", symbol="600519", name="贵州茅台",
                price_threshold_pct=3.0,
                watch_price=True, watch_holding=False, watch_announcement=False,
            ))
            await db.commit()

        # 第 1 天 10:00：+5% > 阈值 3% → 1 条
        _FakeClock.current = datetime(2026, 1, 5, 10, 0, 0)
        first = await watch.check_watch_items(user_id="u1")
        assert first["created"] == 1
        assert first["by_type"] == {"price": 1}
        assert first["events"][0]["event_type"] == "price"

        # 同一天再检查：dedup_key 不变 → 不产生第二条
        _FakeClock.current = datetime(2026, 1, 5, 14, 30, 0)
        second = await watch.check_watch_items(user_id="u1")
        assert second["created"] == 0

        # 换一天：dedup_key 里的日期变了 → 允许再次触发
        _FakeClock.current = datetime(2026, 1, 6, 10, 0, 0)
        third = await watch.check_watch_items(user_id="u1")
        assert third["created"] == 1

        async with Session() as db:
            rows = (await db.execute(select(WatchEvent))).scalars().all()
            keys = sorted(r.dedup_key for r in rows)
        # 两天各一条，同日重复没有写进去
        assert len(keys) == 2
        assert keys[0].endswith(":price:2026-01-05:up")
        assert keys[1].endswith(":price:2026-01-06:up")
        await engine.dispose()

    asyncio.run(_run())


def test_holding_event_fires_again_when_quantity_moves_again(monkeypatch):
    monkeypatch.setattr(watch.settings, "SMTP_HOST", None)
    monkeypatch.setattr(watch.settings, "SMTP_USER", None)
    monkeypatch.setattr(watch.settings, "SMTP_PASSWORD", None)

    async def _run():
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(watch, "async_session", Session)

        async with Session() as db:
            p = Portfolio(user_id="u1", name="测试组合")
            db.add(p)
            await db.flush()
            db.add(Holding(portfolio_id=p.id, symbol="600519", quantity=1000))
            db.add(WatchItem(
                user_id="u1", symbol="600519", name="贵州茅台",
                price_threshold_pct=5.0,
                watch_price=False, watch_holding=True, watch_announcement=False,
            ))
            await db.commit()

        await watch.check_watch_items(user_id="u1")  # 建基线

        async with Session() as db:
            (await db.execute(select(Holding))).scalar_one().quantity = 1200
            await db.commit()
        first = await watch.check_watch_items(user_id="u1")
        assert first["created"] == 1
        async with Session() as db:
            key1 = (await db.execute(select(WatchEvent))).scalar_one().dedup_key
        assert key1.endswith(":holding:1000->1200")

        # 数量继续变成 1300 → 是另一个事件，不能因为「同一标的」被吞掉
        async with Session() as db:
            (await db.execute(select(Holding))).scalar_one().quantity = 1300
            await db.commit()
        second = await watch.check_watch_items(user_id="u1")
        assert second["created"] == 1

        async with Session() as db:
            rows = (await db.execute(select(WatchEvent))).scalars().all()
            keys = sorted(r.dedup_key for r in rows)
            assert [r.event_type for r in rows] == ["holding", "holding"]
        assert len(keys) == 2
        assert any(k.endswith(":holding:1000->1200") for k in keys)
        assert any(k.endswith(":holding:1200->1300") for k in keys)
        await engine.dispose()

    asyncio.run(_run())

