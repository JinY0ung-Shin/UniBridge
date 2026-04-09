from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.schemas import QueryResponse

logger = logging.getLogger(__name__)

# Pattern to detect the primary SQL statement type
_SQL_TYPE_RE = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH|EXPLAIN|CREATE|ALTER|DROP|TRUNCATE|EXEC|EXECUTE|CALL)\b",
    re.IGNORECASE,
)


def check_multi_statement(sql: str) -> bool:
    """
    Detect semicolons outside of single-quoted string literals.

    Uses a simple state machine to track whether we are inside a string.
    Returns True if a semicolon is found outside of quotes.
    """
    in_single_quote = False
    for char in sql:
        if char == "'" :
            in_single_quote = not in_single_quote
        elif char == ";" and not in_single_quote:
            return True
    return False


# DML keywords to scan for inside WITH CTE bodies
_CTE_DML_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE)\b",
    re.IGNORECASE,
)


def detect_statement_type(sql: str) -> str:
    """
    Return the SQL statement type as a lowercase string.

    WITH ... SELECT is treated as 'select' unless the CTE body contains
    DML keywords (INSERT, UPDATE, DELETE), in which case the DML type is returned.
    EXEC/EXECUTE/CALL is treated as 'execute'.
    Returns 'unknown' if not detected.
    """
    match = _SQL_TYPE_RE.match(sql)
    if not match:
        return "unknown"
    keyword = match.group(1).upper()
    if keyword == "WITH":
        # Scan for DML keywords in the full SQL text
        dml_match = _CTE_DML_RE.search(sql)
        if dml_match:
            return dml_match.group(1).lower()
        return "select"
    if keyword in ("EXEC", "EXECUTE", "CALL"):
        return "execute"
    return keyword.lower()


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
