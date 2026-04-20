import os
from collections.abc import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

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

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


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
    """Create all meta-DB tables if they don't exist, then seed default roles."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_db_connection_columns()
    await ensure_alert_rule_channels_no_unique()
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
