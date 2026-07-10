from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKeyAccess, Permission
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


async def _grant_db_permission(
    client, admin_token, *, role: str, alias: str,
    allow_select: bool = True, allowed_tables: list[str] | None = None,
) -> None:
    resp = await client.put(
        "/admin/query/permissions",
        json={
            "role": role,
            "db_alias": alias,
            "allow_select": allow_select,
            "allow_insert": False,
            "allow_update": False,
            "allow_delete": False,
            "allowed_tables": allowed_tables,
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


async def test_query_user_listing_matches_select_and_table_permissions(
    client, admin_token, querier_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token, path="reports/users", sql="SELECT * FROM users")
    await _create_template(client, admin_token, path="reports/orders", sql="SELECT * FROM orders")
    await _grant_db_permission(client, admin_token, role="querier", alias="maindb")
    session_factory = async_sessionmaker(
        seeded_db, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as db:
        result = await db.execute(
            select(Permission).where(
                Permission.role == "querier", Permission.db_alias == "maindb"
            )
        )
        result.scalar_one().allowed_tables = json.dumps(["users"])
        await db.commit()

    scoped = await client.get("/query/templates", headers=auth_header(querier_token))
    assert scoped.status_code == 200
    assert [template["path"] for template in scoped.json()] == ["reports/users"]

    await _grant_db_permission(
        client, admin_token, role="querier", alias="maindb", allow_select=False
    )
    no_select = await client.get("/query/templates", headers=auth_header(querier_token))
    assert no_select.status_code == 200
    assert no_select.json() == []


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


async def _create_api_key(
    seeded_db, *, name: str, allowed_databases: list[str],
    allowed_routes: list[str] | None = None,
    allowed_tables: list[str] | None = None,
) -> None:
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
                allowed_routes=json.dumps(
                    ["query-api"] if allowed_routes is None else allowed_routes
                ),
                allowed_tables=(
                    json.dumps(allowed_tables) if allowed_tables is not None else None
                ),
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


async def test_apikey_listing_hides_templates_outside_table_scope(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token, path="reports/users", sql="SELECT * FROM users")
    await _create_template(client, admin_token, path="reports/orders", sql="SELECT * FROM orders")
    await _create_api_key(
        seeded_db, name="users-reader", allowed_databases=["maindb"],
        allowed_tables=["users"],
    )

    resp = await client.get(
        "/query/templates", headers={"X-Consumer-Username": "users-reader"}
    )
    assert resp.status_code == 200
    assert [template["path"] for template in resp.json()] == ["reports/users"]


# ── Agent guide + independently granted template lifecycle ─────────────────


async def test_query_template_agent_guide_is_markdown(client, seeded_db):
    await _create_api_key(
        seeded_db, name="guide-reader", allowed_databases=[],
        allowed_routes=["query-api"],
    )
    resp = await client.get(
        "/query/templates/guide", headers={"X-Consumer-Username": "guide-reader"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "GET /api/query/templates" in resp.text
    assert "PUT /api/query/templates/reports/new-users" in resp.text
    assert "DELETE /api/query/templates/reports/new-users" in resp.text
    assert "query-template-write-api" in resp.text
    assert "## Parameter value formats" in resp.text
    assert "ANY(CAST(:ids AS bigint[]))" in resp.text
    assert "OPENJSON(:ids_json)" in resp.text
    assert "{ids:Array(UInt64)}" in resp.text
    assert "u.id IN $ids" in resp.text
    assert "GraphDB" in resp.text
    assert "## Query result format" in resp.text


async def test_agent_creates_and_deletes_template_with_write_route(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_api_key(
        seeded_db, name="lifecycle-agent", allowed_databases=["maindb"],
        allowed_routes=["query-template-write-api"], allowed_tables=["users"],
    )
    headers = {"X-Consumer-Username": "lifecycle-agent"}

    created = await client.put(
        "/query/templates/reports/created",
        json={
            "name": "Created report",
            "description": "Created by an agent",
            "database": "maindb",
            "sql": "SELECT id FROM users",
            "default_limit": 25,
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["path"] == "reports/created"
    assert created.json()["enabled"] is True
    deleted = await client.delete(
        "/query/templates/reports/created",
        params={"expected_updated_at": created.json()["updated_at"]},
        headers=headers,
    )
    templates = await client.get(
        "/admin/query/templates", headers=auth_header(admin_token)
    )
    logs = await client.get(
        "/admin/audit-logs",
        params={"resource_type": "query_template"},
        headers=auth_header(admin_token),
    )

    assert deleted.status_code == 204
    assert templates.json() == []
    lifecycle_logs = [
        entry for entry in logs.json() if entry["actor"] == "apikey:lifecycle-agent"
    ]
    assert [entry["action"] for entry in lifecycle_logs] == ["delete", "create"]
    assert json.loads(lifecycle_logs[0]["before"])["path"] == "reports/created"
    assert json.loads(lifecycle_logs[1]["after"])["path"] == "reports/created"


async def test_agent_create_enforces_write_database_table_and_sql_scope(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token, alias="maindb")
    await _create_database(client, admin_token, alias="otherdb")
    await _create_api_key(
        seeded_db, name="scoped-creator", allowed_databases=["maindb"],
        allowed_routes=["query-template-write-api"], allowed_tables=["users"],
    )
    await _create_api_key(
        seeded_db, name="read-only-creator", allowed_databases=["maindb"],
        allowed_routes=["query-api"],
    )
    headers = {"X-Consumer-Username": "scoped-creator"}
    valid_body = {
        "name": "Users report",
        "database": "maindb",
        "sql": "SELECT id FROM users",
    }

    created = await client.put(
        "/query/templates/reports/users", json=valid_body, headers=headers
    )
    duplicate = await client.put(
        "/query/templates/reports/users", json=valid_body, headers=headers
    )
    wrong_table = await client.put(
        "/query/templates/reports/orders",
        json={**valid_body, "sql": "SELECT id FROM orders"},
        headers=headers,
    )
    mutating = await client.put(
        "/query/templates/reports/delete-users",
        json={**valid_body, "sql": "DELETE FROM users"},
        headers=headers,
    )
    wrong_database = await client.put(
        "/query/templates/reports/other",
        json={**valid_body, "database": "otherdb"},
        headers=headers,
    )
    extra_field = await client.put(
        "/query/templates/reports/extra",
        json={**valid_body, "enabled": False},
        headers=headers,
    )
    no_write_route = await client.put(
        "/query/templates/reports/denied",
        json=valid_body,
        headers={"X-Consumer-Username": "read-only-creator"},
    )

    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert wrong_table.status_code == 403
    assert mutating.status_code == 400
    assert wrong_database.status_code == 403
    assert extra_field.status_code == 422
    assert no_write_route.status_code == 403


async def test_agent_delete_requires_write_route_and_current_version(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)
    await _create_api_key(
        seeded_db, name="delete-agent", allowed_databases=["maindb"],
        allowed_routes=["query-api", "query-template-write-api"],
    )
    await _create_api_key(
        seeded_db, name="read-only-delete-agent", allowed_databases=["maindb"],
        allowed_routes=["query-api"],
    )
    await _create_api_key(
        seeded_db, name="wrong-table-delete-agent", allowed_databases=["maindb"],
        allowed_routes=["query-template-write-api"], allowed_tables=["orders"],
    )
    headers = {"X-Consumer-Username": "delete-agent"}
    discovered = await client.get("/query/templates", headers=headers)
    updated_at = discovered.json()[0]["updated_at"]

    denied = await client.delete(
        "/query/templates/reports/users",
        params={"expected_updated_at": updated_at},
        headers={"X-Consumer-Username": "read-only-delete-agent"},
    )
    missing_version = await client.delete(
        "/query/templates/reports/users", headers=headers
    )
    wrong_table = await client.delete(
        "/query/templates/reports/users",
        params={"expected_updated_at": updated_at},
        headers={"X-Consumer-Username": "wrong-table-delete-agent"},
    )
    stale = await client.delete(
        "/query/templates/reports/users",
        params={"expected_updated_at": "2000-01-01T00:00:00Z"},
        headers=headers,
    )
    deleted = await client.delete(
        "/query/templates/reports/users",
        params={"expected_updated_at": updated_at},
        headers=headers,
    )

    assert denied.status_code == 403
    assert missing_version.status_code == 422
    assert wrong_table.status_code == 403
    assert stale.status_code == 409
    assert deleted.status_code == 204


async def test_template_write_route_is_independent_from_read_route(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)
    await _create_api_key(
        seeded_db, name="read-only-agent", allowed_databases=["maindb"],
        allowed_routes=["query-api"],
    )
    await _create_api_key(
        seeded_db, name="write-only-agent", allowed_databases=["maindb"],
        allowed_routes=["query-template-write-api"],
    )

    denied_edit = await client.patch(
        "/query/templates/reports/users", json={"description": "must not be saved"},
        headers={"X-Consumer-Username": "read-only-agent"},
    )
    denied_read = await client.get(
        "/query/templates", headers={"X-Consumer-Username": "write-only-agent"}
    )
    with patch("app.routers.query.log_admin_action", new_callable=AsyncMock) as audit:
        allowed_edit = await client.patch(
            "/query/templates/reports/users",
            json={"description": "agent-maintained report", "default_limit": None},
            headers={"X-Consumer-Username": "write-only-agent"},
        )

    assert denied_edit.status_code == 403
    assert denied_edit.json()["detail"] == "Required API key route: query-template-write-api"
    assert denied_read.status_code == 403
    assert denied_read.json()["detail"] == "Required API key route: query-api"
    assert allowed_edit.status_code == 200
    assert allowed_edit.json()["description"] == "agent-maintained report"
    assert allowed_edit.json()["default_limit"] is None
    assert audit.await_args.kwargs["actor"] == "apikey:write-only-agent"


async def test_agent_template_edit_rejects_admin_only_fields(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)
    await _create_api_key(
        seeded_db, name="template-editor", allowed_databases=["maindb"],
        allowed_routes=["query-template-write-api"],
    )
    resp = await client.patch(
        "/query/templates/reports/users",
        json={"sql": "SELECT 1", "database": "otherdb", "enabled": False},
        headers={"X-Consumer-Username": "template-editor"},
    )
    assert resp.status_code == 422
    assert {error["loc"][-1] for error in resp.json()["detail"]} == {
        "database", "enabled"
    }


async def test_agent_template_edit_enforces_read_only_and_table_scope(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token, sql="SELECT * FROM users")
    await _create_api_key(
        seeded_db, name="scoped-editor", allowed_databases=["maindb"],
        allowed_routes=["query-template-write-api"], allowed_tables=["users"],
    )
    headers = {"X-Consumer-Username": "scoped-editor"}
    mutating = await client.patch(
        "/query/templates/reports/users", json={"sql": "DELETE FROM users"}, headers=headers
    )
    other_table = await client.patch(
        "/query/templates/reports/users", json={"sql": "SELECT * FROM orders"},
        headers=headers,
    )
    allowed = await client.patch(
        "/query/templates/reports/users", json={"sql": "SELECT id FROM users"},
        headers=headers,
    )
    assert mutating.status_code == 400
    assert other_table.status_code == 403
    assert other_table.json()["detail"] == "Access denied to table(s): orders"
    assert allowed.status_code == 200
    assert allowed.json()["sql"] == "SELECT id FROM users"


async def test_agent_template_edit_enforces_database_scope(client, admin_token, seeded_db):
    await _create_database(client, admin_token, alias="maindb")
    await _create_template(client, admin_token, database="maindb")
    await _create_api_key(
        seeded_db, name="otherdb-editor", allowed_databases=["otherdb"],
        allowed_routes=["query-template-write-api"],
    )
    resp = await client.patch(
        "/query/templates/reports/users", json={"description": "not allowed"},
        headers={"X-Consumer-Username": "otherdb-editor"},
    )
    assert resp.status_code == 403
    assert "not allowed to access database 'maindb'" in resp.json()["detail"]


async def test_agent_template_edit_detects_stale_discovery(client, admin_token, seeded_db):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)
    await _create_api_key(
        seeded_db, name="full-template-agent", allowed_databases=["maindb"],
        allowed_routes=["query-api", "query-template-write-api"],
    )
    headers = {"X-Consumer-Username": "full-template-agent"}
    discovered = await client.get("/query/templates", headers=headers)
    original_updated_at = discovered.json()[0]["updated_at"]
    first_edit = await client.patch(
        "/query/templates/reports/users",
        json={"description": "first edit", "expected_updated_at": original_updated_at},
        headers=headers,
    )
    stale_edit = await client.patch(
        "/query/templates/reports/users",
        json={"timeout": 20, "expected_updated_at": original_updated_at},
        headers=headers,
    )
    assert first_edit.status_code == 200
    assert stale_edit.status_code == 409
    assert "changed since it was discovered" in stale_edit.json()["detail"]


async def test_agent_template_edit_writes_actor_and_snapshots_to_audit_log(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_template(client, admin_token)
    await _create_api_key(
        seeded_db, name="audited-editor", allowed_databases=["maindb"],
        allowed_routes=["query-template-write-api"],
    )

    edited = await client.patch(
        "/query/templates/reports/users", json={"description": "audited change"},
        headers={"X-Consumer-Username": "audited-editor"},
    )
    logs = await client.get(
        "/admin/audit-logs",
        params={"resource_type": "query_template", "action": "update"},
        headers=auth_header(admin_token),
    )

    assert edited.status_code == 200
    assert logs.status_code == 200
    entry = logs.json()[0]
    assert entry["actor"] == "apikey:audited-editor"
    assert json.loads(entry["before"])["description"] == ""
    assert json.loads(entry["after"])["description"] == "audited change"
