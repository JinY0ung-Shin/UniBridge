from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import (
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

    assert {"resource_owners", "alert_settings"} <= table_names
    # Owner-group / rule tables are gone in the simplified model.
    assert "owner_groups" not in table_names
    assert "alert_rules" not in table_names
    assert "alert_rule_channels" not in table_names
    assert "recipient_item_template" in alert_channel_cols
    # AlertHistory keeps resource_type but drops rule_id / owner_group_id.
    assert "resource_type" in alert_history_cols
    assert "rule_id" not in alert_history_cols
    assert "owner_group_id" not in alert_history_cols
    # AlertSettings now stores admin_emails directly, no fallback group FK.
    assert "admin_emails" in alert_settings_cols
    assert "fallback_owner_group_id" not in alert_settings_cols
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
