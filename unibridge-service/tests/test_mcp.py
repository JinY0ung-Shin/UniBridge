from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AdminAuditLog, ApiKeyAccess, DBConnection, QueryTemplate
from app.schemas import QueryResponse
from app.services import mcp_tools
from tests.conftest import auth_header

ALL_ROUTES = ["mcp-api", "query-api", "s3-api", "nas-api"]


class FakeGateway(httpx.AsyncBaseTransport):
    """APISIX stand-in for the tool calls MCP sends back out.

    key-auth: ``apikey: key:<consumer>`` authenticates as <consumer>; the
    route prefixes are rewritten like the fixed routes' proxy-rewrite, and each
    call is recorded with the route APISIX would count it on.
    """

    _ROUTES = (
        (b"/api/query/", b"/query/", "query-api"),
        (b"/api/s3/", b"/s3/", "s3-api"),
        (b"/api/nas/", b"/nas/", "nas-api"),
    )

    def __init__(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.calls: list[tuple[str, str, str]] = []
        self.reply: httpx.Response | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("apikey", "")
        if not key.startswith("key:"):
            return httpx.Response(401, json={"message": "Invalid API key in request"})
        raw = request.url.raw_path
        for prefix, target, route in self._ROUTES:
            if raw.startswith(prefix):
                self.calls.append((route, request.method, request.url.path))
                break
        else:
            return httpx.Response(404, json={"error_msg": "404 Route Not Found"})
        if self.reply is not None:
            return self.reply
        headers = {k: v for k, v in request.headers.items() if k != "apikey"}
        headers["x-consumer-username"] = key.removeprefix("key:")
        forwarded = httpx.Request(
            request.method,
            request.url.copy_with(raw_path=target + raw[len(prefix):]),
            headers=headers,
            content=await request.aread(),
        )
        return await self.inner.handle_async_request(forwarded)


@pytest.fixture
async def gateway(app):
    fake = FakeGateway(app)
    async with httpx.AsyncClient(transport=fake, base_url="http://apisix:9080") as client:
        with patch("app.services.mcp_tools._gateway_client", return_value=client):
            yield fake


async def _create_api_key(
    seeded_db,
    name: str = "agent",
    *,
    routes: list[str] | None = None,
    databases: list[str] | None = None,
    tables: list[str] | None = None,
    allow_insert: bool = False,
) -> dict[str, str]:
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(
            ApiKeyAccess(
                consumer_name=name,
                allowed_databases=json.dumps(["maindb"] if databases is None else databases),
                allowed_routes=json.dumps(ALL_ROUTES if routes is None else routes),
                allowed_tables=json.dumps(tables) if tables is not None else None,
                allow_insert=allow_insert,
            )
        )
        await db.commit()
    # What APISIX hands the app on /api/mcp: the consumer it authenticated
    # (trusted without the proxy secret in dev-token mode) and the key itself.
    return {"X-Consumer-Username": name, "apikey": f"key:{name}"}


async def _create_template(
    seeded_db,
    path: str = "reports/users",
    sql: str = "SELECT id, name FROM users WHERE id = :id",
    default_limit: int | None = 50,
    name: str = "Users report",
    db_alias: str = "maindb",
    enabled: bool = True,
) -> None:
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(
            QueryTemplate(
                path=path,
                name=name,
                description="One user by id",
                db_alias=db_alias,
                enabled=enabled,
                sql=sql,
                default_limit=default_limit,
            )
        )
        if (
            await db.execute(select(DBConnection).where(DBConnection.alias == db_alias))
        ).scalar_one_or_none() is None:
            db.add(
                DBConnection(
                    alias=db_alias,
                    db_type="postgres",
                    host="localhost",
                    port=5432,
                    database="app",
                    username="u",
                    password_encrypted="x",
                )
            )
        await db.commit()


async def _rpc(client, headers, method: str, params: dict | None = None, request_id: Any = 1):
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return await client.post("/mcp", json=message, headers=headers)


async def _tool_names(client, headers) -> list[str]:
    resp = await _rpc(client, headers, "tools/list")
    assert resp.status_code == 200, resp.text
    return [tool["name"] for tool in resp.json()["result"]["tools"]]


async def _call(client, headers, name: str, arguments: dict) -> dict:
    resp = await _rpc(client, headers, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _text(result: dict, index: int = 0) -> str:
    return result["content"][index]["text"]


def _query_response(rows: list[list[Any]], truncated: bool = False) -> QueryResponse:
    return QueryResponse(
        columns=["id", "name"],
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=3,
    )


@contextmanager
def _postgres(response: QueryResponse | None = None, db_type: str = "postgres"):
    with patch(
        "app.routers.query.connection_manager.get_db_type", return_value=db_type
    ), patch(
        "app.services.mcp_tools.connection_manager.list_aliases",
        return_value=["maindb", "otherdb"],
    ), patch(
        "app.routers.query.connection_manager.get_engine", return_value=MagicMock()
    ), patch(
        "app.routers.query.execute_query",
        new_callable=AsyncMock,
        return_value=response or _query_response([[1, "alice"]]),
    ) as execute_query, patch("app.routers.query.log_query", new_callable=AsyncMock):
        yield execute_query


async def _put_settings(client, admin_token, **changes) -> None:
    body = mcp_tools.McpSettings().model_dump()
    for key, value in changes.items():
        if key in body["tool_groups"]:
            body["tool_groups"][key] = value
        else:
            body[key] = value
    resp = await client.put("/admin/mcp/settings", json=body, headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text


# ── Protocol ────────────────────────────────────────────────────────────────


async def test_initialize_negotiates_the_protocol_version(client, seeded_db):
    headers = await _create_api_key(seeded_db)

    resp = await _rpc(client, headers, "initialize", {"protocolVersion": "2025-06-18"})
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "unibridge"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "run_query" in result["instructions"]
    assert "mcp-session-id" not in resp.headers

    resp = await _rpc(client, headers, "initialize", {"protocolVersion": "1999-01-01"})
    assert resp.json()["result"]["protocolVersion"] == "2025-11-25"


async def test_notifications_and_batches(client, seeded_db):
    headers = await _create_api_key(seeded_db)

    resp = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )
    assert resp.status_code == 202
    assert resp.content == b""

    resp = await client.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
            {"jsonrpc": "2.0", "id": 8, "method": "resources/list"},
        ],
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == [
        {"jsonrpc": "2.0", "id": 7, "result": {}},
        {"jsonrpc": "2.0", "id": 8, "error": {"code": -32601, "message": "Method not found: resources/list"}},
    ]


async def test_batches_are_capped(client, seeded_db):
    headers = await _create_api_key(seeded_db)
    batch = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(17)]
    resp = await client.post("/mcp", json=batch, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32600
    resp = await client.post("/mcp", json=batch[:16], headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 16


async def test_rejects_bad_json_and_unknown_protocol_header(client, seeded_db):
    headers = await _create_api_key(seeded_db)

    resp = await client.post("/mcp", content=b"{not json", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700

    resp = await _rpc(client, {**headers, "MCP-Protocol-Version": "1999-01-01"}, "ping")
    assert resp.status_code == 400


async def test_get_and_delete_are_not_supported(client, seeded_db):
    headers = await _create_api_key(seeded_db)
    for method in ("GET", "DELETE"):
        resp = await client.request(method, "/mcp", headers=headers)
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST"


# ── Access ──────────────────────────────────────────────────────────────────


async def test_jwt_users_are_refused(client, admin_token):
    resp = await _rpc(client, auth_header(admin_token), "ping")
    assert resp.status_code == 403
    assert "API key" in resp.json()["detail"]


async def test_key_needs_the_mcp_route_grant(client, seeded_db):
    headers = await _create_api_key(seeded_db, routes=["query-api"])
    resp = await _rpc(client, headers, "ping")
    assert resp.status_code == 403
    assert "mcp-api" in resp.json()["detail"]

    headers = await _create_api_key(seeded_db, "master", routes=["*"], databases=["*"])
    assert (await _rpc(client, headers, "ping")).status_code == 200


async def test_disabled_endpoint_refuses_every_key(client, seeded_db, admin_token):
    headers = await _create_api_key(seeded_db)
    await _put_settings(client, admin_token, enabled=False)
    resp = await _rpc(client, headers, "ping")
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"]


async def test_tools_follow_route_grants_and_groups(client, seeded_db, admin_token, gateway):
    await _create_template(seeded_db)
    full = await _create_api_key(seeded_db)
    s3_only = await _create_api_key(seeded_db, "s3-only", routes=["mcp-api", "s3-api"])

    with _postgres():
        names = await _tool_names(client, full)
        assert names[:4] == ["list_databases", "list_tables", "describe_table", "run_query"]
        assert "s3_read_object" in names and "nas_read_file" in names
        # One search tool and one run tool, however many templates there are.
        assert names[-2:] == ["search_templates", "run_template"]
        assert all(name.startswith("s3_") for name in await _tool_names(client, s3_only))

        await _put_settings(client, admin_token, run_query=False, templates=False, nas=False)
        names = await _tool_names(client, full)
    assert "run_query" not in names
    assert "search_templates" not in names and "run_template" not in names
    assert not any(name.startswith("nas_") for name in names)
    assert "list_tables" in names
    # Discovery is answered locally: nothing went through the gateway.
    assert gateway.calls == []


async def test_unknown_tool_is_a_protocol_error(client, seeded_db):
    headers = await _create_api_key(seeded_db, routes=["mcp-api"])
    resp = await _rpc(client, headers, "tools/call", {"name": "run_query", "arguments": {}})
    assert resp.json()["error"]["code"] == -32602


async def test_bad_arguments_are_a_tool_error(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db, databases=["maindb", "store"])
    with _postgres():
        result = await _call(client, headers, "run_query", {"database": "maindb"})
        assert result["isError"] is True
        assert "sql" in _text(result)
        result = await _call(client, headers, "run_query", {"database": "maindb", "sql": 1})
        assert "must be of type string" in _text(result)
        result = await _call(client, headers, "list_databases", {"extra": True})
        assert "Unknown argument" in _text(result)
    result = await _call(
        client, headers, "s3_get_object_metadata", {"connection": "store", "bucket": None, "key": "k"}
    )
    assert "'bucket' must be of type string" in _text(result)
    assert gateway.calls == []


async def test_tool_calls_need_the_forwarded_key(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db)
    del headers["apikey"]
    with _postgres() as execute_query:
        result = await _call(client, headers, "run_query", {"database": "maindb", "sql": "SELECT 1"})
    assert result["isError"] is True
    assert "apikey" in _text(result)
    execute_query.assert_not_awaited()


async def test_gateway_refusals_reach_the_model(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db)
    gateway.reply = httpx.Response(429, json={"error_msg": "Requests are too frequent"})
    with _postgres():
        result = await _call(client, headers, "run_query", {"database": "maindb", "sql": "SELECT 1"})
    assert result["isError"] is True
    assert _text(result) == "429: Requests are too frequent"


# ── Counting: tool calls land on the REST routes ────────────────────────────


async def test_tool_calls_go_out_on_their_rest_routes(client, seeded_db, gateway):
    await _create_template(seeded_db)
    headers = await _create_api_key(seeded_db, databases=["maindb", "store", "share"])
    with _postgres(), patch(
        "app.services.schema_browser.list_tables", new_callable=AsyncMock, return_value=["users"]
    ):
        await _call(client, headers, "list_tables", {"database": "maindb"})
        await _call(client, headers, "run_query", {"database": "maindb", "sql": "SELECT 1"})
        await _call(client, headers, "search_templates", {})
        await _call(client, headers, "run_template", {"template": "reports/users", "params": {"id": 1}})
    with patch("app.routers.s3.s3_manager.has_connection", return_value=True), patch(
        "app.routers.s3.s3_manager.list_buckets", new_callable=AsyncMock, return_value=[]
    ), patch("app.routers.s3.s3_manager.allowed_buckets", return_value=None):
        result = await _call(client, headers, "s3_list_buckets", {"connection": "store"})
        assert result["isError"] is False, result
    with patch("app.routers.nas.nas_manager.has_connection", return_value=True), patch(
        "app.routers.nas.nas_manager.stat_path", new_callable=AsyncMock, return_value={"type": "file"}
    ):
        result = await _call(client, headers, "nas_get_metadata", {"connection": "share", "path": "a.txt"})
        assert result["isError"] is False, result

    assert gateway.calls == [
        ("query-api", "GET", "/api/query/databases/maindb/tables"),
        ("query-api", "POST", "/api/query/execute"),
        ("query-api", "POST", "/api/query/templates/reports/users"),
        ("s3-api", "GET", "/api/s3/store/buckets"),
        ("nas-api", "GET", "/api/nas/share/metadata"),
    ]


# ── Databases ───────────────────────────────────────────────────────────────


async def test_list_databases_follows_the_key_scope(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db)
    with _postgres():
        result = await _call(client, headers, "list_databases", {})
    assert json.loads(_text(result)) == {"databases": [{"database": "maindb", "type": "postgres"}]}


async def test_list_tables_and_describe_follow_table_scope(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db, tables=["users"])
    with _postgres(), patch(
        "app.services.schema_browser.list_tables",
        new_callable=AsyncMock,
        return_value=["orders", "users"],
    ), patch(
        "app.services.schema_browser.describe_table",
        new_callable=AsyncMock,
        return_value=[{"name": "id", "type": "integer", "nullable": False}],
    ) as describe:
        result = await _call(client, headers, "list_tables", {"database": "maindb"})
        assert json.loads(_text(result))["tables"] == ["users"]

        result = await _call(
            client, headers, "describe_table", {"database": "maindb", "table": "orders"}
        )
        assert result["isError"] is True
        assert _text(result) == "403: Access denied to table: orders"
        describe.assert_not_awaited()

        result = await _call(
            client, headers, "describe_table", {"database": "maindb", "table": "public.users"}
        )
    assert result["isError"] is False
    assert json.loads(_text(result))["columns"][0]["name"] == "id"
    describe.assert_awaited_once_with("maindb", "public.users")


async def test_database_outside_the_key_scope_is_refused(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db)
    with _postgres() as execute_query:
        result = await _call(
            client, headers, "run_query", {"database": "otherdb", "sql": "SELECT 1"}
        )
    assert result["isError"] is True
    assert "otherdb" in _text(result)
    execute_query.assert_not_awaited()


async def test_run_query_refuses_what_does_not_parse_as_one_select(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db, allow_insert=True)
    with _postgres(db_type="mssql") as execute_query:
        for sql in (
            "INSERT INTO users (id) VALUES (1)",
            # A T-SQL batch without semicolons: the regex fallback calls it a SELECT.
            "SELECT 1 AS a DELETE FROM users COMMIT",
            "SELECT 1; DELETE FROM users",
            "EXPLAIN SELECT * FROM users",
        ):
            result = await _call(client, headers, "run_query", {"database": "maindb", "sql": sql})
            assert result["isError"] is True, sql
            assert "read-only" in _text(result)
        execute_query.assert_not_awaited()
        result = await _call(
            client, headers, "run_query", {"database": "maindb", "sql": "SELECT TOP 5 * FROM users"}
        )
    assert result["isError"] is False, result
    execute_query.assert_awaited_once()


async def test_run_query_caps_rows_at_max_rows(client, seeded_db, admin_token, gateway):
    headers = await _create_api_key(seeded_db)
    await _put_settings(client, admin_token, max_rows=2)
    with _postgres(_query_response([[1, "a"], [2, "b"]], truncated=True)) as execute_query:
        result = await _call(
            client,
            headers,
            "run_query",
            {"database": "maindb", "sql": "SELECT id, name FROM users WHERE id > :id", "params": {"id": 0}},
        )
    assert execute_query.await_args.kwargs["limit"] == 2
    assert execute_query.await_args.kwargs["params"] == {"id": 0}
    payload = json.loads(_text(result))
    assert payload["rows"] == [[1, "a"], [2, "b"]]
    assert payload["truncated"] is True
    assert payload["truncated_reason"] == "max_rows"


async def test_run_query_cuts_rows_at_max_result_bytes(client, seeded_db, admin_token, gateway):
    headers = await _create_api_key(seeded_db)
    await _put_settings(client, admin_token, max_result_bytes=1024)
    rows = [[i, "x" * 100] for i in range(50)]
    with _postgres(_query_response(rows)):
        result = await _call(
            client, headers, "run_query", {"database": "maindb", "sql": "SELECT id, name FROM users"}
        )
    text = _text(result)
    payload = json.loads(text)
    assert 0 < payload["row_count"] < 50
    assert payload["truncated_reason"] == "max_result_bytes"
    assert len(text.encode()) <= 1024


async def test_search_templates_finds_what_the_key_may_run(client, seeded_db, gateway):
    await _create_template(seeded_db)
    await _create_template(
        seeded_db, path="sales/daily", name="Daily sales", sql="SELECT 1", db_alias="otherdb"
    )
    headers = await _create_api_key(seeded_db, databases=["maindb", "otherdb"])
    other_key = await _create_api_key(seeded_db, "maindb-only")
    with _postgres():
        result = await _call(client, headers, "search_templates", {})
        payload = json.loads(_text(result))
        assert [t["template"] for t in payload["templates"]] == ["reports/users", "sales/daily"]
        assert payload["total"] == 2 and payload["truncated"] is False
        users = payload["templates"][0]
        assert users["params"] == ["id"]
        assert users["database"] == "maindb"
        assert "SELECT id, name FROM users" in users["sql"]

        # Every word must match path, name or description (case-insensitive).
        result = await _call(client, headers, "search_templates", {"query": "USERS report"})
        assert [t["template"] for t in json.loads(_text(result))["templates"]] == ["reports/users"]
        result = await _call(client, headers, "search_templates", {"database": "otherdb"})
        assert [t["template"] for t in json.loads(_text(result))["templates"]] == ["sales/daily"]
        result = await _call(client, headers, "search_templates", {"limit": 1})
        payload = json.loads(_text(result))
        assert len(payload["templates"]) == 1 and payload["truncated"] is True

        # A key only finds templates on databases it may use.
        result = await _call(client, other_key, "search_templates", {})
        assert [t["template"] for t in json.loads(_text(result))["templates"]] == ["reports/users"]
    assert gateway.calls == []


async def test_run_template_runs_by_path_or_name_with_the_key(client, seeded_db, gateway):
    await _create_template(seeded_db, default_limit=500)
    headers = await _create_api_key(seeded_db)
    with _postgres() as execute_query:
        tools = (await _rpc(client, headers, "tools/list")).json()["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "run_template")
        assert tool["inputSchema"]["required"] == ["template"]
        assert tool["annotations"]["readOnlyHint"] is True

        result = await _call(client, headers, "run_template", {"template": "reports/users", "params": {"id": 1}})
        assert result["isError"] is False, result
        assert json.loads(_text(result))["rows"] == [[1, "alice"]]
        kwargs = execute_query.await_args.kwargs
        assert kwargs["params"] == {"id": 1}
        # The template's default_limit (500) is clamped to max_rows (200).
        assert kwargs["limit"] == 200

        result = await _call(
            client, headers, "run_template", {"template": "users report", "params": {"id": 2}, "row_limit": 5}
        )
        assert result["isError"] is False, result
        assert execute_query.await_args.kwargs["limit"] == 5

        result = await _call(client, headers, "run_template", {"template": "reports/users"})
        assert result["isError"] is True and "Missing template param(s): id" in _text(result)
        result = await _call(
            client, headers, "run_template", {"template": "reports/users", "params": {"id": 1, "x": 2}}
        )
        assert "Unknown template param(s): x" in _text(result)
        result = await _call(client, headers, "run_template", {"template": "nope"})
        assert "search_templates" in _text(result)
    assert len(gateway.calls) == 2


async def test_run_template_asks_for_a_path_when_names_collide(client, seeded_db, gateway):
    await _create_template(seeded_db, path="a/users")
    await _create_template(seeded_db, path="b/users")
    headers = await _create_api_key(seeded_db)
    with _postgres():
        result = await _call(client, headers, "run_template", {"template": "Users report", "params": {"id": 1}})
    assert result["isError"] is True
    assert "a/users, b/users" in _text(result)
    assert gateway.calls == []


async def test_excluded_templates_are_hidden_from_mcp(client, seeded_db, admin_token, gateway):
    await _create_template(seeded_db)
    await _create_template(seeded_db, path="sales/daily", name="Daily sales", sql="SELECT 1")
    headers = await _create_api_key(seeded_db)
    await _put_settings(client, admin_token, excluded_templates=[" reports/users ", "reports/users"])
    resp = await client.get("/admin/mcp/settings", headers=auth_header(admin_token))
    assert resp.json()["excluded_templates"] == ["reports/users"]

    with _postgres():
        result = await _call(client, headers, "search_templates", {})
        assert [t["template"] for t in json.loads(_text(result))["templates"]] == ["sales/daily"]
        result = await _call(client, headers, "run_template", {"template": "reports/users", "params": {"id": 1}})
    assert result["isError"] is True
    assert gateway.calls == []


async def test_search_templates_keeps_whole_entries_within_max_result_bytes(
    client, seeded_db, admin_token, gateway
):
    for index in range(10):
        await _create_template(
            seeded_db, path=f"big/t{index}", sql="SELECT " + ", ".join(f"c{i}" for i in range(300))
        )
    headers = await _create_api_key(seeded_db)
    await _put_settings(client, admin_token, max_result_bytes=4096)
    with _postgres():
        result = await _call(client, headers, "search_templates", {})
    text = _text(result)
    payload = json.loads(text)  # still valid JSON: entries are dropped, not cut
    assert 0 < len(payload["templates"]) < 10
    assert payload["total"] == 10 and payload["truncated"] is True
    assert len(text.encode()) <= 4096


# ── S3 / NAS ────────────────────────────────────────────────────────────────


@contextmanager
def _s3_object(body: bytes, content_type: str = "text/plain"):
    with patch("app.routers.s3.s3_manager.has_connection", return_value=True), patch(
        "app.routers.s3.s3_manager.allowed_buckets", return_value=None
    ), patch(
        "app.routers.s3.s3_manager.get_object_metadata",
        new_callable=AsyncMock,
        return_value={"size": len(body), "content_type": content_type},
    ), patch(
        "app.routers.s3.s3_manager.read_object_prefix",
        new_callable=AsyncMock,
        side_effect=lambda alias, bucket, key, n: body[:n],
    ) as read_prefix:
        yield read_prefix


async def test_s3_read_object_returns_text_and_marks_truncation(
    client, seeded_db, admin_token, gateway
):
    headers = await _create_api_key(seeded_db, databases=["store"])
    await _put_settings(client, admin_token, max_result_bytes=1024)
    # 1023 ASCII bytes then a 3-byte character cut by the 1024-byte cap.
    with _s3_object(("a" * 1023 + "가나").encode()) as read_prefix:
        result = await _call(
            client, headers, "s3_read_object", {"connection": "store", "bucket": "b", "key": "k.txt"}
        )
    read_prefix.assert_awaited_once_with("store", "b", "k.txt", 1024)
    assert result["isError"] is False, result
    meta_text, text = _text(result, 0), _text(result, 1)
    meta = json.loads(meta_text)
    assert meta["truncated"] is True and meta["size"] == 1029
    # Metadata and text together stay within max_result_bytes, marker included.
    assert len(meta_text.encode()) + len(text.encode()) <= 1024
    assert text.startswith("a" * 500) and text.endswith("[truncated at max_result_bytes]")


async def test_s3_refuses_binary_and_foreign_connections(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db, databases=["store"])
    with _s3_object(b"\x89PNG\x00\x00", "image/png"):
        result = await _call(
            client, headers, "s3_read_object", {"connection": "store", "bucket": "b", "key": "k"}
        )
    assert result["isError"] is True
    assert "UTF-8" in _text(result)

    result = await _call(client, headers, "s3_list_buckets", {"connection": "elsewhere"})
    assert result["isError"] is True
    assert "elsewhere" in _text(result)


async def test_s3_list_objects_drops_null_optionals(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db, databases=["store"])
    with patch("app.routers.s3.s3_manager.has_connection", return_value=True), patch(
        "app.routers.s3.s3_manager.allowed_buckets", return_value=None
    ), patch(
        "app.routers.s3.s3_manager.list_objects",
        new_callable=AsyncMock,
        return_value={"folders": [], "objects": []},
    ) as list_objects:
        result = await _call(
            client,
            headers,
            "s3_list_objects",
            {"connection": "store", "bucket": "b", "continuation_token": None},
        )
    assert result["isError"] is False, result
    list_objects.assert_awaited_once_with("store", "b", "", "/", 200, None)


async def test_nas_read_file_reads_the_beginning(client, seeded_db, gateway):
    headers = await _create_api_key(seeded_db, databases=["share"])

    async def chunks():
        yield b"hello "
        yield b"world"

    with patch("app.routers.nas.nas_manager.has_connection", return_value=True), patch(
        "app.routers.nas.nas_manager.open_read_stream",
        new_callable=AsyncMock,
        return_value=(chunks(), {"size": 11, "content_type": "text/plain", "filename": "a.txt"}),
    ) as open_stream:
        result = await _call(
            client, headers, "nas_read_file", {"connection": "share", "path": "docs/a.txt"}
        )
    open_stream.assert_awaited_once_with("share", "docs/a.txt", length=65_536)
    assert result["isError"] is False, result
    assert _text(result, 1) == "hello world"
    assert json.loads(_text(result, 0))["truncated"] is False


# ── REST endpoints the tools use ────────────────────────────────────────────


async def test_table_listing_endpoint_scopes_jwt_users(client, admin_token, user_token):
    with _postgres(), patch(
        "app.services.schema_browser.list_tables", new_callable=AsyncMock, return_value=["users"]
    ):
        resp = await client.get("/query/databases/maindb/tables", headers=auth_header(admin_token))
        assert resp.json() == {"database": "maindb", "tables": ["users"]}
        resp = await client.get("/query/databases/maindb/tables", headers=auth_header(user_token))
    assert resp.status_code == 403


async def test_catalog_queries_time_out(client, admin_token):
    async def hang(alias):
        await asyncio.sleep(5)

    with _postgres(), patch("app.routers.query.SCHEMA_QUERY_TIMEOUT_SECONDS", 0.05), patch(
        "app.services.schema_browser.list_tables", side_effect=hang
    ):
        resp = await client.get("/query/databases/maindb/tables", headers=auth_header(admin_token))
    assert resp.status_code == 408


async def test_downloads_return_only_the_first_max_bytes(client, admin_token):
    with _s3_object(b"0123456789") as read_prefix:
        resp = await client.get(
            "/s3/store/objects/download",
            params={"bucket": "b", "key": "k.txt", "max_bytes": 4},
            headers=auth_header(admin_token),
        )
    assert resp.content == b"0123"
    assert resp.headers["x-object-size"] == "10"
    read_prefix.assert_awaited_once_with("store", "b", "k.txt", 4)


def test_graph_results_are_cut_as_valid_json():
    cfg = mcp_tools.McpSettings(max_result_bytes=1024)
    data = {
        "columns": [], "rows": [], "row_count": 0, "truncated": False, "elapsed_ms": 1,
        "graph": '<a> <b> "c\\n" .\n' * 500,
    }
    payload = mcp_tools._query_payload("kg", data, cfg)
    assert payload["truncated"] is True
    assert payload["truncated_reason"] == "max_result_bytes"
    assert len(mcp_tools._dumps(payload).encode()) <= 1024


def test_capped_text_stays_within_the_cap():
    text = mcp_tools._capped_text("가" * 1000, 100)
    assert len(text.encode()) <= 100
    assert text.endswith("[truncated at max_result_bytes]")


# ── Admin ───────────────────────────────────────────────────────────────────


async def test_admin_settings_round_trip_and_audit(client, admin_token, seeded_db):
    resp = await client.get("/admin/mcp/settings", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == mcp_tools.McpSettings().model_dump()

    await _put_settings(client, admin_token, max_rows=10, s3=False)
    resp = await client.get("/admin/mcp/settings", headers=auth_header(admin_token))
    assert resp.json()["max_rows"] == 10
    assert resp.json()["tool_groups"]["s3"] is False

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        entry = (
            await db.execute(select(AdminAuditLog).where(AdminAuditLog.resource_type == "mcp_settings"))
        ).scalar_one()
    assert entry.action == "update"


async def test_admin_settings_validate_and_need_write_permission(client, admin_token, user_token):
    body = mcp_tools.McpSettings().model_dump()
    resp = await client.put(
        "/admin/mcp/settings", json={**body, "max_rows": 0}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422
    resp = await client.put("/admin/mcp/settings", json=body, headers=auth_header(user_token))
    assert resp.status_code == 403


async def test_admin_preview_shows_what_a_key_would_get(client, admin_token, seeded_db):
    await _create_template(seeded_db)
    await _create_template(seeded_db, path="hidden/one", enabled=False)
    await _create_api_key(seeded_db, routes=["query-api", "s3-api"])
    with _postgres():
        resp = await client.get(
            "/admin/mcp/preview", params={"consumer": "agent"}, headers=auth_header(admin_token)
        )
    assert resp.status_code == 200
    preview = resp.json()
    assert preview["has_mcp_grant"] is False
    assert preview["mcp_enabled"] is True
    groups = {group["group"]: group for group in preview["groups"]}
    assert groups["nas"] == {"group": "nas", "enabled": True, "granted": False, "required_route": "nas-api"}
    tool_groups = {tool["group"] for tool in preview["tools"]}
    assert tool_groups == {"db_explore", "run_query", "s3", "templates"}
    assert preview["templates"] == [
        {"path": "reports/users", "name": "Users report", "database": "maindb", "enabled": True}
    ]

    resp = await client.get(
        "/admin/mcp/preview", params={"consumer": "nobody"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 404


async def test_admin_template_list_includes_disabled_ones(client, admin_token, user_token, seeded_db):
    await _create_template(seeded_db)
    await _create_template(seeded_db, path="hidden/one", enabled=False)
    resp = await client.get("/admin/mcp/templates", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == [
        {"path": "hidden/one", "name": "Users report", "database": "maindb", "enabled": False},
        {"path": "reports/users", "name": "Users report", "database": "maindb", "enabled": True},
    ]
    resp = await client.get("/admin/mcp/templates", headers=auth_header(user_token))
    assert resp.status_code == 403
