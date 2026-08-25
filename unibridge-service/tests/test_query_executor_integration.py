"""Integration tests for query_executor against a real SQLite engine."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.schemas import QueryResponse
from app.services.query_executor import execute_query
from app.services.settings_manager import settings_manager


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
async def test_select_reads_through_server_side_cursor_and_stops_at_limit():
    """The SELECT path must stream, not buffer the whole result set.

    A buffered ``conn.execute()`` makes the driver materialise every row at
    execute time, so the marker function fires once per table row and `limit`
    only trims the response afterwards. With ``conn.stream()`` the marker fires
    only for the rows actually read (``limit + 1``), which is what keeps peak
    memory bounded by the limit instead of by the size of the table.
    """
    produced: list[int] = []
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def register_marker(dbapi_conn, _connection_record):
        def mark(value):
            produced.append(value)
            return value

        dbapi_conn.create_function("mark", 1, mark)

    try:
        await _create_items_table(engine)
        await _seed_items(engine, [f"item-{i:03d}" for i in range(500)])

        produced.clear()
        response = await execute_query(
            engine,
            "SELECT mark(id) AS marked_id, name FROM items ORDER BY items.id",
            limit=5,
            db_type="sqlite",
        )

        assert response.columns == ["marked_id", "name"]
        assert response.rows[0] == [1, "item-000"]
        assert len(response.rows) == 5
        assert response.row_count == 5
        assert response.truncated is True
        # Bounded by the limit (plus a little driver-level lookahead), not by
        # the 500 rows in the table. Buffering would make this 500.
        assert len(produced) <= 25, (
            f"database produced {len(produced)} rows for a limit of 5 — "
            "the result set was buffered instead of streamed"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_select_below_limit_is_not_truncated_on_streaming_path(engine_sqlite):
    await _create_items_table(engine_sqlite)
    await _seed_items(engine_sqlite, ["alpha", "bravo"])

    response = await execute_query(
        engine_sqlite,
        "SELECT id, name FROM items ORDER BY id",
        limit=10,
        db_type="sqlite",
    )

    assert response.rows == [[1, "alpha"], [2, "bravo"]]
    assert response.row_count == 2
    assert response.truncated is False


@pytest.mark.asyncio
async def test_select_exactly_at_limit_is_not_truncated(engine_sqlite):
    await _create_items_table(engine_sqlite)
    await _seed_items(engine_sqlite, ["alpha", "bravo", "charlie"])

    response = await execute_query(
        engine_sqlite,
        "SELECT id, name FROM items ORDER BY id",
        limit=3,
        db_type="sqlite",
    )

    assert response.row_count == 3
    assert response.truncated is False


@pytest.mark.asyncio
async def test_explain_uses_streaming_path(engine_sqlite):
    await _create_items_table(engine_sqlite)
    await _seed_items(engine_sqlite, ["alpha"])

    response = await execute_query(
        engine_sqlite,
        "EXPLAIN QUERY PLAN SELECT id FROM items",
        limit=10,
        db_type="sqlite",
    )

    assert response.columns
    assert response.truncated is False


@pytest.mark.asyncio
async def test_execute_query_clamps_limit_to_max_row_limit(engine_sqlite):
    with patch(
        "app.services.query_executor._execute", new_callable=AsyncMock
    ) as fake_execute:
        fake_execute.return_value = QueryResponse(
            columns=[], rows=[], row_count=0, truncated=False, elapsed_ms=0
        )
        await execute_query(
            engine_sqlite,
            "SELECT 1",
            limit=settings.MAX_ROW_LIMIT * 5,
            db_type="sqlite",
        )

    assert fake_execute.await_args.args[3] == settings.MAX_ROW_LIMIT


@pytest.mark.asyncio
async def test_execute_query_clamps_admin_default_row_limit(engine_sqlite, monkeypatch):
    monkeypatch.setattr(
        settings_manager, "default_row_limit", settings.MAX_ROW_LIMIT + 1
    )

    with patch(
        "app.services.query_executor._execute", new_callable=AsyncMock
    ) as fake_execute:
        fake_execute.return_value = QueryResponse(
            columns=[], rows=[], row_count=0, truncated=False, elapsed_ms=0
        )
        await execute_query(engine_sqlite, "SELECT 1", db_type="sqlite")

    assert fake_execute.await_args.args[3] == settings.MAX_ROW_LIMIT


@pytest.mark.asyncio
async def test_execute_query_leaves_reasonable_limit_untouched(engine_sqlite):
    with patch(
        "app.services.query_executor._execute", new_callable=AsyncMock
    ) as fake_execute:
        fake_execute.return_value = QueryResponse(
            columns=[], rows=[], row_count=0, truncated=False, elapsed_ms=0
        )
        await execute_query(engine_sqlite, "SELECT 1", limit=42, db_type="sqlite")

    assert fake_execute.await_args.args[3] == 42


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
