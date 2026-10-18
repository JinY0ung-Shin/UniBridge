"""ClickHouse-rendered results (QueryRequest.render == "database"): ClickHouse
writes the rows as JSON and the service relays the bytes in QueryResponse's
shape without parsing a value."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz
from clickhouse_connect.driver.exceptions import DatabaseError
from clickhouse_connect.driver.httpclient import HttpClient
from clickhouse_connect.driver.models import SettingDef
from urllib3.exceptions import ProtocolError

from app.auth import CurrentUser
from app.config import settings
from app.routers import query
from app.schemas import QueryResponse, QueryTemplateExecuteRequest
from app.services import query_executor
from app.services.query_executor import (
    ClickHouseStreamError,
    RenderedQueryResult,
    execute_clickhouse_json,
)
from tests.conftest import auth_header

CHUNK_SIZES = [1, 2, 3, 7, 64, 1 << 20]


class _FakeRawResponse:
    """Stands in for the urllib3 response that clickhouse-connect's raw_stream
    returns: hands out the body in fixed-size chunks (an empty one between
    each, as a decoder may yield) and records closing. ``broken`` ends the
    stream the way ClickHouse does after an exception block: without its
    closing chunk, which urllib3 reports as a ProtocolError."""

    def __init__(self, body: bytes, chunk_size: int, *, broken: bool = False) -> None:
        self._body = body
        self._chunk_size = chunk_size
        self._broken = broken
        self.bytes_read = 0
        self.closed = False

    def stream(self, _amt: int):
        for start in range(0, len(self._body), self._chunk_size):
            chunk = self._body[start:start + self._chunk_size]
            self.bytes_read += len(chunk)
            yield chunk
            yield b""
        if self._broken:
            raise ProtocolError("Connection broken: IncompleteRead(0 bytes read)")

    def close(self) -> None:
        self.closed = True


def _clickhouse_body(names: list[str], rows: list[list]) -> bytes:
    """JSONCompactEachRowWithNames as ClickHouse writes it: a line of names,
    then one JSON array per row, every line ending in a newline."""
    lines = [names, *rows]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def _exception_block(code: int, message: str, tag: str = "lvdmluzvgelsedkc") -> bytes:
    """What ClickHouse 26.3 appends when a query fails after rows were sent."""
    text = f"Code: {code}. DB::Exception: {message}"
    return (
        f"\r\n__exception__\r\n{tag}\r\n{text}\n{len(text)} {tag}\r\n__exception__\r\n"
    ).encode()


def _client(
    body: bytes, chunk_size: int = 7, *, broken: bool = False
) -> tuple[MagicMock, _FakeRawResponse]:
    response = _FakeRawResponse(body, chunk_size, broken=broken)
    client = MagicMock()
    client.raw_stream.return_value = response
    return client, response


class _ChunkedRawResponse(_FakeRawResponse):
    """Hands out exactly the given chunks."""

    def __init__(self, chunks: list[bytes], *, broken: bool = False) -> None:
        super().__init__(b"".join(chunks), 1, broken=broken)
        self._chunks = chunks

    def stream(self, _amt: int):
        yield from self._chunks
        if self._broken:
            raise ProtocolError("Connection broken: IncompleteRead(0 bytes read)")


def _payload(result: RenderedQueryResult) -> dict:
    return json.loads(b"".join(result.parts))


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_relays_rows_in_the_query_response_shape(chunk_size):
    rows = [[1, "a"], [2, "line\nbreak, \"quoted\""], [3, None]]
    client, response = _client(_clickhouse_body(["id", "name"], rows), chunk_size)

    result = await execute_clickhouse_json(client, "SELECT id, name FROM t")

    payload = _payload(result)
    assert payload == {
        "columns": ["id", "name"],
        "rows": rows,
        "row_count": 3,
        "truncated": False,
        "elapsed_ms": result.elapsed_ms,
        "graph": None,
    }
    QueryResponse.model_validate(payload)
    assert (result.row_count, result.truncated) == (3, False)
    assert response.closed is True


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_empty_result_keeps_the_column_names(chunk_size):
    client, _ = _client(_clickhouse_body(["id"], []), chunk_size)

    result = await execute_clickhouse_json(client, "SELECT id FROM t WHERE 0")

    payload = _payload(result)
    assert (payload["columns"], payload["rows"], payload["row_count"]) == (["id"], [], 0)
    assert payload["truncated"] is False


async def test_sends_the_format_and_settings_clickhouse_needs():
    client, _ = _client(_clickhouse_body(["v"], [[1]]))

    await execute_clickhouse_json(client, "SELECT 1 AS v;  ", limit=10, timeout=42)

    args, kwargs = client.raw_stream.call_args
    # The trailing semicolon would end the statement before the FORMAT clause.
    assert args == ("SELECT 1 AS v",)
    assert kwargs["fmt"] == "JSONCompactEachRowWithNames"
    assert kwargs["parameters"] == {}
    ch_settings = kwargs["settings"]
    assert ch_settings["max_result_rows"] == 11
    assert ch_settings["result_overflow_mode"] == "break"
    assert ch_settings["max_execution_time"] == 42
    # The reply has to stream: closing it at the limit or the byte budget is
    # what stops the query on the server. With wait_end_of_query ClickHouse
    # would first buffer the whole result, however large.
    assert "wait_end_of_query" not in ch_settings
    assert ch_settings["date_time_output_format"] == "iso"
    assert ch_settings["output_format_json_quote_64bit_integers"] == 0
    assert ch_settings["output_format_json_quote_decimals"] == 1
    # Written into the format, a failure would read as one more row.
    assert ch_settings["http_write_exception_in_output_format"] == 0


async def test_passes_bind_parameters_through():
    client, _ = _client(_clickhouse_body(["v"], [[7]]))

    await execute_clickhouse_json(client, "SELECT {v:UInt8} AS v", params={"v": 7})

    assert client.raw_stream.call_args.kwargs["parameters"] == {"v": 7}


async def test_caller_limit_is_clamped():
    client, _ = _client(_clickhouse_body(["v"], [[1]]))

    await execute_clickhouse_json(client, "SELECT 1", limit=settings.MAX_ROW_LIMIT + 5)

    ch_settings = client.raw_stream.call_args.kwargs["settings"]
    assert ch_settings["max_result_rows"] == settings.MAX_ROW_LIMIT + 1


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_limit_cuts_rows_and_marks_truncated(chunk_size):
    rows = [[i] for i in range(12)]
    client, response = _client(_clickhouse_body(["id"], rows), chunk_size)

    result = await execute_clickhouse_json(client, "SELECT id FROM t", limit=10)

    payload = _payload(result)
    assert payload["rows"] == rows[:10]
    assert (payload["row_count"], payload["truncated"]) == (10, True)
    assert response.closed is True


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_limit_met_by_the_last_row_is_not_truncated(chunk_size):
    rows = [[i] for i in range(10)]
    client, _ = _client(_clickhouse_body(["id"], rows), chunk_size)

    result = await execute_clickhouse_json(client, "SELECT id FROM t", limit=10)

    assert (result.row_count, result.truncated) == (10, False)
    assert _payload(result)["rows"] == rows


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_byte_budget_keeps_whole_rows_and_stops_reading(monkeypatch, chunk_size):
    rows = [["x" * 90] for _ in range(50)]
    row_bytes = len(json.dumps(rows[0])) + 1
    monkeypatch.setattr(settings, "MAX_RESULT_BYTES", row_bytes * 3 + row_bytes // 2)
    body = _clickhouse_body(["payload"], rows)
    client, response = _client(body, chunk_size)

    result = await execute_clickhouse_json(client, "SELECT payload FROM t", limit=100)

    payload = _payload(result)
    assert payload["rows"] == rows[:3]
    assert (payload["row_count"], payload["truncated"]) == (3, True)
    # Leaving early closes the response instead of reading the rest.
    assert response.closed is True
    if chunk_size < len(body):
        assert response.bytes_read < len(body)


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_first_row_is_kept_even_over_the_budget(monkeypatch, chunk_size):
    monkeypatch.setattr(settings, "MAX_RESULT_BYTES", 10)
    rows = [["y" * 100], ["z"]]
    client, _ = _client(_clickhouse_body(["payload"], rows), chunk_size)

    result = await execute_clickhouse_json(client, "SELECT payload FROM t")

    assert _payload(result)["rows"] == rows[:1]
    assert (result.row_count, result.truncated) == (1, True)


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_a_single_row_over_the_budget_is_not_truncated(monkeypatch, chunk_size):
    monkeypatch.setattr(settings, "MAX_RESULT_BYTES", 10)
    rows = [["y" * 100]]
    client, _ = _client(_clickhouse_body(["payload"], rows), chunk_size)

    result = await execute_clickhouse_json(client, "SELECT payload FROM t")

    assert _payload(result)["rows"] == rows
    assert (result.row_count, result.truncated) == (1, False)


async def test_response_ending_inside_a_row_is_an_error():
    body = _clickhouse_body(["id"], [[1]]) + b"[2"
    client, response = _client(body)

    with pytest.raises(RuntimeError, match="inside a row"):
        await execute_clickhouse_json(client, "SELECT id FROM t")
    assert response.closed is True


async def test_response_without_column_names_is_an_error():
    client, response = _client(b"")

    with pytest.raises(RuntimeError, match="no column names"):
        await execute_clickhouse_json(client, "SELECT id FROM t")
    assert response.closed is True


async def test_server_side_timeout_becomes_a_timeout():
    client = MagicMock()
    client.raw_stream.side_effect = DatabaseError(
        "Received ClickHouse exception, code: 159, server response: Code: 159. "
        "DB::Exception: Timeout exceeded: elapsed 1000.2 ms, maximum: 1000 ms. "
        "(TIMEOUT_EXCEEDED)"
    )

    with pytest.raises(asyncio.TimeoutError):
        await execute_clickhouse_json(client, "SELECT sleep(3)", timeout=1)


async def test_other_database_errors_propagate():
    client = MagicMock()
    client.raw_stream.side_effect = DatabaseError(
        "Received ClickHouse exception, code: 47, server response: Code: 47. "
        "DB::Exception: Unknown expression identifier `nope`. (UNKNOWN_IDENTIFIER)"
    )

    with pytest.raises(DatabaseError, match="UNKNOWN_IDENTIFIER"):
        await execute_clickhouse_json(client, "SELECT nope")


async def test_multi_statement_sql_is_rejected_before_clickhouse():
    client, _ = _client(_clickhouse_body(["v"], [[1]]))

    with pytest.raises(ValueError, match="Multi-statement"):
        await execute_clickhouse_json(client, "SELECT 1; SELECT 2")
    client.raw_stream.assert_not_called()


async def test_runs_under_the_alias_lock():
    lock = threading.Lock()
    held: list[bool] = []
    response = _FakeRawResponse(_clickhouse_body(["v"], [[1]]), 7)
    client = MagicMock()

    def raw_stream(*_args, **_kwargs):
        held.append(lock.locked())
        return response

    client.raw_stream.side_effect = raw_stream

    await execute_clickhouse_json(client, "SELECT 1", lock=lock)

    assert held == [True]
    assert lock.locked() is False


async def test_the_driver_sends_the_settings_and_the_format_after_the_sql():
    # Pin what clickhouse-connect itself makes of the call: none of the
    # settings dropped by its validation (given ClickHouse 26.3's defaults),
    # the FORMAT clause after the SQL without its semicolon, params bound.
    client, _ = _client(_clickhouse_body(["v"], [[7]]))
    await execute_clickhouse_json(
        client, "SELECT {v:UInt8} AS v;", params={"v": 7}, limit=10, timeout=42
    )
    (statement,), kwargs = client.raw_stream.call_args

    driver = HttpClient.__new__(HttpClient)
    driver.params = {}
    driver.database = "analytics"
    driver.form_encode_query_params = False
    driver.server_tz = pytz.UTC
    defaults = {
        "output_format_json_quote_64bit_integers": "1",
        "output_format_json_quote_decimals": "0",
        "output_format_decimal_trailing_zeros": "0",
        "output_format_json_escape_forward_slashes": "1",
        "output_format_json_validate_utf8": "0",
        "date_time_output_format": "simple",
        "max_execution_time": "0",
        "max_result_rows": "0",
        "result_overflow_mode": "throw",
        "http_write_exception_in_output_format": "0",
    }
    driver.server_settings = {
        name: SettingDef(name=name, value=value, readonly=0) for name, value in defaults.items()
    }
    body, url_params, _ = driver._prep_raw_query(
        statement, kwargs["parameters"], kwargs["settings"], kwargs["fmt"], True, None
    )

    assert body == "SELECT {v:UInt8} AS v\n FORMAT JSONCompactEachRowWithNames"
    assert url_params["param_v"] == "7"
    assert {name: str(url_params[name]) for name in defaults} == {
        "output_format_json_quote_64bit_integers": "0",
        "output_format_json_quote_decimals": "1",
        "output_format_decimal_trailing_zeros": "1",
        "output_format_json_escape_forward_slashes": "0",
        "output_format_json_validate_utf8": "1",
        "date_time_output_format": "iso",
        "max_execution_time": "42",
        "max_result_rows": "11",
        "result_overflow_mode": "break",
        "http_write_exception_in_output_format": "0",
    }


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_an_exception_block_after_rows_is_an_error(chunk_size):
    body = _clickhouse_body(["id"], [[1], [2]]) + _exception_block(
        395, "boom: while executing 'FUNCTION throwIf()'. (FUNCTION_THROW_IF_VALUE_IS_NON_ZERO)"
    )
    client, response = _client(body, chunk_size, broken=True)

    with pytest.raises(ClickHouseStreamError) as exc_info:
        await execute_clickhouse_json(client, "SELECT id, throwIf(id = 3) FROM t")

    assert exc_info.value.code == 395
    assert "FUNCTION_THROW_IF_VALUE_IS_NON_ZERO" in str(exc_info.value)
    assert response.closed is True


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_an_exception_block_before_any_row_is_an_error(chunk_size):
    body = _clickhouse_body(["id"], []) + _exception_block(241, "Memory limit exceeded")
    client, _ = _client(body, chunk_size, broken=True)

    with pytest.raises(ClickHouseStreamError) as exc_info:
        await execute_clickhouse_json(client, "SELECT id FROM t")

    assert exc_info.value.code == 241


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_a_timeout_in_the_stream_becomes_a_timeout(chunk_size):
    body = _clickhouse_body(["n"], [[0]]) + _exception_block(
        159, "Timeout exceeded: elapsed 1640.1 ms, maximum: 1000 ms. (TIMEOUT_EXCEEDED)"
    )
    client, _ = _client(body, chunk_size, broken=True)

    with pytest.raises(asyncio.TimeoutError):
        await execute_clickhouse_json(client, "SELECT n FROM t", timeout=1)


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_a_failure_past_the_limit_is_never_reached(chunk_size):
    # Like the service path, which stops at the row after the limit.
    body = _clickhouse_body(["id"], [[i] for i in range(7)]) + _exception_block(395, "boom")
    client, _ = _client(body, chunk_size, broken=True)

    result = await execute_clickhouse_json(client, "SELECT id FROM t", limit=5)

    assert _payload(result)["rows"] == [[i] for i in range(5)]
    assert (result.row_count, result.truncated) == (5, True)


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_a_failure_right_after_the_limit_is_an_error(chunk_size):
    # The service path reads one row past the limit to learn whether the
    # result was cut, and meets the failure there; so does this path, however
    # the bytes are chunked.
    body = _clickhouse_body(["id"], [[i] for i in range(5)]) + _exception_block(395, "boom")
    client, _ = _client(body, chunk_size, broken=True)

    with pytest.raises(ClickHouseStreamError):
        await execute_clickhouse_json(client, "SELECT id FROM t", limit=5)


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_a_failure_after_an_oversized_first_row_is_an_error(monkeypatch, chunk_size):
    # The first row is kept over the budget; what follows it is the failure,
    # not a row that could not fit.
    monkeypatch.setattr(settings, "MAX_RESULT_BYTES", 10)
    body = _clickhouse_body(["v"], [["x" * 50]]) + _exception_block(395, "boom")
    client, _ = _client(body, chunk_size, broken=True)

    with pytest.raises(ClickHouseStreamError):
        await execute_clickhouse_json(client, "SELECT v FROM t")


FAILURE = _exception_block(395, "boom while writing a row")


@pytest.mark.parametrize(
    "chunks",
    [
        [b'["v"]\n[1]\n', b"[2" + FAILURE],
        [b'["v"]\n', b"[1]\n[2" + FAILURE],
        [b'["v"]\n[1]\n[', b"2" + FAILURE],
    ],
)
async def test_a_row_started_past_the_limit_stops_before_the_failure(chunks):
    # ClickHouse failed while writing row 2. Row 2 is past the limit, so the
    # result is cut there however the bytes were chunked.
    response = _ChunkedRawResponse(chunks, broken=True)
    client = MagicMock()
    client.raw_stream.return_value = response

    result = await execute_clickhouse_json(client, "SELECT v FROM t", limit=1)

    assert _payload(result)["rows"] == [[1]]
    assert (result.row_count, result.truncated) == (1, True)


@pytest.mark.parametrize(
    "chunks",
    [
        [b'["v"]\n["aaaa"]\n', b'["bbbbbbbbbbbbbbbbbbbb' + FAILURE],
        [b'["v"]\n', b'["aaaa"]\n["bbbbbbbbbbbbbbbbbbbb' + FAILURE],
    ],
)
async def test_a_row_started_past_the_budget_stops_before_the_failure(monkeypatch, chunks):
    monkeypatch.setattr(settings, "MAX_RESULT_BYTES", 15)
    response = _ChunkedRawResponse(chunks, broken=True)
    client = MagicMock()
    client.raw_stream.return_value = response

    result = await execute_clickhouse_json(client, "SELECT v FROM t")

    assert _payload(result)["rows"] == [["aaaa"]]
    assert (result.row_count, result.truncated) == (1, True)


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_a_line_that_is_not_a_row_is_an_error(chunk_size):
    # How a server before 25.11 reports a failure mid-reply: a plain line.
    body = _clickhouse_body(["v"], [[1]]) + b"Code: 395. DB::Exception: boom\n"
    client, _ = _client(body, chunk_size)

    with pytest.raises(RuntimeError, match="not a row"):
        await execute_clickhouse_json(client, "SELECT v FROM t")


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
async def test_a_failure_past_the_budget_is_never_reached(monkeypatch, chunk_size):
    rows = [["x" * 90] for _ in range(6)]
    row_bytes = len(json.dumps(rows[0])) + 1
    monkeypatch.setattr(settings, "MAX_RESULT_BYTES", row_bytes * 3 + row_bytes // 2)
    body = _clickhouse_body(["payload"], rows) + _exception_block(395, "boom")
    client, _ = _client(body, chunk_size, broken=True)

    result = await execute_clickhouse_json(client, "SELECT payload FROM t", limit=100)

    assert (result.row_count, result.truncated) == (3, True)


async def test_a_broken_stream_without_an_exception_block_is_an_error():
    client, _ = _client(_clickhouse_body(["id"], [[1]]), broken=True)

    with pytest.raises(ProtocolError):
        await execute_clickhouse_json(client, "SELECT id FROM t")


@pytest.mark.parametrize("chunk_size", [7, 4096])
async def test_a_long_row_that_cannot_fit_stops_the_read(monkeypatch, chunk_size):
    # Once a row past the first cannot fit the budget, nothing more is read:
    # the rest of a long row is neither waited for nor copied around.
    monkeypatch.setattr(settings, "MAX_RESULT_BYTES", 1000)
    rows = [["a"], ["b" * 200_000], ["c"]]
    body = _clickhouse_body(["v"], rows)
    client, response = _client(body, chunk_size)

    result = await execute_clickhouse_json(client, "SELECT v FROM t")

    assert _payload(result)["rows"] == rows[:1]
    assert (result.row_count, result.truncated) == (1, True)
    assert response.bytes_read < 1000 + 2 * chunk_size + 100


async def test_a_long_first_row_is_kept_whole():
    rows = [["x" * 2_000_000], ["y"]]
    client, _ = _client(_clickhouse_body(["v"], rows), 4096)

    result = await execute_clickhouse_json(client, "SELECT v FROM t")

    assert _payload(result)["rows"] == rows


async def test_a_timeout_named_in_the_query_text_is_not_a_timeout():
    client = MagicMock()
    client.raw_stream.side_effect = DatabaseError(
        "Received ClickHouse exception, code: 47, server response: Code: 47. "
        "DB::Exception: Unknown expression identifier `TIMEOUT_EXCEEDED`. (UNKNOWN_IDENTIFIER)"
    )

    with pytest.raises(DatabaseError):
        await execute_clickhouse_json(client, "SELECT TIMEOUT_EXCEEDED")


async def test_no_row_limit_sends_no_result_cap(monkeypatch):
    # A default row limit of 0 means "no row limit" on the service path too.
    monkeypatch.setattr(query_executor.settings_manager, "default_row_limit", 0)
    rows = [[i] for i in range(30)]
    client, _ = _client(_clickhouse_body(["id"], rows))

    result = await execute_clickhouse_json(client, "SELECT id FROM t")

    ch_settings = client.raw_stream.call_args.kwargs["settings"]
    assert "max_result_rows" not in ch_settings
    assert "result_overflow_mode" not in ch_settings
    assert (result.row_count, result.truncated) == (30, False)


# ── Router ───────────────────────────────────────────────────────────────────


def _rendered(rows: list[list]) -> RenderedQueryResult:
    head = b'{"columns":["v"],"rows":['
    body = b",".join(json.dumps(row).encode() for row in rows)
    tail = b'],"row_count":%d,"truncated":false,"elapsed_ms":3,"graph":null}' % len(rows)
    return RenderedQueryResult([head, body, tail], len(rows), False, 3)


def _clickhouse_alias():
    return (
        patch("app.routers.query.connection_manager.get_db_type", return_value="clickhouse"),
        patch("app.routers.query.connection_manager.get_clickhouse_client", return_value=MagicMock()),
        patch("app.routers.query.connection_manager.get_clickhouse_lock", return_value=MagicMock()),
    )


async def test_render_database_sends_the_clickhouse_written_body(client, admin_token):
    rendered = AsyncMock(return_value=_rendered([[1], [2]]))
    service_path = AsyncMock()
    log_query = AsyncMock()
    db_type, ch_client, ch_lock = _clickhouse_alias()
    with (
        db_type, ch_client, ch_lock,
        patch("app.routers.query.execute_clickhouse_json", rendered),
        patch("app.routers.query.execute_clickhouse_query", service_path),
        patch("app.routers.query.log_query", log_query),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "analytics", "sql": "SELECT v FROM t", "render": "database"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.json() == {
        "columns": ["v"],
        "rows": [[1], [2]],
        "row_count": 2,
        "truncated": False,
        "elapsed_ms": 3,
        "graph": None,
    }
    service_path.assert_not_awaited()
    assert rendered.await_args.kwargs["sql"] == "SELECT v FROM t"
    log_query.assert_awaited_once()
    assert log_query.await_args.kwargs["row_count"] == 2
    assert log_query.await_args.kwargs["status"] == "success"


async def test_render_database_takes_explain_and_records_metrics(client, admin_token):
    rendered = AsyncMock(return_value=_rendered([["Expression"]]))
    record_query = MagicMock()
    db_type, ch_client, ch_lock = _clickhouse_alias()
    with (
        db_type, ch_client, ch_lock,
        patch("app.routers.query.execute_clickhouse_json", rendered),
        patch("app.routers.query.metrics.record_query", record_query),
        patch("app.routers.query.log_query", AsyncMock()),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "analytics", "sql": "EXPLAIN SELECT 1", "render": "database"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    rendered.assert_awaited_once()
    record_query.assert_called_once()
    kwargs = record_query.call_args.kwargs
    assert (kwargs["status"], kwargs["row_count"], kwargs["db_type"]) == ("success", 1, "clickhouse")
    assert kwargs["duration_seconds"] == pytest.approx(0.003)


async def test_render_database_needs_clickhouse_25_11(client, admin_token):
    ch = MagicMock()
    ch.min_version.return_value = False
    rendered = AsyncMock()
    service_path = AsyncMock(
        return_value=QueryResponse(columns=["v"], rows=[[1]], row_count=1, truncated=False, elapsed_ms=1)
    )
    with (
        patch("app.routers.query.connection_manager.get_db_type", return_value="clickhouse"),
        patch("app.routers.query.connection_manager.get_clickhouse_client", return_value=ch),
        patch("app.routers.query.connection_manager.get_clickhouse_lock", return_value=MagicMock()),
        patch("app.routers.query.execute_clickhouse_json", rendered),
        patch("app.routers.query.execute_clickhouse_query", service_path),
        patch("app.routers.query.log_query", AsyncMock()),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "analytics", "sql": "SELECT 1 AS v", "render": "database"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    ch.min_version.assert_called_once_with("25.11")
    rendered.assert_not_awaited()
    service_path.assert_awaited_once()


async def test_render_database_timeout_is_a_408(client, admin_token):
    db_type, ch_client, ch_lock = _clickhouse_alias()
    with (
        db_type, ch_client, ch_lock,
        patch(
            "app.routers.query.execute_clickhouse_json",
            AsyncMock(side_effect=asyncio.TimeoutError),
        ),
        patch("app.routers.query.log_query", AsyncMock()),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "analytics", "sql": "SELECT v FROM t", "render": "database"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 408


async def test_render_database_leaves_clickhouse_writes_on_the_service_path(client, admin_token):
    rendered = AsyncMock()
    service_path = AsyncMock(
        return_value=QueryResponse(columns=[], rows=[], row_count=0, truncated=False, elapsed_ms=1)
    )
    db_type, ch_client, ch_lock = _clickhouse_alias()
    with (
        db_type, ch_client, ch_lock,
        patch("app.routers.query.execute_clickhouse_json", rendered),
        patch("app.routers.query.execute_clickhouse_query", service_path),
        patch("app.routers.query.log_query", AsyncMock()),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "analytics", "sql": "INSERT INTO t VALUES (1)", "render": "database"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    rendered.assert_not_awaited()
    service_path.assert_awaited_once()


async def test_render_database_is_ignored_for_other_databases(client, admin_token):
    rendered = AsyncMock()
    service_path = AsyncMock(
        return_value=QueryResponse(columns=["v"], rows=[[1]], row_count=1, truncated=False, elapsed_ms=1)
    )
    with (
        patch("app.routers.query.connection_manager.get_db_type", return_value="postgres"),
        patch("app.routers.query.connection_manager.get_engine", return_value=MagicMock()),
        patch("app.routers.query.execute_clickhouse_json", rendered),
        patch("app.routers.query.execute_query", service_path),
        patch("app.routers.query.log_query", AsyncMock()),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "main", "sql": "SELECT 1 AS v", "render": "database"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [[1]]
    rendered.assert_not_awaited()


async def test_render_rejects_unknown_values(client, admin_token):
    response = await client.post(
        "/query/execute",
        json={"database": "analytics", "sql": "SELECT 1", "render": "fast"},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 422


async def test_template_execution_passes_render_through(monkeypatch):
    monkeypatch.setattr(query, "_require_template_read_access", AsyncMock())
    template = SimpleNamespace(
        path="reports/daily", enabled=True, db_alias="analytics",
        sql="SELECT v FROM t", default_limit=50, timeout=30,
    )
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: template)
    )
    execute = AsyncMock(return_value="sent")
    monkeypatch.setattr(query, "execute", execute)

    result = await query.execute_template(
        "reports/daily",
        QueryTemplateExecuteRequest(render="database"),
        user=CurrentUser(username="admin", role="admin"),
        db=db,
    )

    assert result == "sent"
    request = execute.await_args.args[0]
    assert (request.render, request.sql, request.limit) == ("database", "SELECT v FROM t", 50)
