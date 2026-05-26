"""End-to-end routing tests for the GraphDB path of POST /query/execute."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

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
