from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import itertools
import json
import logging
import re
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

from clickhouse_connect.driver.exceptions import DatabaseError as ClickHouseDatabaseError
import httpx
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncEngine
from urllib3.exceptions import ProtocolError

from neo4j import READ_ACCESS as NEO4J_READ_ACCESS
from neo4j import WRITE_ACCESS as NEO4J_WRITE_ACCESS
from neo4j import Query as Neo4jQuery
from neo4j.graph import Node as Neo4jNode
from neo4j.graph import Path as Neo4jPath
from neo4j.graph import Relationship as Neo4jRelationship

from app.config import settings
from app.schemas import QueryResponse
from app.services.settings_manager import settings_manager
from app.services.connection_manager import run_clickhouse_locked
from app.services.graphdb_utils import (
    GraphDBResponseTooLarge,
    graphdb_repository_path,
    read_capped_response,
)
from app.services.sql_analysis import statement_type

# Re-exported for callers that import from this module
__all__ = [
    "RenderedQueryResult",
    "check_multi_statement",
    "clickhouse_can_render_json",
    "check_permission",
    "detect_statement_type",
    "execute_clickhouse_json",
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


# Rough memory cost of one result value: a Python object plus the references
# to it in the row lists, the response model and the serialized JSON. Measured
# against FastAPI's full serialization path, the peak stays within about 0.8-1.2x
# of this estimate for numeric, mixed, wide and text-heavy rows.
_VALUE_COST = 64
# Postgres keeps its parallel workers only when the whole capped result is read
# in one execute, which buffers limit + 1 rows in the driver before anything
# can be measured. Requests up to this many rows keep that fast path; larger
# ones are read in batches so the byte budget can stop them early. Fixed rather
# than tied to the admin's default row limit, which may be set as high as
# MAX_ROW_LIMIT.
_FAST_PATH_MAX_ROWS = 50_000
_STREAM_BATCH_ROWS = 5_000


def _estimated_size(value: Any, depth: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return 8
    if isinstance(value, str | bytes | bytearray | memoryview):
        return _VALUE_COST + len(value)
    if depth < 4:
        if isinstance(value, dict):
            return _VALUE_COST + sum(
                _estimated_size(key, depth + 1) + _estimated_size(item, depth + 1)
                for key, item in value.items()
            )
        if isinstance(value, list | tuple | set):
            return _VALUE_COST + sum(_estimated_size(item, depth + 1) for item in value)
    return _VALUE_COST


_SIZED_TYPES = (str, bytes)
_CONTAINER_TYPES = (dict, list, tuple, set)


def _row_size(values: list[Any]) -> int:
    """``_estimated_size`` summed over a row, without a call per value: every
    query's rows go through here, so the common scalar case stays cheap."""
    size = _VALUE_COST * (len(values) + 1)
    for value in values:
        kind = type(value)
        if kind in _SIZED_TYPES:
            size += len(value)
        elif value is None or kind is bool:
            size -= _VALUE_COST - 8
        elif kind in _CONTAINER_TYPES or isinstance(value, bytearray | memoryview):
            size += _estimated_size(value) - _VALUE_COST
    return size


class _RowCollector:
    """Turns driver rows into lists until ``limit`` rows or the byte budget.

    The first row is always kept: it is already in memory, and an empty result
    would hide why it was cut. ``truncated`` is set once a row is left behind.
    """

    __slots__ = ("limit", "budget", "used", "rows", "truncated")

    def __init__(self, limit: int, budget: int) -> None:
        self.limit = limit
        self.budget = budget
        self.used = 0
        self.rows: list[list[Any]] = []
        self.truncated = False

    def add(self, row: Any) -> bool:
        """Keep ``row`` if it fits; return False once the result is full."""
        if self.limit and len(self.rows) >= self.limit:
            self.truncated = True
            return False
        values = list(row)
        size = _row_size(values)
        if self.rows and self.used + size > self.budget:
            self.truncated = True
            return False
        self.used += size
        self.rows.append(values)
        return True


def _collect_rows(
    source: Iterable[Any], limit: int, budget: int
) -> tuple[list[list[Any]], bool]:
    collector = _RowCollector(limit, budget)
    for row in source:
        if not collector.add(row):
            break
    return collector.rows, collector.truncated


async def _collect_streamed_rows(
    result: Any, limit: int, budget: int
) -> tuple[list[list[Any]], bool]:
    """Read a streaming result in batches, stopping at ``limit`` rows or the
    byte budget, so memory stays bounded by whichever comes first."""
    collector = _RowCollector(limit, budget)
    while True:
        # Never ask for more than one row past the limit: a small limit still
        # reads only limit + 1 rows.
        size = _STREAM_BATCH_ROWS
        if limit:
            size = min(size, limit - len(collector.rows) + 1)
        batch = await result.fetchmany(size)
        if not batch:
            return collector.rows, collector.truncated
        for row in batch:
            if not collector.add(row):
                return collector.rows, collector.truncated


def _postgres_row_capped_sql(sql: str, max_rows: int, *, materialized: bool) -> str:
    """Wrap a SELECT so Postgres itself returns at most ``max_rows`` rows.

    The query goes into a MATERIALIZED CTE rather than a subquery because
    Postgres plans a CTE for full retrieval. A LIMIT on a subquery can switch
    the planner to a fast-start plan, e.g. walking an ORDER BY index through a
    filter that matches far fewer rows than estimated, which is many times
    slower when the estimate is wrong. The CTE is still read lazily, so the
    scan stops once the LIMIT is met. Pass ``materialized=False`` for
    Postgres < 12: it rejects the keyword and always materializes CTEs anyway.
    """
    body = sql.rstrip()
    # check_multi_statement() lets a single trailing semicolon through, but it
    # is a syntax error inside the CTE.
    if body.endswith(";"):
        body = body[:-1].rstrip()
    fence = "MATERIALIZED " if materialized else ""
    # The newline before ")" keeps a trailing `--` comment from swallowing it.
    return (
        f"WITH unibridge_capped AS {fence}(\n{body}\n)\n"
        f"SELECT * FROM unibridge_capped LIMIT {int(max_rows)}"
    )


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

    budget = settings.MAX_RESULT_BYTES
    async with engine.connect() as conn:
        if is_select:
            if (
                limit
                and limit <= _FAST_PATH_MAX_ROWS
                and statement_type == "select"
                and engine.dialect.name == "postgresql"
            ):
                # Postgres never runs parallel workers for a query the client
                # reads in batches (an Execute message with a row count, which
                # is how the server-side cursor below is paged), so aggregates
                # and scans over big tables were 2-3x slower through
                # conn.stream(). Put the cap in the SQL instead: a plain
                # execute runs to completion with parallel workers, and the
                # LIMIT still bounds what the driver buffers to limit + 1 rows.
                # Larger requests take the streaming path below: a buffered
                # result of up to a million rows would fill the container
                # before the byte budget could look at it.
                server_version = engine.dialect.server_version_info
                capped = text(
                    _postgres_row_capped_sql(
                        sql,
                        limit + 1,
                        materialized=server_version is not None and server_version >= (12,),
                    )
                )
                if params:
                    capped = capped.bindparams(**params)
                try:
                    result = await conn.execute(capped)
                except StatementError as exc:
                    # Errors land in the audit log and query history; show the
                    # caller's SQL there, not the wrapper around it.
                    exc.statement = sql
                    raise
                columns = list(result.keys())
                rows, truncated = _collect_rows(result.fetchall(), limit, budget)
            else:
                # Read through a server-side (streaming) cursor rather than
                # buffering the whole result set: `conn.execute()` makes the
                # driver materialise every row in memory at execute time, so
                # `limit` would only trim the response after the damage was
                # done. With `conn.stream()` rows are pulled in batches and we
                # stop at the limit or the byte budget — one row more is what
                # tells us the result was truncated — so peak memory is bounded
                # by the smaller of the two, not by the size of the table.
                async with conn.stream(stmt) as result:
                    columns = list(result.keys())
                    rows, truncated = await _collect_streamed_rows(result, limit, budget)
            row_count = len(rows)
        else:
            # DML / DDL - commit and return rowcount
            result = await conn.execute(stmt)
            await conn.commit()
            if result.returns_rows:
                # Handle RETURNING clauses
                columns = list(result.keys())
                all_rows = result.fetchmany(limit + 1) if limit else result.fetchall()
                rows, truncated = _collect_rows(all_rows, limit, budget)
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
    effective_limit = limit or settings_manager.default_row_limit
    # Hard ceiling on rows ever pulled into service memory. Clamp rather than
    # reject: callers legitimately ask for very large result sets and
    # `truncated` already tells them the response was cut short.
    effective_limit = min(effective_limit, settings.MAX_ROW_LIMIT)
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


def _clickhouse_select(
    client: Any,
    sql: str,
    params: dict[str, Any] | None,
    ch_settings: dict[str, Any],
    limit: int,
    budget: int,
) -> tuple[list[str], list[list[Any]], bool]:
    """Read a ClickHouse result block by block until the row limit or the byte
    budget. Leaving the stream early closes the HTTP response, which cancels
    the read-only query on the server."""
    with client.query_row_block_stream(
        sql, parameters=params or {}, settings=ch_settings
    ) as stream:
        columns = list(stream.source.column_names)
        collector = _RowCollector(limit, budget)
        for block in stream:
            for row in block:
                if not collector.add(row):
                    return columns, collector.rows, collector.truncated
    return columns, collector.rows, collector.truncated


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
        select_args = (client, sql, params, ch_settings, limit, settings.MAX_RESULT_BYTES)
        if lock is None:
            columns, rows, truncated = await asyncio.to_thread(_clickhouse_select, *select_args)
        else:
            columns, rows, truncated = await asyncio.to_thread(
                run_clickhouse_locked, lock, _clickhouse_select, *select_args
            )
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
    effective_limit = min(limit or settings_manager.default_row_limit, settings.MAX_ROW_LIMIT)
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


# ── ClickHouse-rendered results (QueryRequest.render == "database") ──────────

# ClickHouse writes the rows itself, one JSON array per line after a line of
# column names, and the service only relays the bytes. These settings make
# that JSON match the service's own for most types; QueryRequest.render lists
# the values that still come out differently. The reply streams (no
# wait_end_of_query), so closing it at the row limit or the byte budget stops
# the query on the server rather than leaving it to buffer the whole result.
_CLICKHOUSE_JSON_FORMAT = "JSONCompactEachRowWithNames"
_CLICKHOUSE_JSON_SETTINGS: dict[str, Any] = {
    "output_format_json_quote_64bit_integers": 0,
    "output_format_json_quote_decimals": 1,
    "output_format_decimal_trailing_zeros": 1,
    "output_format_json_escape_forward_slashes": 0,
    "output_format_json_validate_utf8": 1,
    "date_time_output_format": "iso",
    # Never write a failure into the format itself: in JSONCompactEachRow it
    # would look like one more row of the result.
    "http_write_exception_in_output_format": 0,
}
_RELAY_CHUNK_BYTES = 1 << 20
# A failure after the rows have started arrives as a block appended to the
# stream ("\r\n__exception__\r\n<tag>\r\n<message>..."), after which the
# server drops the connection. JSON rows never hold a raw carriage return, so
# the first one marks the block; this much of it is read for the error code.
_EXCEPTION_BLOCK_MAX_BYTES = 64 * 1024
_EXCEPTION_CODE_RE = re.compile(rb"Code: (\d+)\.")
_CLICKHOUSE_TIMEOUT_CODE = 159
# Before 25.11 such a failure is a plain text line with nothing to tell it
# apart from the rows, so older servers keep the service path.
_CLICKHOUSE_JSON_MIN_VERSION = "25.11"


@dataclass(frozen=True)
class RenderedQueryResult:
    """A QueryResponse body written by ClickHouse, kept as the byte parts it
    was read in so that it is sent without being joined or parsed."""

    parts: list[bytes]
    row_count: int
    truncated: bool
    elapsed_ms: int


class ClickHouseStreamError(Exception):
    """ClickHouse failed after it had started sending rows."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code


def _exception_block_error(data: bytes, chunks: Iterator[bytes]) -> ClickHouseStreamError:
    """Read the rest of the exception block that starts at the first carriage
    return in ``data`` and turn it into an error."""
    block = data[data.find(b"\r"):]
    try:
        for chunk in chunks:
            block += chunk
            if len(block) >= _EXCEPTION_BLOCK_MAX_BYTES:
                break
    except ProtocolError:
        pass  # The server ends the reply without its closing chunk.
    block = block[:_EXCEPTION_BLOCK_MAX_BYTES]
    match = _EXCEPTION_CODE_RE.search(block)
    return ClickHouseStreamError(
        int(match.group(1)) if match else None,
        block.decode("utf-8", "replace").strip(),
    )


def clickhouse_can_render_json(client: Any) -> bool:
    """Whether this ClickHouse marks a failure that happens mid-reply."""
    return bool(client.min_version(_CLICKHOUSE_JSON_MIN_VERSION))


def _is_clickhouse_timeout(exc: Exception) -> bool:
    """Whether ClickHouse stopped the query at max_execution_time (code 159).
    Before the rows start, clickhouse-connect's message opens with the code
    from ClickHouse's exception header; text later in it is the query's own."""
    if isinstance(exc, ClickHouseStreamError):
        return exc.code == _CLICKHOUSE_TIMEOUT_CODE
    return str(exc).startswith(
        f"Received ClickHouse exception, code: {_CLICKHOUSE_TIMEOUT_CODE},"
    )


def _nth_newline(data: bytes, n: int) -> int:
    """Index of the ``n``-th newline in ``data`` (1-based), or -1."""
    pos = -1
    for _ in range(n):
        pos = data.find(b"\n", pos + 1)
        if pos < 0:
            break
    return pos


def _clickhouse_json_select(
    client: Any,
    sql: str,
    params: dict[str, Any] | None,
    ch_settings: dict[str, Any],
    limit: int,
    budget: int,
) -> tuple[list[bytes], int, bool]:
    """Read ClickHouse's JSON into QueryResponse's shape, up to the open rows
    array. No value is parsed: each row is one line, so rows are counted by
    newlines and joined by turning every newline into a comma.

    Stops after ``limit`` rows or once the rows reach ``budget`` bytes, closing
    the response early, which also stops the query on the server. As in
    _RowCollector the first row is always kept, and ``truncated`` is set once a
    row is left behind.
    """
    response = client.raw_stream(
        sql, parameters=params or {}, settings=ch_settings, fmt=_CLICKHOUSE_JSON_FORMAT
    )
    try:
        chunks = iter(response.stream(_RELAY_CHUNK_BYTES))
        head = b""
        for chunk in chunks:
            head += chunk
            if b"\n" in head:
                break
        names, newline, rest = head.partition(b"\n")
        if b"\r" in names:
            raise _exception_block_error(head, chunks)
        if not newline:
            raise RuntimeError("ClickHouse returned no column names")
        parts = [b'{"columns":', names, b',"rows":[']
        rows = used = 0
        truncated = False
        # Pieces of a row whose end has not arrived yet; none holds a newline.
        pending: list[bytes] = []
        pending_size = 0
        for chunk in itertools.chain((rest,), chunks):
            if not chunk:
                continue
            if limit and rows >= limit and (pending_size or not chunk.startswith(b"\r")):
                # A row past the limit has started.
                truncated = True
                break
            # A raw carriage return opens ClickHouse's exception block. Only the
            # rows before it are real, and as on the service path only stopping
            # short of it (row limit or budget) avoids the error.
            cr = chunk.find(b"\r")
            failure = chunk[cr:] if cr >= 0 else b""
            if failure:
                chunk = chunk[:cr]
            end = chunk.rfind(b"\n") + 1
            if end:
                pending.append(chunk[:end])
                block = b"".join(pending)
                pending = [chunk[end:]]
                pending_size = len(chunk) - end
                if not block.startswith(b"[") or block.count(b"\n[") != block.count(b"\n") - 1:
                    raise RuntimeError("ClickHouse sent a line that is not a row")
                keep = len(block)
                if limit and rows + block.count(b"\n") > limit:
                    keep = _nth_newline(block, limit - rows) + 1
                if used + keep > budget:
                    keep = block.rfind(b"\n", 0, max(0, min(keep, budget - used))) + 1
                    if not keep and not rows:
                        # The first row is kept, however long it is.
                        keep = block.find(b"\n") + 1
                if keep:
                    rows += block.count(b"\n", 0, keep)
                    used += keep
                    parts.append(block[:keep].replace(b"\n", b","))
                if keep < len(block):
                    truncated = True
                    break
            else:
                pending.append(chunk)
                pending_size += len(chunk)
                if pending_size and rows and used + pending_size > budget:
                    # The row being read can no longer fit.
                    truncated = True
                    break
            if failure:
                if pending_size and (
                    (limit and rows >= limit) or (rows and used + pending_size > budget)
                ):
                    # A row had started that this result stops before anyway.
                    truncated = True
                    break
                raise _exception_block_error(failure, chunks)
        else:
            if pending_size:
                raise RuntimeError("ClickHouse response ended inside a row")
        if rows:
            parts[-1] = parts[-1][:-1]
        return parts, rows, truncated
    finally:
        response.close()


async def execute_clickhouse_json(
    client: Any,
    sql: str,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    timeout: int | None = None,
    lock: threading.Lock | None = None,
) -> RenderedQueryResult:
    """Run a ClickHouse SELECT/EXPLAIN with ClickHouse writing the result JSON.

    Same limits as ``execute_clickhouse_query``, except that MAX_RESULT_BYTES
    caps the JSON itself rather than an estimate of Python objects. The
    timeout also goes to the server as ``max_execution_time``: a query that
    has not written anything yet would not notice that the caller gave up.
    """
    effective_limit = min(limit or settings_manager.default_row_limit, settings.MAX_ROW_LIMIT)
    effective_timeout = timeout or settings.DEFAULT_QUERY_TIMEOUT

    if check_multi_statement(sql):
        raise ValueError(
            "Multi-statement SQL is not allowed. Remove semicolons or contact an admin."
        )
    ch_settings = {**_CLICKHOUSE_JSON_SETTINGS, "max_execution_time": effective_timeout}
    if effective_limit:
        ch_settings["max_result_rows"] = effective_limit + 1
        ch_settings["result_overflow_mode"] = "break"
    # raw_stream appends the FORMAT clause to the SQL as given, so a trailing
    # semicolon (check_multi_statement lets one through) would end it early.
    statement = sql.rstrip().removesuffix(";")
    select_args = (client, statement, params, ch_settings, effective_limit, settings.MAX_RESULT_BYTES)
    start = time.monotonic()

    def _run() -> tuple[list[bytes], int, bool]:
        if lock is None:
            return _clickhouse_json_select(*select_args)
        return run_clickhouse_locked(lock, _clickhouse_json_select, *select_args)

    try:
        parts, row_count, truncated = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=effective_timeout
        )
    except asyncio.TimeoutError:
        logger.warning("Query timed out after %ds", effective_timeout)
        raise
    except (ClickHouseDatabaseError, ClickHouseStreamError) as exc:
        if not _is_clickhouse_timeout(exc):
            raise
        logger.warning("Query timed out after %ds", effective_timeout)
        raise asyncio.TimeoutError from exc
    elapsed_ms = int((time.monotonic() - start) * 1000)
    parts.append(
        b'],"row_count":%d,"truncated":%s,"elapsed_ms":%d,"graph":null}'
        % (row_count, b"true" if truncated else b"false", elapsed_ms)
    )
    return RenderedQueryResult(parts, row_count, truncated, elapsed_ms)


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
    readonly: bool,
) -> QueryResponse:
    """Core execution logic for Neo4j via the official driver."""
    start = time.monotonic()
    cypher = Neo4jQuery(query, timeout=timeout)
    # READ access mode is enforced by the server (and routes to secondaries in
    # a cluster), so a write misclassified as a read upstream still fails here.
    access_mode = NEO4J_READ_ACCESS if readonly else NEO4J_WRITE_ACCESS
    with driver.session(database=database, default_access_mode=access_mode) as session:
        result = session.run(cypher, parameters=params or {})
        columns = list(result.keys())
        collector = _RowCollector(limit, settings.MAX_RESULT_BYTES)
        for record in result:
            if not collector.add(_convert_neo4j_value(value) for value in record.values()):
                break
        rows, truncated = collector.rows, collector.truncated

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
    readonly: bool = True,
) -> QueryResponse:
    """Execute a Neo4j Cypher query with timeout and row limit.

    ``readonly=True`` (the default) opens the session in READ access mode, so
    the server rejects writes; pass ``False`` only for authorized data writes.
    """
    effective_limit = min(limit or settings_manager.default_row_limit, settings.MAX_ROW_LIMIT)
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
                readonly,
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
_XSD_DECIMAL = {_XSD + "decimal"}
_XSD_FLOAT = {_XSD + s for s in ("double", "float")}


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
    if datatype in _XSD_DECIMAL:
        try:
            return Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return value
    if datatype in _XSD_FLOAT:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    language = binding.get("xml:lang")
    if language:
        return {"value": value, "xml:lang": language}
    return value


def _truncate_preview(body: str, max_chars: int = 200) -> str:
    return body if len(body) <= max_chars else body[: max_chars - 1] + "…"


def _map_graphdb_error(status_code: int, body_preview: str, repo: str) -> HTTPException:
    # Log the raw preview for operators regardless of mapping.
    logger.warning(
        "GraphDB returned status=%d for repo=%s body=%r",
        status_code,
        repo,
        body_preview[:500],
    )

    if status_code in (401, 403):
        return HTTPException(status_code=502, detail="GraphDB authentication failed")
    if status_code == 404 and "repository" in body_preview.lower():
        return HTTPException(
            status_code=404, detail=f"GraphDB repository not found: {repo}"
        )
    if 400 <= status_code < 500:
        sanitized = "".join(
            c for c in body_preview if c.isprintable() or c in " \t\r\n"
        )
        return HTTPException(
            status_code=400,
            detail=f"GraphDB rejected query: {_truncate_preview(sanitized)}",
        )
    return HTTPException(status_code=502, detail="GraphDB upstream error")


async def execute_graphdb_query(
    *,
    client: httpx.AsyncClient,
    repo: str,
    sparql: str,
    statement_type: str,
    limit: int,
    timeout: int | float | None = None,
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
        stream_kwargs: dict[str, Any] = {}
        if timeout is not None:
            stream_kwargs["timeout"] = timeout
        async with client.stream(
            "POST",
            graphdb_repository_path(repo),
            content=sparql,
            headers={
                "Content-Type": "application/sparql-query",
                "Accept": accept,
            },
            **stream_kwargs,
        ) as resp:
            raw = await read_capped_response(resp, max_bytes)

            if resp.status_code >= 400:
                preview = raw.decode("utf-8", errors="replace")
                raise _map_graphdb_error(resp.status_code, preview, repo)
    except GraphDBResponseTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
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
        if "boolean" not in parsed:
            raise HTTPException(
                status_code=502,
                detail="GraphDB returned malformed ASK result",
            )
        raw_val = parsed["boolean"]
        if isinstance(raw_val, bool):
            ask_value = raw_val
        elif isinstance(raw_val, str):
            normalized = raw_val.strip().lower()
            if normalized not in {"true", "false"}:
                raise HTTPException(
                    status_code=502,
                    detail="GraphDB returned malformed ASK result",
                )
            ask_value = normalized == "true"
        else:
            raise HTTPException(
                status_code=502,
                detail="GraphDB returned malformed ASK result",
            )
        return QueryResponse(
            columns=["boolean"], rows=[[ask_value]], row_count=1,
            truncated=False, elapsed_ms=elapsed_ms, graph=None,
        )

    # SELECT
    head = parsed.get("head", {})
    columns = list(head.get("vars", []))
    bindings = parsed.get("results", {}).get("bindings", [])
    sampled_bindings = bindings[: limit + 1] if limit else bindings
    raw_rows = [
        [_coerce_binding_value(b[v]) if v in b else None for v in columns]
        for b in sampled_bindings
    ]
    truncated = len(raw_rows) > limit
    rows = raw_rows[:limit] if truncated else raw_rows
    return QueryResponse(
        columns=columns, rows=rows, row_count=len(rows),
        truncated=truncated, elapsed_ms=elapsed_ms, graph=None,
    )
