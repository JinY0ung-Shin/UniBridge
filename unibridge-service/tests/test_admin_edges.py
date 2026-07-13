"""Stable error and boundary-path tests for the query admin router."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers import admin
from app.schemas import QueryTemplateUpdate
from tests.conftest import auth_header


DB_PAYLOAD = {
    "alias": "testdb",
    "db_type": "postgres",
    "host": "localhost",
    "port": 5432,
    "database": "app",
    "username": "tester",
    "password": "secret",
}


def _manager(*, db_type: str = "postgres") -> MagicMock:
    manager = MagicMock()
    manager.add_connection = AsyncMock()
    manager.remove_connection = AsyncMock()
    manager.get_status.return_value = {"status": "registered"}
    manager.get_db_type.return_value = db_type
    manager.has_connection.return_value = True
    manager.test_connection = AsyncMock(return_value=(True, "ok"))
    return manager


class _Rows:
    def __init__(self, rows) -> None:
        self.rows = rows

    def fetchall(self):
        return self.rows


class _SqlConnection:
    def __init__(self, rows=None, *, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        if self.error is not None:
            raise self.error
        return _Rows(self.rows)


class _ConnectionContext:
    def __init__(self, connection: _SqlConnection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Engine:
    def __init__(self, connection: _SqlConnection) -> None:
        self.connection = connection

    def connect(self):
        return _ConnectionContext(self.connection)


async def _create_database(client, token, manager, *, alias="testdb") -> None:
    response = await client.post(
        "/admin/query/databases",
        json={**DB_PAYLOAD, "alias": alias},
        headers=auth_header(token),
    )
    assert response.status_code == 201, response.text


async def test_postgres_protocol_without_secure_is_rejected(client, admin_token):
    manager = _manager()
    with patch("app.routers.admin.connection_manager", manager):
        response = await client.post(
            "/admin/query/databases",
            json={**DB_PAYLOAD, "protocol": "https"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 400
    assert "protocol is only valid" in response.json()["detail"]


async def test_create_template_with_unknown_database_returns_404(client, admin_token):
    response = await client.post(
        "/admin/query/templates",
        json={
            "path": "reports/missing",
            "name": "Missing database",
            "database": "does-not-exist",
            "sql": "SELECT 1",
        },
        headers=auth_header(admin_token),
    )

    assert response.status_code == 404
    assert "Database alias 'does-not-exist' not found" in response.json()["detail"]


async def test_update_connection_reports_engine_recreation_failure(client, admin_token):
    manager = _manager()
    manager.add_connection.side_effect = [None, RuntimeError("driver unavailable")]
    with patch("app.routers.admin.connection_manager", manager):
        await _create_database(client, admin_token, manager)
        response = await client.put(
            "/admin/query/databases/testdb",
            json={"host": "new-host"},
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["host"] == "new-host"


@pytest.mark.parametrize(
    ("db_type", "expected_fragment"),
    [
        ("postgres", "pg_catalog.pg_tables"),
        ("mssql", "INFORMATION_SCHEMA.TABLES"),
    ],
)
async def test_list_tables_for_relational_database(
    client, admin_token, db_type, expected_fragment
):
    manager = _manager(db_type=db_type)
    connection = _SqlConnection(rows=[("public.users",), ("public.orders",)])
    manager.get_engine.return_value = _Engine(connection)
    with patch("app.routers.admin.connection_manager", manager):
        response = await client.get(
            "/admin/query/databases/main/tables",
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200
    assert response.json() == ["public.users", "public.orders"]
    assert expected_fragment in connection.statements[0]


async def test_list_tables_maps_database_failure_to_500(client, admin_token):
    manager = _manager(db_type="postgres")
    manager.get_engine.return_value = _Engine(
        _SqlConnection(error=RuntimeError("catalog unavailable"))
    )
    with patch("app.routers.admin.connection_manager", manager):
        response = await client.get(
            "/admin/query/databases/main/tables",
            headers=auth_header(admin_token),
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to list tables: catalog unavailable"


async def test_permission_validation_for_unknown_database_returns_404(
    client, admin_token
):
    manager = _manager(db_type="unknown")
    with patch("app.routers.admin.connection_manager", manager):
        response = await client.put(
            "/admin/query/permissions",
            json={
                "role": "user",
                "db_alias": "missing",
                "allowed_tables": ["users"],
            },
            headers=auth_header(admin_token),
        )

    assert response.status_code == 404
    assert "not registered or not connected" in response.json()["detail"]


@pytest.mark.parametrize("db_type", ["postgres", "mssql"])
async def test_permission_validation_accepts_existing_relational_tables(
    client, admin_token, db_type
):
    manager = _manager(db_type=db_type)
    connection = _SqlConnection(rows=[("Users",), ("orders",)])
    manager.get_engine.return_value = _Engine(connection)
    with patch("app.routers.admin.connection_manager", manager):
        response = await client.put(
            "/admin/query/permissions",
            json={
                "role": "user",
                "db_alias": "main",
                "allowed_tables": ["users"],
            },
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    assert response.json()["allowed_tables"] == ["users"]


async def test_permission_validation_rejects_missing_table(client, admin_token):
    manager = _manager()
    manager.get_engine.return_value = _Engine(_SqlConnection(rows=[("users",)]))
    with patch("app.routers.admin.connection_manager", manager):
        response = await client.put(
            "/admin/query/permissions",
            json={
                "role": "user",
                "db_alias": "main",
                "allowed_tables": ["users", "missing"],
            },
            headers=auth_header(admin_token),
        )

    assert response.status_code == 400
    assert "missing" in response.json()["detail"]


async def test_permission_validation_maps_engine_failure_to_500(client, admin_token):
    manager = _manager()
    manager.get_engine.return_value = _Engine(
        _SqlConnection(error=RuntimeError("metadata denied"))
    )
    with patch("app.routers.admin.connection_manager", manager):
        response = await client.put(
            "/admin/query/permissions",
            json={
                "role": "user",
                "db_alias": "main",
                "allowed_tables": ["users"],
            },
            headers=auth_header(admin_token),
        )

    assert response.status_code == 500
    assert "metadata denied" in response.json()["detail"]


async def test_permission_validation_accepts_clickhouse_table(client, admin_token):
    manager = _manager(db_type="clickhouse")
    manager.get_clickhouse_client.return_value = SimpleNamespace(query=MagicMock())
    manager.get_clickhouse_lock.return_value = MagicMock()
    clickhouse_result = SimpleNamespace(result_rows=[("Events",), ("users",)])
    with (
        patch("app.routers.admin.connection_manager", manager),
        patch(
            "app.routers.admin.asyncio.to_thread",
            new=AsyncMock(return_value=clickhouse_result),
        ) as to_thread,
    ):
        response = await client.put(
            "/admin/query/permissions",
            json={
                "role": "user",
                "db_alias": "analytics",
                "allowed_tables": ["events"],
            },
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    to_thread.assert_awaited_once()


async def test_template_duplicate_update_all_fields_and_missing_delete(
    client, admin_token
):
    manager = _manager()
    with patch("app.routers.admin.connection_manager", manager):
        await _create_database(client, admin_token, manager)
        create_body = {
            "path": "reports/users",
            "name": "Users",
            "database": "testdb",
            "sql": "SELECT id FROM users",
            "default_limit": 50,
            "timeout": 20,
        }
        created = await client.post(
            "/admin/query/templates",
            json=create_body,
            headers=auth_header(admin_token),
        )
        assert created.status_code == 201, created.text

        duplicate = await client.post(
            "/admin/query/templates",
            json=create_body,
            headers=auth_header(admin_token),
        )
        updated = await client.put(
            "/admin/query/templates/reports/users",
            json={
                "name": "Users v2",
                "description": "Updated report",
                "database": "testdb",
                "sql": "SELECT name FROM users",
                "default_limit": None,
                "timeout": None,
                "enabled": False,
            },
            headers=auth_header(admin_token),
        )
        missing_update = await client.put(
            "/admin/query/templates/reports/missing",
            json={"name": "Missing"},
            headers=auth_header(admin_token),
        )
        missing_delete = await client.delete(
            "/admin/query/templates/reports/missing",
            headers=auth_header(admin_token),
        )

    assert duplicate.status_code == 409
    assert updated.status_code == 200, updated.text
    updated_body = updated.json()
    assert updated_body["name"] == "Users v2"
    assert updated_body["description"] == "Updated report"
    assert updated_body["database"] == "testdb"
    assert updated_body["sql"] == "SELECT name FROM users"
    assert updated_body["default_limit"] is None
    assert updated_body["timeout"] is None
    assert updated_body["enabled"] is False
    assert missing_update.status_code == 404
    assert missing_delete.status_code == 404


@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_template_mutation_rejects_invalid_path_before_database_access(operation):
    db = MagicMock()
    body = QueryTemplateUpdate(name="ignored")

    with pytest.raises(HTTPException) as exc_info:
        if operation == "update":
            await admin.update_query_template("../bad", body, SimpleNamespace(username="a"), db)
        else:
            await admin.delete_query_template("../bad", SimpleNamespace(username="a"), db)

    assert exc_info.value.status_code == 400
    db.execute.assert_not_called()


@pytest.mark.parametrize("field", ["from_date", "to_date"])
async def test_admin_audit_rejects_invalid_date(client, admin_token, field):
    response = await client.get(
        "/admin/audit-logs",
        params={field: "not-a-date"},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 400
    assert f"Invalid {field} format" in response.json()["detail"]


async def test_admin_audit_accepts_all_filters_and_date_range(client, admin_token):
    response = await client.get(
        "/admin/audit-logs",
        params={
            "actor": "nobody",
            "resource_type": "role",
            "action": "update",
            "from_date": "2025-01-01T00:00:00",
            "to_date": "2025-12-31T23:59:59",
            "offset": 1,
            "limit": 5,
        },
        headers=auth_header(admin_token),
    )

    assert response.status_code == 200
    assert response.json() == []


def _settings_data(*, query_timeout=30, gateway_timeout=60):
    return {
        "rate_limit_per_minute": 120,
        "max_concurrent_queries": 8,
        "default_row_limit": 500,
        "query_route_timeout": query_timeout,
        "gateway_route_timeout": gateway_timeout,
        "blocked_sql_keywords": ["DROP"],
    }


async def test_get_settings_returns_runtime_values(client, admin_token):
    manager = MagicMock()
    manager.get_all.return_value = _settings_data()
    with patch("app.routers.admin.settings_manager", manager):
        response = await client.get(
            "/admin/query/settings", headers=auth_header(admin_token)
        )

    assert response.status_code == 200
    assert response.json() == _settings_data()


@pytest.mark.parametrize("dependencies_fail", [False, True])
async def test_update_settings_persists_and_best_effort_syncs_routes(
    client, admin_token, dependencies_fail, caplog
):
    manager = MagicMock()
    manager.update = AsyncMock()
    manager.get_all.side_effect = [_settings_data(), _settings_data(query_timeout=45, gateway_timeout=90)]
    manager.query_route_timeout = 45
    manager.gateway_route_timeout = 90
    rate_limiter = MagicMock()
    failure = RuntimeError("gateway offline") if dependencies_fail else None
    patch_query = AsyncMock(side_effect=failure)
    sync_gateway = AsyncMock(side_effect=failure, return_value=3)

    with (
        patch("app.routers.admin.settings_manager", manager),
        patch("app.middleware.rate_limiter.rate_limiter", rate_limiter),
        patch("app.services.apisix_client.patch_resource", patch_query),
        patch("app.routers.gateway.sync_default_route_timeout", sync_gateway),
    ):
        response = await client.put(
            "/admin/query/settings",
            json={
                "rate_limit_per_minute": 120,
                "max_concurrent_queries": 8,
                "default_row_limit": 500,
                "query_route_timeout": 45,
                "gateway_route_timeout": 90,
                "blocked_sql_keywords": ["DROP"],
            },
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    assert response.json() == _settings_data(query_timeout=45, gateway_timeout=90)
    manager.update.assert_awaited_once()
    rate_limiter.update_limits.assert_called_once_with(
        rate_limit=120, max_concurrent=8
    )
    patch_query.assert_awaited_once()
    sync_gateway.assert_awaited_once_with(90)
    if dependencies_fail:
        assert "Failed to live-patch query-api route timeout" in caplog.text
        assert "Failed to apply default gateway timeout" in caplog.text
