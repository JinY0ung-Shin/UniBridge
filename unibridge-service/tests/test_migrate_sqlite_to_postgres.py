from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import ALEMBIC_HEAD_REVISION
from scripts.migrate_sqlite_to_postgres import _ensure_source_at_head


async def _sqlite_source(revision: str | None = ALEMBIC_HEAD_REVISION):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE db_connections (id INTEGER PRIMARY KEY)"))
        if revision is not None:
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL)")
            )
            await conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": revision},
            )
    return engine


@pytest.mark.asyncio
async def test_ensure_source_at_head_accepts_current_revision():
    engine = await _sqlite_source()
    try:
        await _ensure_source_at_head(engine, {"db_connections", "alembic_version"})
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_source_at_head_rejects_stale_revision():
    engine = await _sqlite_source("0001_initial")
    try:
        with pytest.raises(SystemExit, match="not at Alembic head"):
            await _ensure_source_at_head(engine, {"db_connections", "alembic_version"})
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_source_at_head_requires_alembic_version_table():
    engine = await _sqlite_source(None)
    try:
        with pytest.raises(SystemExit, match="no alembic_version table"):
            await _ensure_source_at_head(engine, {"db_connections"})
    finally:
        await engine.dispose()
