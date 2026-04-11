"""Tests for settings manager."""
from __future__ import annotations

import json
import pytest
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
        assert manager.blocked_sql_keywords == []

    async def test_load_from_db(self, manager, settings_db):
        settings_db.add(SystemConfig(key="rate_limit_per_minute", value="100"))
        settings_db.add(SystemConfig(key="max_concurrent_queries", value="10"))
        settings_db.add(SystemConfig(
            key="blocked_sql_keywords",
            value=json.dumps(["VACUUM", "ANALYZE"]),
        ))
        await settings_db.commit()

        await manager.load_from_db(settings_db)

        assert manager.rate_limit_per_minute == 100
        assert manager.max_concurrent_queries == 10
        assert manager.blocked_sql_keywords == ["VACUUM", "ANALYZE"]

    async def test_update_setting(self, manager, settings_db):
        await manager.update(settings_db, rate_limit_per_minute=200)
        assert manager.rate_limit_per_minute == 200

        from sqlalchemy import select
        result = await settings_db.execute(
            select(SystemConfig).where(SystemConfig.key == "rate_limit_per_minute")
        )
        row = result.scalar_one()
        assert row.value == "200"

    async def test_update_blocked_keywords(self, manager, settings_db):
        await manager.update(settings_db, blocked_sql_keywords=["VACUUM"])
        assert manager.blocked_sql_keywords == ["VACUUM"]

    async def test_partial_update(self, manager, settings_db):
        original_concurrent = manager.max_concurrent_queries
        await manager.update(settings_db, rate_limit_per_minute=999)
        assert manager.max_concurrent_queries == original_concurrent

    async def test_get_all(self, manager):
        result = manager.get_all()
        assert "rate_limit_per_minute" in result
        assert "max_concurrent_queries" in result
        assert "blocked_sql_keywords" in result
