from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.schemas import QueryResponse
from app.services.sql_analysis import statement_type

# Re-exported for callers that import from this module
__all__ = [
    "check_multi_statement",
    "check_permission",
    "detect_statement_type",
    "execute_clickhouse_query",
    "execute_neo4j_query",
    "execute_query",
]

logger = logging.getLogger(__name__)

def check_multi_statement(sql: str) -> bool:
    """
    Detect semicolons outside of string literals and comments.

    Handles single quotes, double quotes, dollar-quoted strings (PostgreSQL),
    single-line comments (--), and block comments (/* */).
    Returns True if a semicolon is found outside of these contexts.
    """
    i = 0
    length = len(sql)
    while i < length:
        c = sql[i]

        # Single-line comment
        if c == '-' and i + 1 < length and sql[i + 1] == '-':
            i = sql.find('\n', i)
            if i == -1:
                break
            i += 1
            continue

        # Block comment
        if c == '/' and i + 1 < length and sql[i + 1] == '*':
            end = sql.find('*/', i + 2)
            if end == -1:
                break
            i = end + 2
            continue

        # Single-quoted string
        if c == "'":
            i += 1
            while i < length:
                if sql[i] == "'" :
                    if i + 1 < length and sql[i + 1] == "'":
                        i += 2  # escaped quote ''
                    else:
                        i += 1
                        break
                else:
                    i += 1
            continue

        # Double-quoted identifier
        if c == '"':
            i += 1
            while i < length:
                if sql[i] == '"':
                    if i + 1 < length and sql[i + 1] == '"':
                        i += 2  # escaped quote ""
                    else:
                        i += 1
                        break
                else:
                    i += 1
            continue

        # Dollar-quoted string (PostgreSQL)
        if c == '$':
            # Find the tag: $tag$ or $$
            tag_end = sql.find('$', i + 1)
            if tag_end != -1:
                tag = sql[i:tag_end + 1]
                # Validate tag content (only letters, digits, underscore)
                tag_body = tag[1:-1]
                if all(ch.isalnum() or ch == '_' for ch in tag_body):
                    close = sql.find(tag, tag_end + 1)
                    if close == -1:
                        break
                    i = close + len(tag)
                    continue
            i += 1
            continue

        if c == ';':
            # Allow trailing semicolons — only block if there's a statement after
            rest = sql[i + 1:].strip()
            if rest:
                return True

        i += 1
    return False


def _strip_strings_and_comments(sql: str) -> str:
    """Remove string literals and comments from SQL for safe keyword scanning."""
    result: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        c = sql[i]
        # Single-line comment
        if c == '-' and i + 1 < length and sql[i + 1] == '-':
            i = sql.find('\n', i)
            if i == -1:
                break
            i += 1
            continue
        # Block comment
        if c == '/' and i + 1 < length and sql[i + 1] == '*':
            end = sql.find('*/', i + 2)
            if end == -1:
                break
            i = end + 2
            continue
        # Single-quoted string
        if c == "'":
            i += 1
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            result.append("''")
            continue
        # Double-quoted identifier
        if c == '"':
            i += 1
            while i < length:
                if sql[i] == '"':
                    if i + 1 < length and sql[i + 1] == '"':
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            result.append('""')
            continue
        # Dollar-quoted string
        if c == '$':
            tag_end = sql.find('$', i + 1)
            if tag_end != -1:
                tag = sql[i:tag_end + 1]
                tag_body = tag[1:-1]
                if all(ch.isalnum() or ch == '_' for ch in tag_body):
                    close = sql.find(tag, tag_end + 1)
                    if close == -1:
                        break
                    i = close + len(tag)
                    result.append("''")
                    continue
            result.append(c)
            i += 1
            continue
        result.append(c)
        i += 1
    return "".join(result)


def detect_statement_type(sql: str) -> str:
    """
    Return the SQL statement type as a lowercase string.

    WITH ... SELECT is treated as 'select' unless the CTE body contains
    DML keywords (INSERT, UPDATE, DELETE), in which case the DML type is returned.
    EXEC/EXECUTE/CALL is treated as 'execute'.
    Returns 'unknown' if not detected.
    """
    return statement_type(sql)


def check_permission(
    statement_type: str,
    allow_select: bool,
    allow_insert: bool,
    allow_update: bool,
    allow_delete: bool,
) -> bool:
    """Check whether the given statement type is allowed by the permission flags."""
    if statement_type == "select" or statement_type == "explain":
        return allow_select
    elif statement_type == "insert":
        return allow_insert
    elif statement_type == "update":
        return allow_update
    elif statement_type == "delete":
        return allow_delete
    elif statement_type in ("create", "alter", "drop", "truncate"):
        # DDL requires all permissions
        return allow_select and allow_insert and allow_update and allow_delete
    elif statement_type == "execute":
        # Stored procedures can perform arbitrary operations — require all permissions
        return allow_select and allow_insert and allow_update and allow_delete
    else:
        return False


