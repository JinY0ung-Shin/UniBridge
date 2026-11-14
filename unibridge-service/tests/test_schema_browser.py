"""Table listing and description (app/services/schema_browser.py).

Behind the admin table list, the query routes' catalog endpoints and the MCP
list_tables / describe_table tools.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import schema_browser


class _Connection:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict | None]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return SimpleNamespace(fetchall=lambda: list(self.rows))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _manager(db_type: str, *, rows: list[tuple] | None = None, ch_rows: list[tuple] | None = None):
    manager = MagicMock()
    manager.get_db_type.return_value = db_type
    connection = _Connection(rows or [])
    manager.get_engine.return_value = SimpleNamespace(connect=lambda: connection)
    client = MagicMock()
    client.query.return_value = SimpleNamespace(result_rows=list(ch_rows or []))
    manager.get_clickhouse_client.return_value = client
    manager.get_clickhouse_lock.return_value = threading.Lock()
    return manager, connection, client


@pytest.mark.parametrize("db_type", ["neo4j", "graphdb"])
async def test_graph_databases_have_no_tables(db_type):
    manager, _, _ = _manager(db_type)
    with patch.object(schema_browser, "connection_manager", manager):
        assert await schema_browser.list_tables("kg") == []
        with pytest.raises(LookupError, match="has no tables"):
            await schema_browser.describe_table("kg", "anything")


async def test_clickhouse_columns_mark_nullable_types():
    manager, _, client = _manager(
        "clickhouse", ch_rows=[("id", "UInt64"), ("note", "Nullable(String)")]
    )
    with patch.object(schema_browser, "connection_manager", manager):
        columns = await schema_browser.describe_table("ch", "events")

    assert columns == [
        {"name": "id", "type": "UInt64", "nullable": False},
        {"name": "note", "type": "Nullable(String)", "nullable": True},
    ]
    # The table name is a bound parameter, never part of the SQL text.
    assert client.query.call_args.kwargs == {"parameters": {"table": "events"}}
    assert "events" not in client.query.call_args.args[0]


@pytest.mark.parametrize(
    "db_type,table,schema,name",
    [
        ("postgres", "users", "public", "users"),
        ("mssql", "orders", "dbo", "orders"),
        ("mssql", "sales.orders", "sales", "orders"),
    ],
)
async def test_relational_columns_default_the_schema(db_type, table, schema, name):
    manager, connection, _ = _manager(
        db_type, rows=[("id", "integer", "NO"), ("email", "text", "YES")]
    )
    with patch.object(schema_browser, "connection_manager", manager):
        columns = await schema_browser.describe_table("db", table)

    assert columns == [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "email", "type": "text", "nullable": True},
    ]
    (_sql, params), = connection.calls
    assert params == {"schema": schema, "table": name}


async def test_a_table_without_columns_is_not_found():
    manager, _, _ = _manager("postgres", rows=[])
    with patch.object(schema_browser, "connection_manager", manager):
        with pytest.raises(LookupError, match="Table 'ghost' not found in 'db'"):
            await schema_browser.describe_table("db", "ghost")
