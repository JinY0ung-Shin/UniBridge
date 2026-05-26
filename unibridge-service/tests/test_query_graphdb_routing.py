"""End-to-end routing tests for the GraphDB path of POST /query/execute."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import httpx
import pytest

from app.schemas import QueryResponse
from tests.test_admin import _cm_patch, auth_header


SELECT_RESPONSE = {
    "head": {"vars": ["s"]},
    "results": {"bindings": [{"s": {"type": "uri", "value": "http://ex/a"}}]},
}

CONSTRUCT_RESPONSE = "<a> <b> <c> ."


def _graphdb_mock_client():
    def handler(request):
        accept = request.headers.get("Accept", "")
        if "application/sparql-results+json" in accept:
            sparql = request.content.decode("utf-8") if request.content else ""
            if sparql.strip().lower().startswith("ask"):
                return httpx.Response(200, json={"head": {}, "boolean": True})
            return httpx.Response(200, json=SELECT_RESPONSE)
        if "text/turtle" in accept:
            return httpx.Response(200, text=CONSTRUCT_RESPONSE)
        return httpx.Response(415)
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(base_url="http://gdb:7200", transport=transport)


def _make_graphdb_payload(**overrides):
    payload = {
        "alias": "kg-route",
        "db_type": "graphdb",
        "host": "graphdb.local",
        "port": 7200,
        "database": "my-repo",
        "username": "admin",
        "password": "pw",
        "protocol": "http",
    }
    payload.update(overrides)
    return payload


async def _register_graphdb(client, admin_token, alias="kg-route"):
    """Helper: create a graphdb connection via the admin API for testing."""
    resp = await client.post(
        "/admin/query/databases",
        json=_make_graphdb_payload(alias=alias),
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text


def _patch_query_cm():
    """Patch the connection_manager used by app.routers.query for graphdb dispatch."""
    return patch.multiple(
        "app.routers.query.connection_manager",
        get_db_type=lambda alias: "graphdb",
        get_graphdb_client=lambda alias: _graphdb_mock_client(),
        get_database_name=lambda alias: "my-repo",
        update_pool_metrics=lambda alias: None,
    )


def _patch_query_cm_db_type_only():
    """Patch only get_db_type to graphdb so the alias-existence check passes
    (used by tests that don't actually reach the executor)."""
    return patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="graphdb",
    )


@pytest.mark.asyncio
async def test_select_via_admin_token_returns_columns(client, admin_token):
    """Admin role executes SELECT against graphdb alias and gets bindings."""
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-select")
        with _patch_query_cm():
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-select",
                    "sql": "SELECT ?s WHERE { ?s ?p ?o }",
                },
                headers=auth_header(admin_token),
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["columns"] == ["s"]
    assert data["rows"] == [["http://ex/a"]]


@pytest.mark.asyncio
async def test_ask_via_admin_token_returns_boolean(client, admin_token):
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-ask")
        with _patch_query_cm():
            resp = await client.post(
                "/query/execute",
                json={"database": "kg-ask", "sql": "ASK { ?s ?p ?o }"},
                headers=auth_header(admin_token),
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["columns"] == ["boolean"]
    assert data["rows"] == [[True]]


@pytest.mark.asyncio
async def test_construct_via_admin_token_returns_graph(client, admin_token):
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-construct")
        with _patch_query_cm():
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-construct",
                    "sql": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
                },
                headers=auth_header(admin_token),
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["graph"] is not None
    assert data["columns"] == []
    assert data["rows"] == []


@pytest.mark.asyncio
async def test_insert_rejected_with_422(client, admin_token):
    """Write SPARQL must be rejected by statement-type detector before reaching upstream."""
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-insert")
        with _patch_query_cm_db_type_only():
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-insert",
                    "sql": "INSERT DATA { <a> <b> <c> }",
                },
                headers=auth_header(admin_token),
            )
    assert resp.status_code == 422, resp.text

    audit_resp = await client.get(
        "/admin/query/audit-logs",
        params={"database": "kg-insert"},
        headers=auth_header(admin_token),
    )
    assert audit_resp.status_code == 200, audit_resp.text
    logs = audit_resp.json()
    assert len(logs) == 1
    assert logs[0]["status"] == "error"
    assert "Unsupported SPARQL statement" in logs[0]["error_message"]


