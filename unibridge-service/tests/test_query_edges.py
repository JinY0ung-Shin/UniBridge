"""Failure and boundary-path tests for query execution and agent templates."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.auth import ApiKeyUser, CurrentUser
from app.routers import query
from app.schemas import (
    QueryRequest,
    QueryResponse,
    QueryTemplateAgentCreate,
    QueryTemplateAgentUpdate,
)
from tests.conftest import auth_header


class _Result:
    def __init__(self, value=None, *, rows=None, rowcount=0) -> None:
        self.value = value
        self.rows = list(rows or [])
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.rows


def _db(*results, commit_error: Exception | None = None):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.commit = AsyncMock(side_effect=commit_error)
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


def _query_response() -> QueryResponse:
    return QueryResponse(
        columns=["value"],
        rows=[[1]],
        row_count=1,
        truncated=False,
        elapsed_ms=12,
    )


async def test_template_write_access_denies_jwt_without_permission(monkeypatch):
    monkeypatch.setattr(query, "get_role_permissions", AsyncMock(return_value=[]))

    with pytest.raises(HTTPException) as exc_info:
        await query._require_template_write_access(
            MagicMock(), CurrentUser(username="reader", role="reader")
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Required permission: query.settings.write"


def test_template_scope_is_noop_for_jwt_user():
    query._ensure_api_key_template_scope(
        CurrentUser(username="admin", role="admin"),
        database="main",
        db_type="postgres",
        sql="SELECT * FROM secrets",
    )


@pytest.mark.parametrize(
    "cypher",
    [
        "// unterminated line comment",
        "/* unterminated block comment",
        r"RETURN 'escaped \' quote'",
    ],
)
def test_neo4j_sanitizer_handles_unterminated_comments_and_escapes(cypher):
    assert isinstance(query._strip_neo4j_literals_and_comments(cypher), str)


def test_neo4j_unknown_statement_and_blocked_keyword_no_match():
    assert query._detect_neo4j_statement_type("SHOW INDEXES") == "unknown"
    assert query._extra_blocked_keyword_error("SELECT 1", ["VACUUM"]) is None


@pytest.mark.parametrize(
    "cypher",
    [
        "// ignored comment\nRETURN 1",
        "/* ignored comment */ RETURN 1",
    ],
)
def test_neo4j_sanitizer_resumes_after_terminated_comment(cypher):
    sanitized = query._strip_neo4j_literals_and_comments(cypher)
    assert "ignored comment" not in sanitized
    assert "RETURN 1" in sanitized


async def test_record_failed_query_survives_audit_failure(monkeypatch, caplog):
    record_metric = MagicMock()
    monkeypatch.setattr(query.metrics, "record_query", record_metric)
    monkeypatch.setattr(
        query, "log_query", AsyncMock(side_effect=RuntimeError("audit offline"))
    )

    await query._record_failed_query(
        MagicMock(),
        username="alice",
        database_alias="main",
        db_type="postgres",
        sql="SELECT 1",
        params=None,
        metric_status="error",
        audit_status="error",
        error_message="failed",
    )

    record_metric.assert_called_once()
    assert "Failed to write audit log for failed query" in caplog.text


async def test_execute_rejects_api_key_without_query_route():
    user = ApiKeyUser(
        consumer_name="limited",
        allowed_databases=["*"],
        allowed_routes=[],
    )

    with pytest.raises(HTTPException) as exc_info:
        await query.execute(
            QueryRequest(database="main", sql="SELECT 1"),
            user=user,
            db=MagicMock(),
        )

    assert exc_info.value.status_code == 403
    assert "query-api" in exc_info.value.detail


async def test_execute_jwt_rate_limit_rejection(client, admin_token):
    with patch(
        "app.routers.query.rate_limiter.check_rate_limit",
        return_value=(False, "Slow down", None),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "main", "sql": "SELECT 1"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.json()["detail"] == "Slow down"


async def test_execute_concurrency_limit_rejection(client, admin_token):
    with (
        patch(
            "app.routers.query.rate_limiter.check_rate_limit",
            return_value=(True, "", None),
        ),
        patch("app.routers.query.rate_limiter.try_acquire", return_value=False),
        patch("app.routers.query.connection_manager.get_db_type", return_value="postgres"),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "main", "sql": "SELECT 1"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 429
    assert "Too many concurrent queries" in response.json()["detail"]


async def test_execute_rejects_blocked_sql_before_executor(client, admin_token):
    with (
        patch("app.routers.query.connection_manager.get_db_type", return_value="postgres"),
        patch("app.routers.query.execute_query", new_callable=AsyncMock) as execute_query,
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "main", "sql": "DROP TABLE users"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 403
    execute_query.assert_not_awaited()


async def test_execute_clickhouse_success(client, admin_token):
    ch_client = MagicMock()
    ch_lock = MagicMock()
    execute_clickhouse = AsyncMock(return_value=_query_response())
    with (
        patch("app.routers.query.connection_manager.get_db_type", return_value="clickhouse"),
        patch(
            "app.routers.query.connection_manager.get_clickhouse_client",
            return_value=ch_client,
        ),
        patch(
            "app.routers.query.connection_manager.get_clickhouse_lock",
            return_value=ch_lock,
        ),
        patch("app.routers.query.execute_clickhouse_query", execute_clickhouse),
        patch("app.routers.query.log_query", new_callable=AsyncMock),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "analytics", "sql": "SELECT 1"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    execute_clickhouse.assert_awaited_once_with(
        client=ch_client,
        sql="SELECT 1",
        params=None,
        limit=None,
        timeout=None,
        lock=ch_lock,
    )


async def test_execute_timeout_survives_audit_failure(client, admin_token, caplog):
    record_metric = MagicMock()
    with (
        patch("app.routers.query.connection_manager.get_db_type", return_value="postgres"),
        patch("app.routers.query.connection_manager.get_engine", return_value=MagicMock()),
        patch(
            "app.routers.query.execute_query",
            new=AsyncMock(side_effect=asyncio.TimeoutError),
        ),
        patch("app.routers.query.metrics.record_query", record_metric),
        patch(
            "app.routers.query.log_query",
            new=AsyncMock(side_effect=RuntimeError("audit offline")),
        ),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "main", "sql": "SELECT 1"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 408
    assert response.json()["detail"] == "Query timed out"
    assert record_metric.call_args.kwargs["status"] == "timeout"
    assert "Failed to write audit log for timed-out query" in caplog.text


async def test_execute_generic_failure_hides_internal_error(
    client, admin_token, caplog
):
    with (
        patch("app.routers.query.connection_manager.get_db_type", return_value="postgres"),
        patch("app.routers.query.connection_manager.get_engine", return_value=MagicMock()),
        patch(
            "app.routers.query.execute_query",
            new=AsyncMock(side_effect=RuntimeError("secret driver detail")),
        ),
        patch(
            "app.routers.query.log_query",
            new=AsyncMock(side_effect=RuntimeError("audit offline")),
        ),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "main", "sql": "SELECT 1"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Query execution failed. Check server logs for details."
    )
    assert "secret driver detail" not in response.text
    assert "Failed to write audit log for failed query" in caplog.text
    assert "Query execution failed" in caplog.text


def test_validate_read_only_template_reraises_non_graphdb_http_error(monkeypatch):
    original = HTTPException(status_code=418, detail="teapot")
    monkeypatch.setattr(
        query, "_detect_statement_type", MagicMock(side_effect=original)
    )

    with pytest.raises(HTTPException) as exc_info:
        query._validate_read_only_template_sql("SELECT 1", "postgres")

    assert exc_info.value is original


@pytest.mark.parametrize("value", ["not-json", '{"users": true}', '["users", 1]'])
def test_decode_allowed_tables_rejects_malformed_or_non_string_values(
    value, caplog
):
    assert query._decode_allowed_tables(value) == []
    assert "Ignoring" in caplog.text


def test_template_tables_allowed_handles_missing_and_graph_database_types():
    template = SimpleNamespace(db_alias="main", sql="SELECT * FROM users")
    assert query._template_tables_allowed(
        template,
        db_types={},
        per_database_limits={},
        api_key_table_limit=None,
    ) is False
    assert query._template_tables_allowed(
        template,
        db_types={"main": "neo4j"},
        per_database_limits={},
        api_key_table_limit=[],
    ) is True


@pytest.mark.parametrize("template_path", ["../invalid", "missing"])
async def test_execute_template_rejects_invalid_or_missing_path(
    monkeypatch, template_path
):
    monkeypatch.setattr(query, "_require_template_read_access", AsyncMock())
    db = _db(_Result(None))

    with pytest.raises(HTTPException) as exc_info:
        await query.execute_template(
            template_path,
            user=CurrentUser(username="admin", role="admin"),
            db=db,
        )

    assert exc_info.value.status_code == (400 if template_path.startswith("..") else 404)


async def test_list_accessible_templates_returns_early_when_query_is_empty(
    monkeypatch,
):
    monkeypatch.setattr(query, "_require_template_read_access", AsyncMock())
    monkeypatch.setattr(
        query,
        "_template_access_scope",
        AsyncMock(return_value=(None, {}, None)),
    )
    db = _db(_Result(rows=[]))

    result = await query.list_accessible_query_templates(
        user=CurrentUser(username="admin", role="admin"), db=db
    )

    assert result == []
    assert db.execute.await_count == 1


def _agent_create_body() -> QueryTemplateAgentCreate:
    return QueryTemplateAgentCreate(
        name="Users",
        database="main",
        sql="SELECT * FROM users",
    )


@pytest.mark.parametrize("template_path", ["../invalid"])
async def test_agent_create_rejects_invalid_path(monkeypatch, template_path):
    monkeypatch.setattr(query, "_require_template_write_access", AsyncMock())
    db = _db()

    with pytest.raises(HTTPException) as exc_info:
        await query.create_query_template_as_agent(
            template_path,
            _agent_create_body(),
            CurrentUser(username="admin", role="admin"),
            db,
        )

    assert exc_info.value.status_code == 400


async def test_agent_create_returns_404_for_missing_database(monkeypatch):
    monkeypatch.setattr(query, "_require_template_write_access", AsyncMock())
    db = _db(_Result(None), _Result(None))

    with pytest.raises(HTTPException) as exc_info:
        await query.create_query_template_as_agent(
            "reports/users",
            _agent_create_body(),
            CurrentUser(username="admin", role="admin"),
            db,
        )

    assert exc_info.value.status_code == 404


async def test_agent_create_maps_commit_race_to_conflict(monkeypatch):
    monkeypatch.setattr(query, "_require_template_write_access", AsyncMock())
    integrity_error = IntegrityError("insert", {}, RuntimeError("duplicate"))
    connection = SimpleNamespace(db_type="postgres")
    db = _db(_Result(None), _Result(connection), commit_error=integrity_error)

    with pytest.raises(HTTPException) as exc_info:
        await query.create_query_template_as_agent(
            "reports/users",
            _agent_create_body(),
            CurrentUser(username="admin", role="admin"),
            db,
        )

    assert exc_info.value.status_code == 409
    db.rollback.assert_awaited_once()


def _agent_update_body() -> QueryTemplateAgentUpdate:
    return QueryTemplateAgentUpdate(description="updated")


@pytest.mark.parametrize(
    ("template_path", "template", "connection", "user", "expected"),
    [
        (
            "../invalid",
            None,
            None,
            CurrentUser(username="admin", role="admin"),
            400,
        ),
        (
            "missing",
            None,
            None,
            CurrentUser(username="admin", role="admin"),
            404,
        ),
        (
            "disabled",
            SimpleNamespace(enabled=False, db_alias="main"),
            None,
            ApiKeyUser("key", ["*"], ["*"]),
            403,
        ),
        (
            "orphaned",
            SimpleNamespace(enabled=True, db_alias="main"),
            None,
            CurrentUser(username="admin", role="admin"),
            404,
        ),
    ],
)
async def test_agent_update_error_paths(
    monkeypatch, template_path, template, connection, user, expected
):
    monkeypatch.setattr(query, "_require_template_write_access", AsyncMock())
    results = [] if template_path.startswith("..") else [_Result(template)]
    if template is not None and getattr(template, "enabled", False):
        results.append(_Result(connection))
    db = _db(*results)

    with pytest.raises(HTTPException) as exc_info:
        await query.update_query_template_as_agent(
            template_path, _agent_update_body(), user, db
        )

    assert exc_info.value.status_code == expected


@pytest.mark.parametrize(
    ("template_path", "template", "connection", "user", "expected"),
    [
        (
            "../invalid",
            None,
            None,
            CurrentUser(username="admin", role="admin"),
            400,
        ),
        (
            "missing",
            None,
            None,
            CurrentUser(username="admin", role="admin"),
            404,
        ),
        (
            "disabled",
            SimpleNamespace(enabled=False, db_alias="main"),
            None,
            ApiKeyUser("key", ["*"], ["*"]),
            403,
        ),
        (
            "orphaned",
            SimpleNamespace(enabled=True, db_alias="main"),
            None,
            CurrentUser(username="admin", role="admin"),
            404,
        ),
    ],
)
async def test_agent_delete_error_paths(
    monkeypatch, template_path, template, connection, user, expected
):
    monkeypatch.setattr(query, "_require_template_write_access", AsyncMock())
    results = [] if template_path.startswith("..") else [_Result(template)]
    if template is not None and getattr(template, "enabled", False):
        results.append(_Result(connection))
    db = _db(*results)

    with pytest.raises(HTTPException) as exc_info:
        await query.delete_query_template_as_agent(
            template_path,
            datetime.now(UTC),
            user,
            db,
        )

    assert exc_info.value.status_code == expected


async def test_health_databases_hides_connection_exception(
    client, admin_token, caplog
):
    with (
        patch(
            "app.routers.query.connection_manager.list_aliases",
            return_value=["broken"],
        ),
        patch(
            "app.routers.query.connection_manager.test_connection",
            new=AsyncMock(side_effect=RuntimeError("secret DSN")),
        ),
    ):
        response = await client.get(
            "/health/databases", headers=auth_header(admin_token)
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "databases": {
            "broken": {"status": "error", "detail": "Connection failed"}
        },
    }
    assert "Health check failed for 'broken'" in caplog.text
