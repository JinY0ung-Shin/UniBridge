from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

try:  # pragma: no cover - exercised when neo4j is installed
    from neo4j import Query as Neo4jQuery
    from neo4j.graph import Node as Neo4jNode
    from neo4j.graph import Path as Neo4jPath
    from neo4j.graph import Relationship as Neo4jRelationship
except ImportError:  # pragma: no cover - test environment may omit neo4j
    class Neo4jQuery:
        def __init__(self, text: str, timeout: int | float | None = None) -> None:
            self.text = text
            self.timeout = timeout

        def __str__(self) -> str:
            return self.text

    Neo4jNode = ()
    Neo4jPath = ()
    Neo4jRelationship = ()

from app.config import settings
from app.schemas import QueryResponse
from app.services.connection_manager import run_clickhouse_locked
from app.services.sql_analysis import statement_type

# Re-exported for callers that import from this module
__all__ = [
    "check_multi_statement",
    "check_permission",
    "detect_statement_type",
    "execute_clickhouse_query",
    "execute_graphdb_query",
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
    lock: threading.Lock | None,
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
        if lock is None:
            result = await asyncio.to_thread(
                client.query, sql, parameters=params or {},
                settings=ch_settings,
            )
        else:
            result = await asyncio.to_thread(
                run_clickhouse_locked,
                lock,
                client.query,
                sql,
                parameters=params or {},
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
        if lock is None:
            await asyncio.to_thread(client.command, sql, parameters=params or {})
        else:
            await asyncio.to_thread(
                run_clickhouse_locked,
                lock,
                client.command,
                sql,
                parameters=params or {},
            )
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
    lock: threading.Lock | None = None,
) -> QueryResponse:
    """Execute a ClickHouse query with timeout and row limit.

    Mirrors ``execute_query`` but uses a clickhouse-connect client
    instead of a SQLAlchemy engine.

    ``lock`` serializes access to the underlying clickhouse-connect client
    inside the worker thread. This keeps the client protected even when the
    awaiting coroutine times out while the thread continues running.
    """
    effective_limit = limit or settings.DEFAULT_ROW_LIMIT
    effective_timeout = timeout or settings.DEFAULT_QUERY_TIMEOUT

    if check_multi_statement(sql):
        raise ValueError(
            "Multi-statement SQL is not allowed. Remove semicolons or contact an admin."
        )

    async def _run() -> QueryResponse:
        return await _execute_clickhouse(client, sql, params, effective_limit, lock)

    try:
        return await asyncio.wait_for(_run(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        logger.warning("Query timed out after %ds", effective_timeout)
        raise


# ── Neo4j execution path ─────────────────────────────────────────────────────


def _neo4j_entity_id(entity: Any) -> Any:
    element_id = getattr(entity, "element_id", None)
    if element_id is not None:
        return element_id
    return getattr(entity, "id", None)


def _convert_neo4j_mapping(mapping: Any) -> dict[str, Any]:
    return {key: _convert_neo4j_value(value) for key, value in dict(mapping).items()}


def _convert_neo4j_value(value: Any) -> Any:
    """Convert Neo4j values to JSON-serializable Python objects."""
    if value is None or isinstance(value, str | int | float | bool):
        return value

    if isinstance(value, Neo4jNode):
        return {
            "id": _neo4j_entity_id(value),
            "labels": sorted(value.labels),
            "properties": _convert_neo4j_mapping(value),
        }

    if isinstance(value, Neo4jRelationship):
        return {
            "id": _neo4j_entity_id(value),
            "type": value.type,
            "start_node_id": _neo4j_entity_id(value.start_node),
            "end_node_id": _neo4j_entity_id(value.end_node),
            "properties": _convert_neo4j_mapping(value),
        }

    if isinstance(value, Neo4jPath):
        return {
            "nodes": [_convert_neo4j_value(node) for node in value.nodes],
            "relationships": [
                _convert_neo4j_value(relationship)
                for relationship in value.relationships
            ],
        }

    if isinstance(value, dict):
        return {
            key: _convert_neo4j_value(nested_value)
            for key, nested_value in value.items()
        }

    if isinstance(value, list | tuple):
        return [_convert_neo4j_value(item) for item in value]

    if isinstance(value, set):
        return sorted(
            (_convert_neo4j_value(item) for item in value),
            key=str,
        )

    iso_format = getattr(value, "iso_format", None)
    if callable(iso_format):
        return iso_format()

    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()

    return str(value)


def _execute_neo4j_sync(
    driver: Any,
    database: str,
    query: str,
    params: dict[str, Any] | None,
    limit: int,
    timeout: int | float,
) -> QueryResponse:
    """Core execution logic for Neo4j via the official driver."""
    start = time.monotonic()
    cypher = Neo4jQuery(query, timeout=timeout)
    with driver.session(database=database) as session:
        result = session.run(cypher, parameters=params or {})
        columns = list(result.keys())
        rows: list[list[Any]] = []
        truncated = False
        for index, record in enumerate(result):
            if index >= limit:
                truncated = True
                break
            rows.append([_convert_neo4j_value(value) for value in record.values()])

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
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _execute_neo4j_sync,
                driver,
                database,
                query,
                params,
                effective_limit,
                effective_timeout,
            ),
            timeout=effective_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Query timed out after %ds", effective_timeout)
        raise


# --- GraphDB executor ---------------------------------------------------------

_XSD = "http://www.w3.org/2001/XMLSchema#"
_XSD_BOOL = {_XSD + "boolean"}
_XSD_INT = {_XSD + s for s in (
    "integer", "long", "int", "short", "byte",
    "nonNegativeInteger", "positiveInteger",
    "negativeInteger", "nonPositiveInteger",
    "unsignedLong", "unsignedInt", "unsignedShort", "unsignedByte",
)}
_XSD_FLOAT = {_XSD + s for s in ("decimal", "double", "float")}


def _coerce_binding_value(binding: dict[str, Any]) -> Any:
    """Convert a SPARQL Results JSON binding into a Python value (best-effort).

    See spec §5.1. Failures fall back to the raw string value (never None).
    """
    btype = binding.get("type")
    value = binding.get("value", "")
    if btype == "uri":
        return value
    if btype == "bnode":
        return f"_:{value}"
    # literal or typed-literal
    datatype = binding.get("datatype")
    if datatype in _XSD_BOOL:
        return value.strip().lower() == "true"
    if datatype in _XSD_INT:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if datatype in _XSD_FLOAT:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return value


async def _read_capped_response(resp: httpx.Response, max_bytes: int) -> bytes:
    """Stream resp.aiter_bytes up to max_bytes; raise 413 if exceeded.

    Caller must have already checked Content-Length if present.
    """
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail="GraphDB response exceeded GRAPHDB_MAX_RESPONSE_BYTES",
            )
    return bytes(buf)


def _truncate_preview(text: str, max_chars: int = 200) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _map_graphdb_error(status_code: int, body_preview: str, repo: str) -> HTTPException:
    if status_code in (401, 403):
        return HTTPException(status_code=502, detail="GraphDB authentication failed")
    if status_code == 404 and "repository" in body_preview.lower():
        return HTTPException(
            status_code=404, detail=f"GraphDB repository not found: {repo}"
        )
    if 400 <= status_code < 500:
        return HTTPException(
            status_code=400,
            detail=f"GraphDB rejected query: {_truncate_preview(body_preview)}",
        )
    return HTTPException(status_code=502, detail="GraphDB upstream error")


async def execute_graphdb_query(
    *,
    client: httpx.AsyncClient,
    repo: str,
    sparql: str,
    statement_type: str,
    limit: int,
) -> QueryResponse:
    """Execute a SPARQL read against GraphDB and map the response.

    URL-pinned to ``POST /repositories/{repo}`` — defense in depth against
    SPARQL Update slipping through. Streaming + size cap from
    ``settings.GRAPHDB_MAX_RESPONSE_BYTES``.
    """
    accept = (
        "text/turtle"
        if statement_type in ("construct", "describe")
        else "application/sparql-results+json"
    )
    max_bytes = settings.GRAPHDB_MAX_RESPONSE_BYTES

    start = time.monotonic()
    try:
        async with client.stream(
            "POST",
            f"/repositories/{repo}",
            content=sparql,
            headers={
                "Content-Type": "application/sparql-query",
                "Accept": accept,
            },
        ) as resp:
            content_length = resp.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="GraphDB response exceeded GRAPHDB_MAX_RESPONSE_BYTES",
                        )
                except ValueError:
                    pass  # malformed header — fall through to streaming guard

            raw = await _read_capped_response(resp, max_bytes)

            if resp.status_code >= 400:
                preview = raw.decode("utf-8", errors="replace")
                raise _map_graphdb_error(resp.status_code, preview, repo)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="GraphDB query timed out") from exc
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="GraphDB upstream error") from exc

    elapsed_ms = int((time.monotonic() - start) * 1000)

    if statement_type in ("construct", "describe"):
        try:
            turtle = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=502, detail="GraphDB returned non-UTF-8 graph payload"
            ) from exc
        return QueryResponse(
            columns=[], rows=[], row_count=0, truncated=False,
            elapsed_ms=elapsed_ms, graph=turtle,
        )

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502, detail="GraphDB returned malformed SPARQL Results JSON"
        ) from exc

    if statement_type == "ask":
        ask_value = bool(parsed.get("boolean", False))
        return QueryResponse(
            columns=["boolean"], rows=[[ask_value]], row_count=1,
            truncated=False, elapsed_ms=elapsed_ms, graph=None,
        )

    # SELECT
    head = parsed.get("head", {})
    columns = list(head.get("vars", []))
    bindings = parsed.get("results", {}).get("bindings", [])
    raw_rows = [
        [_coerce_binding_value(b[v]) if v in b else None for v in columns]
        for b in bindings
    ]
    truncated = len(raw_rows) > limit
    rows = raw_rows[:limit] if truncated else raw_rows
    return QueryResponse(
        columns=columns, rows=rows, row_count=len(rows),
        truncated=truncated, elapsed_ms=elapsed_ms, graph=None,
    )
