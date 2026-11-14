"""Tests for settings manager."""
from __future__ import annotations

import json
import pytest
from sqlalchemy import select

from app.models import SystemConfig
from app.services.settings_manager import SettingsManager


@pytest.fixture
def manager():
    return SettingsManager()


class TestSettingsManager:
    async def test_defaults_when_empty(self, manager):
        assert manager.rate_limit_per_minute == 60
        assert manager.max_concurrent_queries == 5
        assert manager.default_row_limit == 10000
        assert manager.blocked_sql_keywords == []

    async def test_load_from_db(self, manager, db_session):
        db_session.add(SystemConfig(key="rate_limit_per_minute", value="100"))
        db_session.add(SystemConfig(key="max_concurrent_queries", value="10"))
        db_session.add(SystemConfig(key="default_row_limit", value="500"))
        db_session.add(SystemConfig(
            key="blocked_sql_keywords",
            value=json.dumps(["VACUUM", "ANALYZE"]),
        ))
        await db_session.commit()

        await manager.load_from_db(db_session)

        assert manager.rate_limit_per_minute == 100
        assert manager.max_concurrent_queries == 10
        assert manager.default_row_limit == 500
        assert manager.blocked_sql_keywords == ["VACUUM", "ANALYZE"]

    async def test_loads_route_timeouts_from_db(self, manager, db_session):
        db_session.add(SystemConfig(key="query_route_timeout", value="45"))
        db_session.add(SystemConfig(key="gateway_route_timeout", value="90"))
        await db_session.commit()

        await manager.load_from_db(db_session)

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
        self, manager, db_session, key, attribute, caplog
    ):
        default = getattr(manager, attribute)
        db_session.add(SystemConfig(key=key, value="not-an-integer"))
        await db_session.commit()

        await manager.load_from_db(db_session)

        assert getattr(manager, attribute) == default
        assert f"Invalid {key} in DB" in caplog.text

    @pytest.mark.parametrize(
        "value",
        ["not-json", "null", '{"DROP": true}', '["DROP", 1]'],
    )
    async def test_invalid_blocked_keywords_keep_default(
        self, manager, db_session, value, caplog
    ):
        db_session.add(SystemConfig(key="blocked_sql_keywords", value=value))
        await db_session.commit()

        await manager.load_from_db(db_session)

        assert manager.blocked_sql_keywords == []
        assert "Invalid blocked_sql_keywords in DB" in caplog.text

    async def test_update_setting(self, manager, db_session):
        await manager.update(db_session, rate_limit_per_minute=200)
        assert manager.rate_limit_per_minute == 200

        result = await db_session.execute(
            select(SystemConfig).where(SystemConfig.key == "rate_limit_per_minute")
        )
        row = result.scalar_one()
        assert row.value == "200"

    async def test_update_blocked_keywords(self, manager, db_session):
        await manager.update(db_session, blocked_sql_keywords=["VACUUM"])
        assert manager.blocked_sql_keywords == ["VACUUM"]

    async def test_update_default_row_limit(self, manager, db_session):
        await manager.update(db_session, default_row_limit=250)
        assert manager.default_row_limit == 250

        result = await db_session.execute(
            select(SystemConfig).where(SystemConfig.key == "default_row_limit")
        )
        row = result.scalar_one()
        assert row.value == "250"

    async def test_partial_update(self, manager, db_session):
        original_concurrent = manager.max_concurrent_queries
        await manager.update(db_session, rate_limit_per_minute=999)
        assert manager.max_concurrent_queries == original_concurrent

    async def test_update_all_settings_and_existing_rows(self, manager, db_session):
        db_session.add(SystemConfig(key="rate_limit_per_minute", value="1"))
        db_session.add(SystemConfig(key="query_route_timeout", value="2"))
        await db_session.commit()

        await manager.update(
            db_session,
            rate_limit_per_minute=120,
            max_concurrent_queries=12,
            default_row_limit=750,
            query_route_timeout=30,
            gateway_route_timeout=60,
            blocked_sql_keywords=["DROP", "TRUNCATE"],
            audit_log_retention_days=90,
            admin_audit_log_retention_days=365,
            alert_history_retention_days=30,
        )

        result = await db_session.execute(select(SystemConfig))
        rows = {row.key: row.value for row in result.scalars().all()}
        assert rows == {
            "rate_limit_per_minute": "120",
            "max_concurrent_queries": "12",
            "default_row_limit": "750",
            "query_route_timeout": "30",
            "gateway_route_timeout": "60",
            "blocked_sql_keywords": json.dumps(["DROP", "TRUNCATE"]),
            "audit_log_retention_days": "90",
            "admin_audit_log_retention_days": "365",
            "alert_history_retention_days": "30",
        }
        assert manager.get_all() == {
            "rate_limit_per_minute": 120,
            "max_concurrent_queries": 12,
            "default_row_limit": 750,
            "query_route_timeout": 30,
            "gateway_route_timeout": 60,
            "blocked_sql_keywords": ["DROP", "TRUNCATE"],
            "audit_log_retention_days": 90,
            "admin_audit_log_retention_days": 365,
            "alert_history_retention_days": 30,
        }

    async def test_get_all(self, manager):
        result = manager.get_all()
        assert "rate_limit_per_minute" in result
        assert "max_concurrent_queries" in result
        assert "default_row_limit" in result
        assert "blocked_sql_keywords" in result
