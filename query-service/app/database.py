import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


async def init_db() -> None:
    """Create all meta-DB tables if they don't exist, then seed default roles."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
                "gateway.consumers.read", "gateway.monitoring.read",
            ],
        },
        "viewer": {
            "description": "Read-only access to monitoring and audit logs",
            "permissions": [
                "gateway.monitoring.read", "query.audit.read",
            ],
        },
    }

    async with async_session() as db:
        from sqlalchemy import select as sa_select
        for role_name, config in SEED_ROLES.items():
            existing = await db.execute(
                sa_select(Role).where(Role.name == role_name)
            )
            if existing.scalar_one_or_none() is not None:
                continue

            role = Role(name=role_name, description=config["description"], is_system=True)
            db.add(role)
            await db.flush()

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
