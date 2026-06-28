import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

ALEMBIC_BASELINE_REVISION = "0001_initial"
_SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _current_alembic_head() -> str:
    config = Config(str(_SERVICE_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_SERVICE_ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("Alembic has no current head revision")
    return head


ALEMBIC_HEAD_REVISION = _current_alembic_head()

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


async def _seed_alert_settings_singleton(target_engine: AsyncEngine) -> None:
    async with target_engine.begin() as conn:
        await conn.execute(text(
            "INSERT OR IGNORE INTO alert_settings "
            "(id, route_error_threshold_pct, check_interval_seconds, updated_at) "
            "VALUES (1, 10.0, 60, CURRENT_TIMESTAMP)"
        ))


# Postgres advisory-lock key serializing the boot-time stamp+upgrade. Under
# blue/green both colors run init_db() against the SAME shared database; if they
# boot near-simultaneously (e.g. manual `docker compose up` of both colors, or a
# host reboot bringing both back at once) they would otherwise race to create
# and stamp the alembic_version table. A blocking pg_advisory_lock makes the
# second booter wait, then find head already applied (a no-op upgrade).
_MIGRATION_LOCK_KEY = 0x554E494247524D  # "UNIBGRM"


async def _run_alembic_upgrade(target_engine: AsyncEngine) -> None:
    _ensure_sqlite_foreign_key_listener(target_engine)

    if _is_in_memory_sqlite(target_engine):
        async with target_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed_alert_settings_singleton(target_engine)
        await _stamp_metadata_schema(target_engine, ALEMBIC_HEAD_REVISION)
        return

    if target_engine.dialect.name == "postgresql":
        # Serialize concurrent boots against the shared meta DB (see note above).
        async with target_engine.connect() as lock_conn:
            await lock_conn.execute(
                text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY}
            )
            try:
                await _do_alembic_upgrade(target_engine)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY}
                )
        return

    await _do_alembic_upgrade(target_engine)


async def _do_alembic_upgrade(target_engine: AsyncEngine) -> None:
    tables = await _table_names(target_engine)
    has_existing_schema = bool(tables - {"alembic_version"})
    has_alembic_version = "alembic_version" in tables
    database_url = _engine_database_url(target_engine)
    config = _alembic_config(database_url)

    if has_existing_schema and not has_alembic_version:
        await ensure_db_connection_columns(target_engine)
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
        "user": {
            "description": "Own gateway monitoring + self-service API key",
            "permissions": [
                "gateway.monitoring.self",
                "apikeys.self",
            ],
        },
    }

    async with async_session() as db:
        from sqlalchemy import delete as sa_delete, select as sa_select

        # Prune obsolete system roles (e.g. developer/viewer) no longer seeded.
        # Cascades via FK ondelete=CASCADE: role_permissions.role_id AND
        # permissions.role (per-DB grants) both drop their dependent rows.
        await db.execute(
            sa_delete(Role).where(
                Role.is_system.is_(True),
                Role.name.notin_(list(SEED_ROLES.keys())),
            )
        )
        await db.commit()

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