@pytest.mark.asyncio
async def test_write_sparql_template_returns_read_only_400(client, admin_token):
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-template")
        resp = await client.post(
            "/admin/query/templates",
            json={
                "path": "kg/write",
                "name": "KG write",
                "database": "kg-template",
                "sql": "INSERT DATA { <a> <b> <c> }",
            },
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 400, resp.text
    assert "read-only" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_non_empty_params_rejected_with_422(client, admin_token):
    """GraphDB does not support bind parameters."""
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-params")
        with _patch_query_cm_db_type_only():
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-params",
                    "sql": "SELECT ?s WHERE { ?s ?p ?o }",
                    "params": {"x": 1},
                },
                headers=auth_header(admin_token),
            )
    assert resp.status_code == 422, resp.text
    assert "bind parameters" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_empty_params_allowed(client, admin_token):
    """Empty dict params is accepted (consistent with other backends)."""
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-empty-params")
        with _patch_query_cm():
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-empty-params",
                    "sql": "SELECT ?s WHERE { ?s ?p ?o }",
                    "params": {},
                },
                headers=auth_header(admin_token),
            )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_validate_sql_skipped_for_graphdb(client, admin_token):
    """A SPARQL query that would trip the sqlglot blacklist if applied must pass for graphdb."""
    # First confirm the SPARQL string would fail validate_sql on a SQL backend:
    from app.services.sql_validator import validate_sql
    sparql_with_blacklisted_kw = (
        "SELECT ?x WHERE { ?x <http://example.org/p> ?o . FILTER(?x = ?GRANT) }"
    )
    # Sanity: this would be blocked if validate_sql ran on it
    # (?GRANT is a SPARQL variable — the bare token "GRANT" matches the blacklist).
    assert validate_sql(sparql_with_blacklisted_kw) is not None, (
        "Precondition failed: test string must trip the sqlglot blacklist"
    )

    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-grant")
        with _patch_query_cm():
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-grant",
                    "sql": sparql_with_blacklisted_kw,
                },
                headers=auth_header(admin_token),
            )
    # Must pass — the validate_sql skip for graphdb is the only thing preventing 403.
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_extra_blocked_keywords_apply_to_graphdb(client, admin_token, monkeypatch):
    monkeypatch.setattr(
        "app.routers.query.settings_manager.blocked_sql_keywords",
        ["credit_card"],
    )

    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-blocked")
        with _patch_query_cm_db_type_only():
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-blocked",
                    "sql": (
                        "SELECT ?credit_card WHERE { "
                        "?s <http://ex/credit_card> ?credit_card }"
                    ),
                },
                headers=auth_header(admin_token),
            )

    assert resp.status_code == 403, resp.text
    assert "credit_card" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_graphdb_executor_http_exception_status_is_preserved(client, admin_token):
    """Executor HTTPException statuses must not be collapsed into generic 400."""
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-too-large")
        with _patch_query_cm(), patch(
            "app.routers.query.execute_graphdb_query",
            AsyncMock(side_effect=HTTPException(status_code=413, detail="too large")),
        ):
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-too-large",
                    "sql": "DESCRIBE <http://ex/x>",
                },
                headers=auth_header(admin_token),
            )
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"] == "too large"


@pytest.mark.asyncio
async def test_graphdb_executor_504_records_timeout(client, admin_token):
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-slow")
        with _patch_query_cm(), patch(
            "app.routers.query.execute_graphdb_query",
            AsyncMock(
                side_effect=HTTPException(
                    status_code=504,
                    detail="GraphDB query timed out",
                )
            ),
        ), patch("app.routers.query.metrics.record_query") as record_query:
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-slow",
                    "sql": "SELECT ?s WHERE { ?s ?p ?o }",
                },
                headers=auth_header(admin_token),
            )

    assert resp.status_code == 504, resp.text
    assert any(
        call.kwargs["db_alias"] == "kg-slow"
        and call.kwargs["status"] == "timeout"
        for call in record_query.call_args_list
    )
    audit_resp = await client.get(
        "/admin/query/audit-logs",
        params={"database": "kg-slow"},
        headers=auth_header(admin_token),
    )
    assert audit_resp.status_code == 200, audit_resp.text
    logs = audit_resp.json()
    assert len(logs) == 1
    assert logs[0]["status"] == "timeout"


@pytest.mark.asyncio
async def test_timeout_override_forwarded_to_graphdb_executor(client, admin_token):
    execute_mock = AsyncMock(
        return_value=QueryResponse(
            columns=["s"],
            rows=[["http://ex/a"]],
            row_count=1,
            truncated=False,
            elapsed_ms=1,
        )
    )
    with _cm_patch("graphdb"):
        await _register_graphdb(client, admin_token, "kg-timeout")
        with _patch_query_cm(), patch(
            "app.routers.query.execute_graphdb_query",
            execute_mock,
        ):
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "kg-timeout",
                    "sql": "SELECT ?s WHERE { ?s ?p ?o }",
                    "timeout": 7,
                },
                headers=auth_header(admin_token),
            )
    assert resp.status_code == 200, resp.text
    assert execute_mock.await_args.kwargs["timeout"] == 7
