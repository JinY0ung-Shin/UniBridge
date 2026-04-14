from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import ensure_db_connection_columns


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
