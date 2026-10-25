"""Read-only schema introspection for the MCP ``list_tables`` / ``describe_table`` tools.

Table listing uses the same catalog queries as the admin endpoint
(``app/routers/admin.py::list_tables``). Graph backends (neo4j, graphdb) have
no tables: listing returns nothing and describing raises ``LookupError``.
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text

from app.services.connection_manager import connection_manager, run_clickhouse_locked

GRAPH_DB_TYPES = frozenset({"neo4j", "graphdb"})

_PG_TABLES_SQL = (
    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY tablename"
)
_MSSQL_TABLES_SQL = (
    "SELECT TABLE_SCHEMA + '.' + TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
    "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME"
)
_CH_TABLES_SQL = (
    "SELECT name FROM system.tables WHERE database = currentDatabase() ORDER BY name"
)
_INFORMATION_SCHEMA_COLUMNS_SQL = (
    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table ORDER BY ORDINAL_POSITION"
)
_CH_COLUMNS_SQL = (
    "SELECT name, type FROM system.columns "
    "WHERE database = currentDatabase() AND table = {table:String} ORDER BY position"
)
_DEFAULT_SCHEMA = {"postgres": "public", "mssql": "dbo"}


async def list_tables(alias: str) -> list[str]:
    """Table names of ``alias`` (``schema.table`` on MSSQL, bare elsewhere)."""
    db_type = connection_manager.get_db_type(alias)
    if db_type in GRAPH_DB_TYPES:
        return []
    if db_type == "clickhouse":
        client = connection_manager.get_clickhouse_client(alias)
        ch_lock = connection_manager.get_clickhouse_lock(alias)
        result = await asyncio.to_thread(
            run_clickhouse_locked, ch_lock, client.query, _CH_TABLES_SQL
        )
        return [row[0] for row in result.result_rows]

    engine = connection_manager.get_engine(alias)
    sql = _MSSQL_TABLES_SQL if db_type == "mssql" else _PG_TABLES_SQL
    async with engine.connect() as conn:
        result = await conn.execute(text(sql))
        return [row[0] for row in result.fetchall()]


async def describe_table(alias: str, table: str) -> list[dict[str, Any]]:
    """Columns of ``table`` in order; ``LookupError`` when it has none.

    ``table`` may be ``schema.table`` on Postgres/MSSQL (default schema
    ``public`` / ``dbo``). Names are bound as parameters, never interpolated.
    """
    db_type = connection_manager.get_db_type(alias)
    if db_type in GRAPH_DB_TYPES:
        raise LookupError(f"'{alias}' is a {db_type} database and has no tables")

    if db_type == "clickhouse":
        client = connection_manager.get_clickhouse_client(alias)
        ch_lock = connection_manager.get_clickhouse_lock(alias)
        result = await asyncio.to_thread(
            run_clickhouse_locked,
            ch_lock,
            client.query,
            _CH_COLUMNS_SQL,
            parameters={"table": table},
        )
        columns = [
            {"name": name, "type": col_type, "nullable": col_type.startswith("Nullable(")}
            for name, col_type in result.result_rows
        ]
    else:
        schema, _, name = table.rpartition(".")
        engine = connection_manager.get_engine(alias)
        async with engine.connect() as conn:
            result = await conn.execute(
                text(_INFORMATION_SCHEMA_COLUMNS_SQL),
                {"schema": schema or _DEFAULT_SCHEMA.get(db_type, "public"), "table": name},
            )
            columns = [
                {"name": col_name, "type": data_type, "nullable": is_nullable == "YES"}
                for col_name, data_type, is_nullable in result.fetchall()
            ]

    if not columns:
        raise LookupError(f"Table '{table}' not found in '{alias}'")
    return columns
