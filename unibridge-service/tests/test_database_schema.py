from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import (
    ensure_alert_rule_channels_no_unique,
    ensure_db_connection_columns,
    set_sqlite_foreign_keys,
)
from app.models import Base, Permission, Role


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


@pytest.mark.asyncio
async def test_sqlite_foreign_keys_are_enabled_for_meta_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from sqlalchemy import event

    event.listen(engine.sync_engine, "connect", set_sqlite_foreign_keys)

    async with engine.connect() as conn:
        enabled = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()

    assert enabled == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_permission_role_fk_rejects_orphan_role():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from sqlalchemy import event

    event.listen(engine.sync_engine, "connect", set_sqlite_foreign_keys)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(Permission(role="missing", db_alias="testdb"))
        with pytest.raises(IntegrityError):
            await db.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_permission_role_fk_cascades_on_role_delete():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    from sqlalchemy import event

    event.listen(engine.sync_engine, "connect", set_sqlite_foreign_keys)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(Role(name="temporary", description="Temporary"))
        await db.flush()
        db.add(Permission(role="temporary", db_alias="testdb"))
        await db.commit()

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM roles WHERE name = 'temporary'"))
        remaining = (await conn.execute(text(
            "SELECT COUNT(*) FROM permissions WHERE role = 'temporary'"
        ))).scalar_one()

    assert remaining == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_runs_alembic_and_stamps_head_for_file_sqlite(tmp_path):
    from unittest.mock import patch

    from app.database import ALEMBIC_HEAD_REVISION, init_db

    db_path = tmp_path / "meta.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.database.engine", engine), patch("app.database.async_session", session_factory):
        await init_db()

    async with engine.connect() as conn:
        revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        alert_settings_row = (await conn.execute(text(
            "SELECT id, route_error_threshold_pct, check_interval_seconds "
            "FROM alert_settings WHERE id = 1"
        ))).fetchone()
        permission_fks = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_foreign_keys("permissions")
        )

    assert revision == ALEMBIC_HEAD_REVISION
    assert alert_settings_row == (1, 10.0, 60)
    assert any(
        fk["referred_table"] == "roles"
        and fk["constrained_columns"] == ["role"]
        and fk["referred_columns"] == ["name"]
        for fk in permission_fks
    )

    await engine.dispose()


@pytest.mark.asyncio
async def test_alert_owner_routing_schema_is_created_by_metadata():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        alert_channel_cols = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("alert_channels")}
        )
        alert_history_cols = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("alert_history")}
        )
        alert_settings_cols = await conn.run_sync(
            lambda sync_conn: {
                col["name"]: col for col in inspect(sync_conn).get_columns("alert_settings")
            }
        )
        alert_settings_checks = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_check_constraints("alert_settings")
        )

    assert {"owner_groups", "resource_owners", "alert_settings"} <= table_names
    assert "recipient_item_template" in alert_channel_cols
    assert {"resource_type", "owner_group_id"} <= alert_history_cols
    assert alert_settings_cols["route_error_threshold_pct"]["default"] is not None
    assert alert_settings_cols["check_interval_seconds"]["default"] is not None
    assert any(check["name"] == "ck_alert_settings_singleton" for check in alert_settings_checks)
    await engine.dispose()


@pytest.mark.asyncio
async def test_alert_settings_singleton_check_rejects_other_ids():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO alert_settings "
                "(id, route_error_threshold_pct, check_interval_seconds, updated_at) "
                "VALUES (2, 10.0, 60, CURRENT_TIMESTAMP)"
            ))

    await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_seeds_alert_settings_for_in_memory_sqlite():
    from unittest.mock import patch

    from app.database import init_db

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    with patch("app.database.engine", engine), patch("app.database.async_session", session_factory):
        await init_db()
        await init_db()

    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, route_error_threshold_pct, check_interval_seconds FROM alert_settings"
        ))).fetchall()

    assert rows == [(1, 10.0, 60)]
    await engine.dispose()
