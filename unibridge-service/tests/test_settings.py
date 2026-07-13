"""Tests for settings manager."""
from __future__ import annotations

import json
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, SystemConfig
from app.services.settings_manager import SettingsManager


@pytest.fixture
async def settings_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def settings_db(settings_engine):
    session_factory = async_sessionmaker(
        settings_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
def manager():
    return SettingsManager()


class TestSettingsManager:
    async def test_defaults_when_empty(self, manager):
        assert manager.rate_limit_per_minute == 60
        assert manager.max_concurrent_queries == 5
        assert manager.default_row_limit == 10000
        assert manager.blocked_sql_keywords == []

    async def test_load_from_db(self, manager, settings_db):
        settings_db.add(SystemConfig(key="rate_limit_per_minute", value="100"))
        settings_db.add(SystemConfig(key="max_concurrent_queries", value="10"))
        settings_db.add(SystemConfig(key="default_row_limit", value="500"))
        settings_db.add(SystemConfig(
            key="blocked_sql_keywords",
            value=json.dumps(["VACUUM", "ANALYZE"]),
        ))
        await settings_db.commit()

        await manager.load_from_db(settings_db)

        assert manager.rate_limit_per_minute == 100
        assert manager.max_concurrent_queries == 10
        assert manager.default_row_limit == 500
        assert manager.blocked_sql_keywords == ["VACUUM", "ANALYZE"]

    async def test_loads_route_timeouts_from_db(self, manager, settings_db):
        settings_db.add(SystemConfig(key="query_route_timeout", value="45"))
        settings_db.add(SystemConfig(key="gateway_route_timeout", value="90"))
        await settings_db.commit()

        await manager.load_from_db(settings_db)

        assert manager.query_route_timeout == 45
        assert manager.gateway_route_timeout == 90

    @pytest.mark.parametrize(
        "key,attribute",
        [
            ("rate_limit_per_minute", "rate_limit_per_minute"),
            ("max_concurrent_queries", "max_concurrent_queries"),
            ("default_row_limit", "default_row_limit"),
            ("query_route_timeout", "query_route_timeout"),
            ("gateway_route_timeout", "gateway_route_timeout"),
        ],
    )
    async def test_invalid_integer_setting_keeps_default(
        self, manager, settings_db, key, attribute, caplog
    ):
        default = getattr(manager, attribute)
        settings_db.add(SystemConfig(key=key, value="not-an-integer"))
        await settings_db.commit()

        await manager.load_from_db(settings_db)

        assert getattr(manager, attribute) == default
        assert f"Invalid {key} in DB" in caplog.text

    @pytest.mark.parametrize(
        "value",
        ["not-json", "null", '{"DROP": true}', '["DROP", 1]'],
    )
    async def test_invalid_blocked_keywords_keep_default(
        self, manager, settings_db, value, caplog
    ):
        settings_db.add(SystemConfig(key="blocked_sql_keywords", value=value))
        await settings_db.commit()

        await manager.load_from_db(settings_db)

        assert manager.blocked_sql_keywords == []
        assert "Invalid blocked_sql_keywords in DB" in caplog.text

    async def test_update_setting(self, manager, settings_db):
        await manager.update(settings_db, rate_limit_per_minute=200)
        assert manager.rate_limit_per_minute == 200

        result = await settings_db.execute(
            select(SystemConfig).where(SystemConfig.key == "rate_limit_per_minute")
        )
        row = result.scalar_one()
        assert row.value == "200"

    async def test_update_blocked_keywords(self, manager, settings_db):
        await manager.update(settings_db, blocked_sql_keywords=["VACUUM"])
        assert manager.blocked_sql_keywords == ["VACUUM"]

    async def test_update_default_row_limit(self, manager, settings_db):
        await manager.update(settings_db, default_row_limit=250)
        assert manager.default_row_limit == 250

        result = await settings_db.execute(
            select(SystemConfig).where(SystemConfig.key == "default_row_limit")
        )
        row = result.scalar_one()
        assert row.value == "250"

    async def test_partial_update(self, manager, settings_db):
        original_concurrent = manager.max_concurrent_queries
        await manager.update(settings_db, rate_limit_per_minute=999)
        assert manager.max_concurrent_queries == original_concurrent

    async def test_update_all_settings_and_existing_rows(self, manager, settings_db):
        settings_db.add(SystemConfig(key="rate_limit_per_minute", value="1"))
        settings_db.add(SystemConfig(key="query_route_timeout", value="2"))
        await settings_db.commit()

        await manager.update(
            settings_db,
            rate_limit_per_minute=120,
            max_concurrent_queries=12,
            default_row_limit=750,
            query_route_timeout=30,
            gateway_route_timeout=60,
            blocked_sql_keywords=["DROP", "TRUNCATE"],
        )

        result = await settings_db.execute(select(SystemConfig))
        rows = {row.key: row.value for row in result.scalars().all()}
        assert rows == {
            "rate_limit_per_minute": "120",
            "max_concurrent_queries": "12",
            "default_row_limit": "750",
            "query_route_timeout": "30",
            "gateway_route_timeout": "60",
            "blocked_sql_keywords": json.dumps(["DROP", "TRUNCATE"]),
        }
        assert manager.get_all() == {
            "rate_limit_per_minute": 120,
            "max_concurrent_queries": 12,
            "default_row_limit": 750,
            "query_route_timeout": 30,
            "gateway_route_timeout": 60,
            "blocked_sql_keywords": ["DROP", "TRUNCATE"],
        }

    async def test_get_all(self, manager):
        result = manager.get_all()
        assert "rate_limit_per_minute" in result
        assert "max_concurrent_queries" in result
        assert "default_row_limit" in result
        assert "blocked_sql_keywords" in result
