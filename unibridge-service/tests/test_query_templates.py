from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas import QueryResponse
from tests.conftest import auth_header


def _mock_query_response() -> QueryResponse:
    return QueryResponse(
        columns=["id", "name"],
        rows=[[1, "alice"]],
        row_count=1,
        truncated=False,
        elapsed_ms=12,
    )


async def _create_database(client, admin_token, alias: str = "maindb") -> None:
    with patch(
        "app.routers.admin.connection_manager.add_connection",
        new_callable=AsyncMock,
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
    assert resp.status_code == 201


async def _create_template(
    client,
    admin_token,
    *,
    path: str = "reports/users",
    database: str = "maindb",
    sql: str = "SELECT id, name FROM users WHERE id = :id",
    enabled: bool = True,
) -> None:
    resp = await client.post(
        "/admin/query/templates",
        json={
            "path": path,
            "name": "Users report",
            "database": database,
            "sql": sql,
            "default_limit": 50,
            "enabled": enabled,
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201


async def test_admin_can_create_and_execute_query_template(client, admin_token):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)

    mock_engine = MagicMock()
    with patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="postgres",
    ), patch(
        "app.routers.query.connection_manager.get_engine",
        return_value=mock_engine,
    ), patch(
        "app.routers.query.execute_query",
        new_callable=AsyncMock,
        return_value=_mock_query_response(),
    ) as mock_exec, patch(
        "app.routers.query.log_query",
        new_callable=AsyncMock,
    ):
        resp = await client.post(
            "/query/templates/reports/users",
            json={"params": {"id": 1}},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    assert resp.json()["rows"] == [[1, "alice"]]
    mock_exec.assert_awaited_once_with(
        engine=mock_engine,
        sql="SELECT id, name FROM users WHERE id = :id",
        params={"id": 1},
        limit=50,
        timeout=None,
        db_type="postgres",
    )


async def test_template_execution_uses_database_permissions(client, admin_token, developer_token):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)

    with patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="postgres",
    ), patch(
        "app.routers.query.execute_query",
        new_callable=AsyncMock,
    ) as mock_exec:
        resp = await client.post(
            "/query/templates/reports/users",
            json={"params": {"id": 1}},
            headers=auth_header(developer_token),
        )

    assert resp.status_code == 403
    assert "No permissions configured" in resp.json()["detail"]
    mock_exec.assert_not_awaited()


async def test_disabled_template_cannot_be_executed(client, admin_token):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token, enabled=False)

    resp = await client.post(
        "/query/templates/reports/users",
        json={"params": {"id": 1}},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 403
    assert "is disabled" in resp.json()["detail"]


async def test_mutating_query_template_is_rejected(client, admin_token):
    await _create_database(client, admin_token)

    resp = await client.post(
        "/admin/query/templates",
        json={
            "path": "reports/delete-users",
            "name": "Delete users",
            "database": "maindb",
            "sql": "DELETE FROM users WHERE id = :id",
        },
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Query templates must be read-only SELECT/EXPLAIN statements"


async def test_query_template_crud(client, admin_token):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)

    resp = await client.get("/admin/query/templates", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()[0]["path"] == "reports/users"

    resp = await client.put(
        "/admin/query/templates/reports/users",
        json={"name": "Renamed report", "default_limit": None},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed report"
    assert resp.json()["default_limit"] is None

    resp = await client.delete(
        "/admin/query/templates/reports/users",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204

    resp = await client.get("/admin/query/templates", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []
