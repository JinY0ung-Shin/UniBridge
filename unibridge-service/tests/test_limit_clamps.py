"""MAX_ROW_LIMIT clamps outside the SQLAlchemy execution path.

``execute_query`` clamps the effective row limit to ``settings.MAX_ROW_LIMIT``;
these tests cover the paths that reach a backend some other way (ClickHouse,
Neo4j, GraphDB) and the stored query-template limits, which bypass
``QueryRequest``'s own field bound because they are read back from the DB.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models import QueryTemplate
from app.schemas import (
    QueryResponse,
    QueryTemplateAgentCreate,
    QueryTemplateAgentUpdate,
    QueryTemplateExecuteRequest,
)
from app.services import query_executor
from app.services.query_executor import execute_clickhouse_query, execute_neo4j_query
from tests.conftest import auth_header
from tests.test_admin import _cm_patch


ABOVE_MAX = settings.MAX_ROW_LIMIT + 5_000


def _mock_query_response() -> QueryResponse:
    return QueryResponse(
        columns=["id"], rows=[[1]], row_count=1, truncated=False, elapsed_ms=1,
    )


# ── ClickHouse ───────────────────────────────────────────────────────────────


def _clickhouse_client() -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.column_names = ["id"]
    result.result_rows = [(1,)]
    client.query.return_value = result
    return client


async def test_clickhouse_caller_limit_is_clamped():
    client = _clickhouse_client()

    await execute_clickhouse_query(client, "SELECT id FROM t", limit=ABOVE_MAX)

    ch_settings = client.query.call_args.kwargs["settings"]
    assert ch_settings["max_result_rows"] == settings.MAX_ROW_LIMIT + 1


async def test_clickhouse_default_row_limit_is_clamped(monkeypatch):
    monkeypatch.setattr(query_executor.settings_manager, "default_row_limit", ABOVE_MAX)
    client = _clickhouse_client()

    await execute_clickhouse_query(client, "SELECT id FROM t")

    ch_settings = client.query.call_args.kwargs["settings"]
    assert ch_settings["max_result_rows"] == settings.MAX_ROW_LIMIT + 1


# ── Neo4j ────────────────────────────────────────────────────────────────────


def _capture_neo4j_limit(monkeypatch) -> dict:
    captured: dict = {}

    def fake_execute(driver, database, query, params, limit, timeout, readonly):
        captured["limit"] = limit
        return _mock_query_response()

    monkeypatch.setattr(query_executor, "_execute_neo4j_sync", fake_execute)
    return captured


async def test_neo4j_caller_limit_is_clamped(monkeypatch):
    captured = _capture_neo4j_limit(monkeypatch)

    await execute_neo4j_query(
        MagicMock(), "neo4j", "MATCH (n) RETURN n", limit=ABOVE_MAX,
    )

    assert captured["limit"] == settings.MAX_ROW_LIMIT


async def test_neo4j_default_row_limit_is_clamped(monkeypatch):
    monkeypatch.setattr(query_executor.settings_manager, "default_row_limit", ABOVE_MAX)
    captured = _capture_neo4j_limit(monkeypatch)

    await execute_neo4j_query(MagicMock(), "neo4j", "MATCH (n) RETURN n")

    assert captured["limit"] == settings.MAX_ROW_LIMIT


# ── GraphDB (clamped in the router, not the executor) ────────────────────────


async def test_graphdb_route_clamps_default_row_limit(client, admin_token, monkeypatch):
    monkeypatch.setattr(
        "app.routers.query.settings_manager.default_row_limit", ABOVE_MAX
    )
    with _cm_patch("graphdb"):
        resp = await client.post(
            "/admin/query/databases",
            json={
                "alias": "kg-limit",
                "db_type": "graphdb",
                "host": "graphdb.local",
                "port": 7200,
                "database": "my-repo",
                "username": "admin",
                "password": "pw",
                "protocol": "http",
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text

    execute_mock = AsyncMock(return_value=_mock_query_response())
    with patch.multiple(
        "app.routers.query.connection_manager",
        get_db_type=lambda alias: "graphdb",
        get_graphdb_client=lambda alias: MagicMock(),
        get_database_name=lambda alias: "my-repo",
        update_pool_metrics=lambda alias: None,
    ), patch("app.routers.query.execute_graphdb_query", execute_mock), patch(
        "app.routers.query.log_query", new_callable=AsyncMock
    ):
        resp = await client.post(
            "/query/execute",
            json={"database": "kg-limit", "sql": "SELECT ?s WHERE { ?s ?p ?o }"},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200, resp.text
    assert execute_mock.await_args.kwargs["limit"] == settings.MAX_ROW_LIMIT


# ── Query templates ──────────────────────────────────────────────────────────


async def _create_database(client, admin_token, alias: str = "tmpl-db") -> None:
    with patch(
        "app.routers.admin.connection_manager.add_connection", new_callable=AsyncMock,
    ), patch(
        "app.routers.admin.connection_manager.get_status",
        return_value={"status": "registered"},
    ):
        resp = await client.post(
            "/admin/query/databases",
            json={
                "alias": alias,
                "db_type": "postgres",
                "host": "localhost",
                "port": 5432,
                "database": "app",
                "username": "user",
                "password": "pass",
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text


async def _create_template(
    client, admin_token, *, path: str, default_limit: int | None = 50,
) -> None:
    resp = await client.post(
        "/admin/query/templates",
        json={
            "path": path,
            "name": "Report",
            "database": "tmpl-db",
            "sql": "SELECT id FROM users",
            "default_limit": default_limit,
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text


async def test_template_create_above_max_row_limit_is_rejected(client, admin_token):
    await _create_database(client, admin_token)

    resp = await client.post(
        "/admin/query/templates",
        json={
            "path": "reports/huge",
            "name": "Huge",
            "database": "tmpl-db",
            "sql": "SELECT id FROM users",
            "default_limit": ABOVE_MAX,
        },
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 422, resp.text


async def test_template_update_above_max_row_limit_is_rejected(client, admin_token):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token, path="reports/bounded")

    resp = await client.put(
        "/admin/query/templates/reports/bounded",
        json={"default_limit": ABOVE_MAX},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 422, resp.text


async def test_stored_template_limit_above_max_executes_clamped(
    client, admin_token, seeded_db,
):
    """A row stored before the ceiling existed must execute, not raise in-handler."""
    await _create_database(client, admin_token)
    await _create_template(client, admin_token, path="reports/legacy")

    session_factory = async_sessionmaker(
        seeded_db, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as db:
        result = await db.execute(
            select(QueryTemplate).where(QueryTemplate.path == "reports/legacy")
        )
        result.scalar_one().default_limit = ABOVE_MAX
        await db.commit()

    execute_mock = AsyncMock(return_value=_mock_query_response())
    with patch(
        "app.routers.query.connection_manager.get_db_type", return_value="postgres",
    ), patch(
        "app.routers.query.connection_manager.get_engine", return_value=MagicMock(),
    ), patch("app.routers.query.execute_query", execute_mock), patch(
        "app.routers.query.log_query", new_callable=AsyncMock
    ):
        resp = await client.post(
            "/query/templates/reports/legacy",
            json={},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200, resp.text
    assert execute_mock.await_args.kwargs["limit"] == settings.MAX_ROW_LIMIT


def test_template_execute_request_rejects_limit_above_max():
    with pytest.raises(ValidationError):
        QueryTemplateExecuteRequest(limit=ABOVE_MAX)


def test_agent_template_schemas_reject_limit_above_max():
    with pytest.raises(ValidationError):
        QueryTemplateAgentCreate(
            name="n", database="tmpl-db", sql="SELECT 1", default_limit=ABOVE_MAX,
        )
    with pytest.raises(ValidationError):
        QueryTemplateAgentUpdate(default_limit=ABOVE_MAX)
