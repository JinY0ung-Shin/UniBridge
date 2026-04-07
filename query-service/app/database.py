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
    """Create all meta-DB tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