async def _execute(
    engine: AsyncEngine,
    sql: str,
    params: dict[str, Any] | None,
    limit: int,
    db_type: str,
) -> QueryResponse:
    """Core execution logic."""
    start = time.monotonic()

    statement_type = detect_statement_type(sql)
    is_select = statement_type in ("select", "explain")

    stmt = text(sql)
    if params:
        stmt = stmt.bindparams(**params)

    async with engine.connect() as conn:
        result = await conn.execute(stmt)

        if is_select:
            columns = list(result.keys())
            # Use cursor-based limiting instead of SQL wrapping
            all_rows = result.fetchmany(limit + 1) if limit else result.fetchall()

            truncated = False
            if limit and len(all_rows) > limit:
                all_rows = all_rows[:limit]
                truncated = True

            rows = [list(row) for row in all_rows]
            row_count = len(rows)
        else:
            # DML / DDL - commit and return rowcount
            await conn.commit()
            if result.returns_rows:
                # Handle RETURNING clauses
                columns = list(result.keys())
                all_rows = result.fetchmany(limit + 1) if limit else result.fetchall()
                truncated = False
                if limit and len(all_rows) > limit:
                    all_rows = all_rows[:limit]
                    truncated = True
                rows = [list(row) for row in all_rows]
                row_count = len(rows)
            else:
                columns = []
                rows = []
                row_count = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0
                truncated = False

    elapsed_ms = int((time.monotonic() - start) * 1000)

    return QueryResponse(
        columns=columns,
        rows=rows,
        row_count=row_count,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )


async def execute_query(
    engine: AsyncEngine,
    sql: str,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    timeout: int | None = None,
    db_type: str = "postgres",
) -> QueryResponse:
    """
    Execute an SQL query with timeout and row limit.

    Raises asyncio.TimeoutError on timeout.
    Raises any DB-level exceptions on failure.
    """
    effective_limit = limit or settings.DEFAULT_ROW_LIMIT
    effective_timeout = timeout or settings.DEFAULT_QUERY_TIMEOUT

    # Reject multi-statement SQL for non-admin users
    if check_multi_statement(sql):
        raise ValueError(
            "Multi-statement SQL is not allowed. Remove semicolons or contact an admin."
        )

    try:
        return await asyncio.wait_for(
            _execute(engine, sql, params, effective_limit, db_type),
            timeout=effective_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Query timed out after %ds", effective_timeout)
        raise


# ── ClickHouse execution path ────────────────────────────────────────────────


async def _execute_clickhouse(
    client: Any,
    sql: str,
    params: dict[str, Any] | None,
    limit: int,
) -> QueryResponse:
    """Core execution logic for ClickHouse via clickhouse-connect."""
    start = time.monotonic()

    statement_type = detect_statement_type(sql)
    is_select = statement_type in ("select", "explain")

    if is_select:
        ch_settings: dict[str, Any] = {}
        if limit:
            ch_settings["max_result_rows"] = limit + 1
            ch_settings["result_overflow_mode"] = "break"
        result = await asyncio.to_thread(
            client.query, sql, parameters=params or {},
            settings=ch_settings,
        )
        columns = list(result.column_names)
        all_rows = result.result_rows

        truncated = False
        if limit and len(all_rows) > limit:
            all_rows = all_rows[:limit]
            truncated = True

        rows = [list(row) for row in all_rows]
        row_count = len(rows)
    else:
        await asyncio.to_thread(client.command, sql, parameters=params or {})
        columns = []
        rows = []
        row_count = 0
        truncated = False

    elapsed_ms = int((time.monotonic() - start) * 1000)

    return QueryResponse(
        columns=columns,
        rows=rows,
        row_count=row_count,
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )


async def execute_clickhouse_query(
    client: Any,
    sql: str,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    timeout: int | None = None,
) -> QueryResponse:
    """Execute a ClickHouse query with timeout and row limit.

    Mirrors ``execute_query`` but uses a clickhouse-connect client
    instead of a SQLAlchemy engine.
    """
    effective_limit = limit or settings.DEFAULT_ROW_LIMIT
    effective_timeout = timeout or settings.DEFAULT_QUERY_TIMEOUT

    if check_multi_statement(sql):
        raise ValueError(
            "Multi-statement SQL is not allowed. Remove semicolons or contact an admin."
        )

    try:
        return await asyncio.wait_for(
            _execute_clickhouse(client, sql, params, effective_limit),
            timeout=effective_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Query timed out after %ds", effective_timeout)
        raise


# ── Neo4j execution path ─────────────────────────────────────────────────────


def _execute_neo4j_sync(
    driver: Any,
    database: str,
    query: str,
    params: dict[str, Any] | None,
    limit: int,
) -> QueryResponse:
    """Core execution logic for Neo4j via the official driver."""
    start = time.monotonic()
    with driver.session(database=database) as session:
        result = session.run(query, **(params or {}))
        columns = list(result.keys())
        rows: list[list[Any]] = []
        truncated = False
        for index, record in enumerate(result):
            if index >= limit:
                truncated = True
                break
            rows.append(list(record.values()))

    elapsed_ms = int((time.monotonic() - start) * 1000)
    return QueryResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=elapsed_ms,
    )


async def execute_neo4j_query(
    driver: Any,
    database: str,
    query: str,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    timeout: int | None = None,
) -> QueryResponse:
    """Execute a Neo4j Cypher query with timeout and row limit."""
    effective_limit = limit or settings.DEFAULT_ROW_LIMIT
    effective_timeout = timeout or settings.DEFAULT_QUERY_TIMEOUT
    return await asyncio.wait_for(
        asyncio.to_thread(
            _execute_neo4j_sync,
            driver,
            database,
            query,
            params,
            effective_limit,
        ),
        timeout=effective_timeout,
    )
