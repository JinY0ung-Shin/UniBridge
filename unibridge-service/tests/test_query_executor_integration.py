"""Integration tests for query_executor against a real SQLite engine."""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.query_executor import execute_query


async def _create_items_table(engine):
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))


async def _seed_items(engine, names: list[str]):
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO items (name) VALUES (:name)"),
            [{"name": name} for name in names],
        )


@pytest.mark.asyncio
async def test_select_limit_marks_truncated_with_real_sqlite_engine(engine_sqlite):
    await _create_items_table(engine_sqlite)
    await _seed_items(engine_sqlite, ["alpha", "bravo", "charlie", "delta"])

    response = await execute_query(
        engine_sqlite,
        "SELECT id, name FROM items ORDER BY id",
        limit=2,
        db_type="sqlite",
    )

    assert response.columns == ["id", "name"]
    assert response.rows == [[1, "alpha"], [2, "bravo"]]
    assert response.row_count == 2
    assert response.truncated is True


@pytest.mark.asyncio
async def test_insert_returning_exposes_columns_rows_and_row_count(engine_sqlite):
    await _create_items_table(engine_sqlite)

    response = await execute_query(
        engine_sqlite,
        "INSERT INTO items (name) VALUES (:name) RETURNING id",
        params={"name": "alpha"},
        limit=10,
        db_type="sqlite",
    )

    assert response.columns == ["id"]
    assert response.rows == [[1]]
    assert response.row_count == 1
    assert response.truncated is False


@pytest.mark.asyncio
async def test_insert_returning_applies_limit_and_marks_truncated(engine_sqlite):
    await _create_items_table(engine_sqlite)

    response = await execute_query(
        engine_sqlite,
        """
        INSERT INTO items (name)
        VALUES ('alpha'), ('bravo'), ('charlie')
        RETURNING id, name
        """,
        limit=2,
        db_type="sqlite",
    )

    assert response.columns == ["id", "name"]
    assert response.rows == [[1, "alpha"], [2, "bravo"]]
    assert response.row_count == 2
    assert response.truncated is True


@pytest.mark.asyncio
async def test_update_without_returning_reports_row_count_only(engine_sqlite):
    await _create_items_table(engine_sqlite)
    await _seed_items(engine_sqlite, ["alpha", "bravo", "charlie"])

    response = await execute_query(
        engine_sqlite,
        "UPDATE items SET name = :name WHERE id IN (1, 2)",
        params={"name": "updated"},
        limit=10,
        db_type="sqlite",
    )

    assert response.columns == []
    assert response.rows == []
    assert response.row_count == 2
    assert response.truncated is False


@pytest.mark.asyncio
async def test_multi_statement_sql_is_rejected_before_execution(engine_sqlite):
    with pytest.raises(ValueError, match="Multi-statement SQL is not allowed"):
        await execute_query(
            engine_sqlite,
            "SELECT 1; SELECT 2",
            limit=10,
            db_type="sqlite",
        )


@pytest.mark.asyncio
async def test_timeout_cancels_query_and_engine_can_be_reused():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def register_sleep(dbapi_conn, _connection_record):
        dbapi_conn.create_function("sleep_ms", 1, lambda ms: time.sleep(ms / 1000))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await execute_query(
                engine,
                "SELECT sleep_ms(200)",
                limit=10,
                timeout=0.01,
                db_type="sqlite",
            )

        response = await execute_query(
            engine,
            "SELECT 1",
            limit=10,
            timeout=1,
            db_type="sqlite",
        )
        assert response.rows == [[1]]
    finally:
        await engine.dispose()
