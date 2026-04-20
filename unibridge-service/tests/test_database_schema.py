from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import (
    ensure_alert_rule_channels_no_unique,
    ensure_db_connection_columns,
)


@pytest.mark.asyncio
async def test_ensure_db_connection_columns_adds_clickhouse_fields():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.execute(text(
            """
            CREATE TABLE db_connections (
                id INTEGER PRIMARY KEY,
                alias VARCHAR NOT NULL,
                db_type VARCHAR NOT NULL,
                host VARCHAR NOT NULL,
                port INTEGER NOT NULL,
                database VARCHAR NOT NULL,
                username VARCHAR NOT NULL,
                password_encrypted VARCHAR NOT NULL,
                pool_size INTEGER,
                max_overflow INTEGER,
                query_timeout INTEGER,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        ))

    await ensure_db_connection_columns(engine)

    async with engine.begin() as conn:
        column_names = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("db_connections")}
        )

    assert {"protocol", "secure"}.issubset(column_names)

    await engine.dispose()


def _legacy_alert_rule_channels_ddl() -> str:
    return """
        CREATE TABLE alert_rule_channels (
            id INTEGER NOT NULL,
            rule_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            recipients TEXT NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_rule_channel UNIQUE (rule_id, channel_id)
        )
    """


@pytest.mark.asyncio
async def test_ensure_alert_rule_channels_no_unique_drops_legacy_constraint():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.execute(text(_legacy_alert_rule_channels_ddl()))
        await conn.execute(text(
            "INSERT INTO alert_rule_channels (id, rule_id, channel_id, recipients) "
            "VALUES (1, 10, 20, '[\"a@x.com\"]')"
        ))

    await ensure_alert_rule_channels_no_unique(engine)

    async with engine.begin() as conn:
        ddl_row = (await conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_rule_channels'"
        ))).fetchone()
        assert ddl_row is not None
        assert "uq_rule_channel" not in (ddl_row[0] or "")

        # Data preserved
        data_row = (await conn.execute(text(
            "SELECT rule_id, channel_id, recipients FROM alert_rule_channels WHERE id=1"
        ))).fetchone()
        assert data_row == (10, 20, '["a@x.com"]')

        # Duplicate (rule_id, channel_id) now allowed
        await conn.execute(text(
            "INSERT INTO alert_rule_channels (id, rule_id, channel_id, recipients) "
            "VALUES (2, 10, 20, '[\"b@x.com\"]')"
        ))
        count = (await conn.execute(text(
            "SELECT COUNT(*) FROM alert_rule_channels WHERE rule_id=10 AND channel_id=20"
        ))).scalar_one()
        assert count == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_alert_rule_channels_no_unique_is_idempotent():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE alert_rule_channels (
                id INTEGER NOT NULL,
                rule_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                recipients TEXT NOT NULL,
                PRIMARY KEY (id)
            )
        """))

    await ensure_alert_rule_channels_no_unique(engine)
    await ensure_alert_rule_channels_no_unique(engine)

    async with engine.begin() as conn:
        ddl_row = (await conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_rule_channels'"
        ))).fetchone()
        assert "uq_rule_channel" not in (ddl_row[0] or "")

    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_alert_rule_channels_no_unique_noop_when_table_absent():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # Should not raise
    await ensure_alert_rule_channels_no_unique(engine)

    await engine.dispose()
