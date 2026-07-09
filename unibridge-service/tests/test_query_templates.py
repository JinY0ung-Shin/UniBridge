from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKeyAccess
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


async def test_template_execution_uses_database_permissions(client, admin_token, querier_token):
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
            headers=auth_header(querier_token),
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


async def test_explain_analyze_mutating_query_template_is_rejected(client, admin_token):
    await _create_database(client, admin_token)

    resp = await client.post(
        "/admin/query/templates",
        json={
            "path": "reports/analyze-delete",
            "name": "Analyze delete",
            "database": "maindb",
            "sql": "EXPLAIN (ANALYZE, FORMAT JSON) DELETE FROM users WHERE id = :id",
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


async def _grant_db_permission(client, admin_token, *, role: str, alias: str) -> None:
    resp = await client.put(
        "/admin/query/permissions",
        json={
            "role": role,
            "db_alias": alias,
            "allow_select": True,
            "allow_insert": False,
            "allow_update": False,
            "allow_delete": False,
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200


# ── Template discovery — JWT query users + API-key agents ────────────────────


async def test_query_user_lists_only_accessible_templates(client, admin_token, querier_token):
    await _create_database(client, admin_token, alias="maindb")
    await _create_database(client, admin_token, alias="otherdb")
    await _create_template(client, admin_token, path="reports/users", database="maindb")
    await _create_template(client, admin_token, path="reports/orders", database="otherdb")
    # The querier role can only query maindb.
    await _grant_db_permission(client, admin_token, role="querier", alias="maindb")

    resp = await client.get("/query/templates", headers=auth_header(querier_token))

    assert resp.status_code == 200
    body = resp.json()
    assert [t["path"] for t in body] == ["reports/users"]
    # SQL is exposed for discovery.
    assert body[0]["sql"] == "SELECT id, name FROM users WHERE id = :id"


async def test_query_user_without_db_permission_sees_no_templates(client, admin_token, querier_token):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)

    resp = await client.get("/query/templates", headers=auth_header(querier_token))

    assert resp.status_code == 200
    assert resp.json() == []


async def test_query_templates_listing_requires_query_execute(client, user_token):
    resp = await client.get("/query/templates", headers=auth_header(user_token))
    assert resp.status_code == 403


async def test_query_templates_listing_hides_disabled(client, admin_token):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token, path="reports/users")
    await _create_template(client, admin_token, path="reports/hidden", enabled=False)

    # Admin has query.databases.write → all-DB access, but disabled templates
    # are still hidden from the runnable listing.
    resp = await client.get("/query/templates", headers=auth_header(admin_token))

    assert resp.status_code == 200
    assert [t["path"] for t in resp.json()] == ["reports/users"]


async def _create_api_key(seeded_db, *, name: str, allowed_databases: list[str]) -> None:
    """Insert an API-key consumer directly.

    Bypasses the APISIX-dependent ``POST /admin/api-keys`` endpoint so the
    discovery listing can be exercised for the API-key (agent) principal.
    """
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(
            ApiKeyAccess(
                consumer_name=name,
                allowed_databases=json.dumps(allowed_databases),
                allowed_routes=json.dumps(["query-api"]),
            )
        )
        await db.commit()


async def test_apikey_lists_only_templates_on_allowed_databases(client, admin_token, seeded_db):
    await _create_database(client, admin_token, alias="maindb")
    await _create_database(client, admin_token, alias="otherdb")
    await _create_template(client, admin_token, path="reports/users", database="maindb")
    await _create_template(client, admin_token, path="reports/orders", database="otherdb")
    await _create_api_key(seeded_db, name="agent-key", allowed_databases=["maindb"])

    # No Bearer token: the X-Consumer-Username header alone authenticates as the
    # API key (dev-token mode skips the internal-proxy-secret check).
    resp = await client.get("/query/templates", headers={"X-Consumer-Username": "agent-key"})

    assert resp.status_code == 200
    body = resp.json()
    # Only the template on the key's allowed database is discoverable.
    assert [t["path"] for t in body] == ["reports/users"]
    assert body[0]["sql"] == "SELECT id, name FROM users WHERE id = :id"


async def test_apikey_wildcard_lists_all_enabled_templates(client, admin_token, seeded_db):
    await _create_database(client, admin_token, alias="maindb")
    await _create_database(client, admin_token, alias="otherdb")
    await _create_template(client, admin_token, path="reports/users", database="maindb")
    await _create_template(client, admin_token, path="reports/orders", database="otherdb")
    await _create_template(
        client, admin_token, path="reports/hidden", database="maindb", enabled=False
    )
    await _create_api_key(seeded_db, name="master-key", allowed_databases=["*"])

    resp = await client.get("/query/templates", headers={"X-Consumer-Username": "master-key"})

    assert resp.status_code == 200
    # "*" sees every database, but disabled templates are still hidden.
    assert [t["path"] for t in resp.json()] == ["reports/orders", "reports/users"]
