import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

ALEMBIC_BASELINE_REVISION = "0001_initial"
ALEMBIC_HEAD_REVISION = "0003_s3_private_endpoint_opt_in"
_SERVICE_ROOT = Path(__file__).resolve().parents[1]

# Ensure the data directory exists for SQLite
if settings.META_DB_URL.startswith("sqlite"):
    db_path = settings.META_DB_URL.split("///", 1)[1] if "///" in settings.META_DB_URL else ""
    if db_path:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

engine = create_async_engine(
    settings.META_DB_URL,
    echo=False,
    # SQLite does not support pool_size / max_overflow
    **({} if "sqlite" in settings.META_DB_URL else {"pool_size": 5, "max_overflow": 3}),
)


def set_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: object) -> None:
    """Enable SQLite FK enforcement for every new DB-API connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _ensure_sqlite_foreign_key_listener(target_engine: AsyncEngine) -> None:
    if not target_engine.url.drivername.startswith("sqlite"):
        return
    if not event.contains(target_engine.sync_engine, "connect", set_sqlite_foreign_keys):
        event.listen(target_engine.sync_engine, "connect", set_sqlite_foreign_keys)


_ensure_sqlite_foreign_key_listener(engine)


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_SERVICE_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_SERVICE_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _engine_database_url(target_engine: AsyncEngine) -> str:
    return target_engine.url.render_as_string(hide_password=False)


def _is_in_memory_sqlite(target_engine: AsyncEngine) -> bool:
    return (
        target_engine.url.drivername.startswith("sqlite")
        and target_engine.url.database in {None, "", ":memory:"}
    )


async def _table_names(target_engine: AsyncEngine) -> set[str]:
    async with target_engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))


async def _stamp_metadata_schema(target_engine: AsyncEngine, revision: str) -> None:
    async with target_engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
            ")"
        ))
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )


async def _run_alembic_upgrade(target_engine: AsyncEngine) -> None:
    _ensure_sqlite_foreign_key_listener(target_engine)

    if _is_in_memory_sqlite(target_engine):
        async with target_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _stamp_metadata_schema(target_engine, ALEMBIC_HEAD_REVISION)
        return

    tables = await _table_names(target_engine)
    has_existing_schema = bool(tables - {"alembic_version"})
    has_alembic_version = "alembic_version" in tables
    database_url = _engine_database_url(target_engine)
    config = _alembic_config(database_url)

    if has_existing_schema and not has_alembic_version:
        await ensure_db_connection_columns(target_engine)
        await ensure_alert_rule_channels_no_unique(target_engine)
        await asyncio.to_thread(command.stamp, config, ALEMBIC_BASELINE_REVISION)

    await asyncio.to_thread(command.upgrade, config, "head")


async def ensure_db_connection_columns(target_engine: AsyncEngine | None = None) -> None:
    engine_to_use = target_engine or engine

    async with engine_to_use.begin() as conn:
        existing_tables = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        if "db_connections" not in existing_tables:
            return

        existing_columns = await conn.run_sync(
            lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("db_connections")}
        )

        statements: list[str] = []
        if "protocol" not in existing_columns:
            statements.append("ALTER TABLE db_connections ADD COLUMN protocol VARCHAR(16)")
        if "secure" not in existing_columns:
            secure_type = "BIT" if conn.dialect.name == "mssql" else "BOOLEAN"
            statements.append(f"ALTER TABLE db_connections ADD COLUMN secure {secure_type}")

        for statement in statements:
            await conn.execute(text(statement))


async def ensure_alert_rule_channels_no_unique(target_engine: AsyncEngine | None = None) -> None:
    """Drop the legacy uq_rule_channel UNIQUE constraint on alert_rule_channels.

    Idempotent: no-op if the table does not exist or the constraint is already gone.
    """
    engine_to_use = target_engine or engine

    async with engine_to_use.begin() as conn:
        existing_tables = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )
        if "alert_rule_channels" not in existing_tables:
            return

        dialect = conn.dialect.name

        if dialect == "sqlite":
            result = await conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_rule_channels'"
            ))
            row = result.fetchone()
            if not row or "uq_rule_channel" not in (row[0] or ""):
                return
            await conn.execute(text(
                "ALTER TABLE alert_rule_channels RENAME TO _alert_rule_channels_old"
            ))
            await conn.execute(text("""
                CREATE TABLE alert_rule_channels (
                    id INTEGER NOT NULL,
                    rule_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    recipients TEXT NOT NULL,
                    PRIMARY KEY (id),
                    FOREIGN KEY(rule_id) REFERENCES alert_rules (id) ON DELETE CASCADE,
                    FOREIGN KEY(channel_id) REFERENCES alert_channels (id) ON DELETE CASCADE
                )
            """))
            await conn.execute(text(
                "INSERT INTO alert_rule_channels (id, rule_id, channel_id, recipients) "
                "SELECT id, rule_id, channel_id, recipients FROM _alert_rule_channels_old"
            ))
            await conn.execute(text("DROP TABLE _alert_rule_channels_old"))
        elif dialect == "postgresql":
            await conn.execute(text(
                "ALTER TABLE alert_rule_channels DROP CONSTRAINT IF EXISTS uq_rule_channel"
            ))
        elif dialect == "mssql":
            result = await conn.execute(text(
                "SELECT name FROM sys.key_constraints "
                "WHERE name = 'uq_rule_channel' AND parent_object_id = OBJECT_ID('alert_rule_channels')"
            ))
            if result.fetchone() is not None:
                await conn.execute(text(
                    "ALTER TABLE alert_rule_channels DROP CONSTRAINT uq_rule_channel"
                ))


async def init_db() -> None:
    """Apply meta-DB migrations, then seed default roles."""
    await _run_alembic_upgrade(engine)
    await _seed_roles()


async def _seed_roles() -> None:
    """Create default system roles if they don't exist."""
    from app.auth import ALL_PERMISSIONS
    from app.models import Role, RolePermission

    SEED_ROLES = {
        "admin": {
            "description": "Full access to all features",
            "permissions": ALL_PERMISSIONS,
        },
        "developer": {
            "description": "Read access to queries and gateway, can execute queries",
            "permissions": [
                "query.databases.read", "query.permissions.read", "query.audit.read",
                "query.execute",
                "gateway.routes.read", "gateway.upstreams.read",
                "gateway.monitoring.read",
                "apikeys.read",
                "alerts.read",
                "s3.connections.read", "s3.browse",
            ],
        },
        "viewer": {
            "description": "Read-only access to monitoring and audit logs",
            "permissions": [
                "gateway.monitoring.read", "query.audit.read",
                "alerts.read",
            ],
        },
    }

    async with async_session() as db:
        from sqlalchemy import delete as sa_delete, select as sa_select
        for role_name, config in SEED_ROLES.items():
            result = await db.execute(
                sa_select(Role).where(Role.name == role_name)
            )
            role = result.scalar_one_or_none()

            if role is None:
                role = Role(name=role_name, description=config["description"], is_system=True)
                db.add(role)
                await db.flush()
            else:
                # Update description and sync permissions for existing system roles
                role.description = config["description"]
                await db.execute(
                    sa_delete(RolePermission).where(RolePermission.role_id == role.id)
                )

            for perm in config["permissions"]:
                db.add(RolePermission(role_id=role.id, permission=perm))

            await db.commit()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
