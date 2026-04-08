"""Comprehensive tests for query and roles routers."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.auth import ALL_PERMISSIONS
from app.schemas import QueryResponse
from tests.conftest import auth_header


# ============================================================================
# QUERY ROUTER -- Health endpoints (no auth)
# ============================================================================


class TestHealthEndpoints:
    """Tests for public health-check endpoints."""

    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    async def test_health_databases_no_connections(self, client):
        """With no registered databases the overall status is still ok."""
        with patch(
            "app.routers.query.connection_manager.list_aliases", return_value=[]
        ):
            resp = await client.get("/health/databases")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["databases"] == {}

    async def test_health_databases_with_healthy_db(self, client):
        with patch(
            "app.routers.query.connection_manager.list_aliases",
            return_value=["testdb"],
        ), patch(
            "app.routers.query.connection_manager.test_connection",
            new_callable=AsyncMock,
            return_value=True,
        ):
            resp = await client.get("/health/databases")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["databases"]["testdb"]["status"] == "ok"

    async def test_health_databases_with_unhealthy_db(self, client):
        with patch(
            "app.routers.query.connection_manager.list_aliases",
            return_value=["baddb"],
        ), patch(
            "app.routers.query.connection_manager.test_connection",
            new_callable=AsyncMock,
            return_value=False,
        ):
            resp = await client.get("/health/databases")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["databases"]["baddb"]["status"] == "error"


# ============================================================================
# QUERY ROUTER -- Execute endpoint
# ============================================================================


def _mock_query_response() -> QueryResponse:
    return QueryResponse(
        columns=["id", "name"],
        rows=[[1, "alice"]],
        row_count=1,
        truncated=False,
        elapsed_ms=42,
    )


class TestQueryExecute:
    """Tests for POST /query/execute."""

    async def test_admin_executes_query_bypasses_per_db_check(
        self, client, admin_token
    ):
        """Admin has query.databases.write so per-DB Permission check is skipped."""
        mock_engine = MagicMock()
        with patch(
            "app.routers.query.connection_manager.get_engine",
            return_value=mock_engine,
        ), patch(
            "app.routers.query.connection_manager.get_db_type",
            return_value="postgres",
        ), patch(
            "app.routers.query.execute_query",
            new_callable=AsyncMock,
            return_value=_mock_query_response(),
        ) as mock_exec, patch(
            "app.routers.query.log_query",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/query/execute",
                json={"database": "mydb", "sql": "SELECT 1"},
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"] == ["id", "name"]
        assert data["row_count"] == 1
        mock_exec.assert_awaited_once()

    async def test_developer_with_permission_entry_can_execute(
        self, client, admin_token, developer_token
    ):
        """Developer with a Permission row allowing SELECT can run a SELECT."""
        # First, create a DB connection via admin API (mocking engine creation)
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
                    "alias": "devdb",
                    "db_type": "postgres",
                    "host": "localhost",
                    "port": 5432,
                    "database": "testdb",
                    "username": "user",
                    "password": "pass",
                },
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 201

        # Create a Permission entry for the developer role on devdb
        resp = await client.put(
            "/admin/query/permissions",
            json={
                "role": "developer",
                "db_alias": "devdb",
                "allow_select": True,
                "allow_insert": False,
                "allow_update": False,
                "allow_delete": False,
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200

        # Developer executes a SELECT
        mock_engine = MagicMock()
        with patch(
            "app.routers.query.connection_manager.get_engine",
            return_value=mock_engine,
        ), patch(
            "app.routers.query.connection_manager.get_db_type",
            return_value="postgres",
        ), patch(
            "app.routers.query.execute_query",
            new_callable=AsyncMock,
            return_value=_mock_query_response(),
        ), patch(
            "app.routers.query.log_query",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/query/execute",
                json={"database": "devdb", "sql": "SELECT * FROM users"},
                headers=auth_header(developer_token),
            )

        assert resp.status_code == 200
        assert resp.json()["row_count"] == 1

    async def test_developer_without_permission_entry_gets_403(
        self, client, developer_token
    ):
        """Developer with no Permission row for the database gets 403."""
        mock_engine = MagicMock()
        with patch(
            "app.routers.query.connection_manager.get_engine",
            return_value=mock_engine,
        ), patch(
            "app.routers.query.connection_manager.get_db_type",
            return_value="postgres",
        ):
            resp = await client.post(
                "/query/execute",
                json={"database": "nopermdb", "sql": "SELECT 1"},
                headers=auth_header(developer_token),
            )

        assert resp.status_code == 403
        assert "No permissions configured" in resp.json()["detail"]

    async def test_developer_wrong_statement_type_gets_403(
        self, client, admin_token, developer_token
    ):
        """Developer allowed only SELECT cannot run INSERT."""
        # Create DB connection
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
                    "alias": "insertdb",
                    "db_type": "postgres",
                    "host": "localhost",
                    "port": 5432,
                    "database": "testdb",
                    "username": "user",
                    "password": "pass",
                },
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 201

        # Permission: SELECT only
        resp = await client.put(
            "/admin/query/permissions",
            json={
                "role": "developer",
                "db_alias": "insertdb",
                "allow_select": True,
                "allow_insert": False,
                "allow_update": False,
                "allow_delete": False,
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200

        # Developer tries INSERT
        mock_engine = MagicMock()
        with patch(
            "app.routers.query.connection_manager.get_engine",
            return_value=mock_engine,
        ), patch(
            "app.routers.query.connection_manager.get_db_type",
            return_value="postgres",
        ):
            resp = await client.post(
                "/query/execute",
                json={
                    "database": "insertdb",
                    "sql": "INSERT INTO users (name) VALUES ('bob')",
                },
                headers=auth_header(developer_token),
            )

        assert resp.status_code == 403
        assert "not allowed to execute INSERT" in resp.json()["detail"]

    async def test_unknown_database_alias_returns_404(self, client, admin_token):
        """Requesting a non-existent database alias yields 404."""
        with patch(
            "app.routers.query.connection_manager.get_engine",
            side_effect=KeyError("No engine"),
        ):
            resp = await client.post(
                "/query/execute",
                json={"database": "ghost", "sql": "SELECT 1"},
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 404
        assert "not registered" in resp.json()["detail"]

    async def test_viewer_cannot_execute_query(self, client, viewer_token):
        """Viewer role lacks query.execute permission entirely."""
        resp = await client.post(
            "/query/execute",
            json={"database": "anydb", "sql": "SELECT 1"},
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    async def test_execute_without_auth_returns_401(self, client):
        """No token at all yields 401 (or 403 from HTTPBearer)."""
        resp = await client.post(
            "/query/execute",
            json={"database": "anydb", "sql": "SELECT 1"},
        )
        assert resp.status_code == 403  # HTTPBearer returns 403 when no creds


# ============================================================================
# QUERY ROUTER -- List databases endpoint
# ============================================================================


class TestListDatabases:
    """Tests for GET /query/databases."""

    async def _create_db_connection(self, client, admin_token, alias):
        """Helper to create a DB connection via admin API."""
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
                    "database": f"{alias}_db",
                    "username": "user",
                    "password": "pass",
                },
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 201

    async def test_admin_sees_all_databases(self, client, admin_token):
        """Admin (query.databases.write) sees every registered connection."""
        await self._create_db_connection(client, admin_token, "admindb1")
        await self._create_db_connection(client, admin_token, "admindb2")

        with patch(
            "app.routers.query.connection_manager.get_status",
            return_value={"status": "registered"},
        ):
            resp = await client.get(
                "/query/databases",
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        aliases = [db["alias"] for db in resp.json()]
        assert "admindb1" in aliases
        assert "admindb2" in aliases

    async def test_developer_sees_only_permitted_databases(
        self, client, admin_token, developer_token
    ):
        """Developer without query.databases.write sees only DBs with Permission rows."""
        await self._create_db_connection(client, admin_token, "visibledb")
        await self._create_db_connection(client, admin_token, "hiddendb")

        # Grant developer permission on visibledb only
        resp = await client.put(
            "/admin/query/permissions",
            json={
                "role": "developer",
                "db_alias": "visibledb",
                "allow_select": True,
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200

        with patch(
            "app.routers.query.connection_manager.get_status",
            return_value={"status": "registered"},
        ):
            resp = await client.get(
                "/query/databases",
                headers=auth_header(developer_token),
            )

        assert resp.status_code == 200
        aliases = [db["alias"] for db in resp.json()]
        assert "visibledb" in aliases
        assert "hiddendb" not in aliases

    async def test_developer_with_no_permissions_gets_empty_list(
        self, client, developer_token
    ):
        """Developer with zero Permission entries gets an empty list."""
        # No Permission rows exist for developer on any DB in a fresh test
        resp = await client.get(
            "/query/databases",
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_viewer_cannot_list_databases(self, client, viewer_token):
        """Viewer lacks query.databases.read permission."""
        resp = await client.get(
            "/query/databases",
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403


# ============================================================================
# ROLES ROUTER -- Public endpoints
# ============================================================================


class TestAuthRolesPublic:
    """Tests for GET /auth/roles (public, no auth needed)."""

    async def test_list_auth_roles_returns_seeded_roles(self, client):
        resp = await client.get("/auth/roles")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Seeded roles are admin, developer, viewer -- returned sorted by name
        assert data == ["admin", "developer", "viewer"]


# ============================================================================
# ROLES ROUTER -- Current user info
# ============================================================================


class TestAuthMe:
    """Tests for GET /auth/me."""

    async def test_admin_me_returns_all_permissions(self, client, admin_token):
        resp = await client.get("/auth/me", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testadmin"
        assert data["role"] == "admin"
        assert len(data["permissions"]) == len(ALL_PERMISSIONS)
        assert set(data["permissions"]) == set(ALL_PERMISSIONS)

    async def test_viewer_me_returns_limited_permissions(self, client, viewer_token):
        resp = await client.get("/auth/me", headers=auth_header(viewer_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testviewer"
        assert data["role"] == "viewer"
        assert len(data["permissions"]) == 2
        assert set(data["permissions"]) == {
            "gateway.monitoring.read",
            "query.audit.read",
        }

    async def test_me_without_token_returns_401(self, client):
        """No Authorization header yields 403 from HTTPBearer (no credentials)."""
        resp = await client.get("/auth/me")
        assert resp.status_code == 403  # HTTPBearer returns 403 when missing


# ============================================================================
# ROLES ROUTER -- Permission list
# ============================================================================


class TestAdminPermissions:
    """Tests for GET /admin/permissions."""

    async def test_admin_can_list_all_permissions(self, client, admin_token):
        resp = await client.get(
            "/admin/permissions", headers=auth_header(admin_token)
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 15
        assert data == ALL_PERMISSIONS

    async def test_viewer_cannot_list_permissions(self, client, viewer_token):
        resp = await client.get(
            "/admin/permissions", headers=auth_header(viewer_token)
        )
        assert resp.status_code == 403


# ============================================================================
# ROLES ROUTER -- Role CRUD
# ============================================================================


class TestRoleList:
    """Tests for GET /admin/roles."""

    async def test_list_roles_returns_seeded_roles(self, client, admin_token):
        resp = await client.get("/admin/roles", headers=auth_header(admin_token))
        assert resp.status_code == 200
        roles = resp.json()
        assert len(roles) >= 3
        names = [r["name"] for r in roles]
        assert "admin" in names
        assert "developer" in names
        assert "viewer" in names

    async def test_seeded_roles_have_correct_permissions(self, client, admin_token):
        resp = await client.get("/admin/roles", headers=auth_header(admin_token))
        roles_by_name = {r["name"]: r for r in resp.json()}

        admin_role = roles_by_name["admin"]
        assert set(admin_role["permissions"]) == set(ALL_PERMISSIONS)
        assert admin_role["is_system"] is True

        dev_role = roles_by_name["developer"]
        assert "query.execute" in dev_role["permissions"]
        assert "query.databases.read" in dev_role["permissions"]
        assert "query.databases.write" not in dev_role["permissions"]

        viewer_role = roles_by_name["viewer"]
        assert len(viewer_role["permissions"]) == 2


class TestRoleGetById:
    """Tests for GET /admin/roles/{role_id}."""

    async def test_get_single_role(self, client, admin_token):
        # Get list to find an ID
        resp = await client.get("/admin/roles", headers=auth_header(admin_token))
        roles = resp.json()
        role_id = roles[0]["id"]

        resp = await client.get(
            f"/admin/roles/{role_id}", headers=auth_header(admin_token)
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == role_id

    async def test_get_nonexistent_role_returns_404(self, client, admin_token):
        resp = await client.get(
            "/admin/roles/99999", headers=auth_header(admin_token)
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


class TestRoleCreate:
    """Tests for POST /admin/roles."""

    async def test_create_role_with_valid_permissions(self, client, admin_token):
        resp = await client.post(
            "/admin/roles",
            json={
                "name": "analyst",
                "description": "Data analyst role",
                "permissions": ["query.execute", "query.databases.read"],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "analyst"
        assert data["description"] == "Data analyst role"
        assert data["is_system"] is False
        assert set(data["permissions"]) == {"query.execute", "query.databases.read"}

    async def test_create_duplicate_role_returns_409(self, client, admin_token):
        resp = await client.post(
            "/admin/roles",
            json={"name": "admin", "permissions": []},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    async def test_create_role_with_invalid_permission_returns_400(
        self, client, admin_token
    ):
        resp = await client.post(
            "/admin/roles",
            json={
                "name": "badrole",
                "permissions": ["query.execute", "totally.fake.perm"],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "Invalid permissions" in resp.json()["detail"]

    async def test_create_role_with_empty_permissions(self, client, admin_token):
        resp = await client.post(
            "/admin/roles",
            json={"name": "emptyrole", "permissions": []},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        assert resp.json()["permissions"] == []


class TestRoleUpdate:
    """Tests for PUT /admin/roles/{role_id}."""

    async def _create_custom_role(self, client, admin_token, name="updatable"):
        resp = await client.post(
            "/admin/roles",
            json={
                "name": name,
                "description": "Original description",
                "permissions": ["query.execute"],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    async def test_update_role_description(self, client, admin_token):
        role_id = await self._create_custom_role(client, admin_token, "desc_update")
        resp = await client.put(
            f"/admin/roles/{role_id}",
            json={"description": "Updated description"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated description"
        # Permissions should remain unchanged
        assert "query.execute" in resp.json()["permissions"]

    async def test_update_role_permissions(self, client, admin_token):
        role_id = await self._create_custom_role(client, admin_token, "perm_update")
        resp = await client.put(
            f"/admin/roles/{role_id}",
            json={"permissions": ["query.databases.read", "query.audit.read"]},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["permissions"]) == {
            "query.databases.read",
            "query.audit.read",
        }
        # The old permission should be gone (replaced, not appended)
        assert "query.execute" not in data["permissions"]

    async def test_update_role_with_invalid_permission_returns_400(
        self, client, admin_token
    ):
        role_id = await self._create_custom_role(
            client, admin_token, "invalid_perm_update"
        )
        resp = await client.put(
            f"/admin/roles/{role_id}",
            json={"permissions": ["does.not.exist"]},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "Invalid permissions" in resp.json()["detail"]

    async def test_update_nonexistent_role_returns_404(self, client, admin_token):
        resp = await client.put(
            "/admin/roles/99999",
            json={"description": "nope"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 404


class TestRoleDelete:
    """Tests for DELETE /admin/roles/{role_id}."""

    async def test_delete_custom_role(self, client, admin_token):
        # Create a custom (non-system) role first
        resp = await client.post(
            "/admin/roles",
            json={"name": "deleteme", "permissions": []},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        role_id = resp.json()["id"]

        resp = await client.delete(
            f"/admin/roles/{role_id}", headers=auth_header(admin_token)
        )
        assert resp.status_code == 204

        # Confirm it is gone
        resp = await client.get(
            f"/admin/roles/{role_id}", headers=auth_header(admin_token)
        )
        assert resp.status_code == 404

    async def test_delete_system_role_returns_400(self, client, admin_token):
        """System roles (admin, developer, viewer) cannot be deleted."""
        # Find the admin role ID
        resp = await client.get("/admin/roles", headers=auth_header(admin_token))
        admin_role = next(r for r in resp.json() if r["name"] == "admin")
        role_id = admin_role["id"]

        resp = await client.delete(
            f"/admin/roles/{role_id}", headers=auth_header(admin_token)
        )
        assert resp.status_code == 400
        assert "Cannot delete system role" in resp.json()["detail"]

    async def test_delete_nonexistent_role_returns_404(self, client, admin_token):
        resp = await client.delete(
            "/admin/roles/99999", headers=auth_header(admin_token)
        )
        assert resp.status_code == 404


# ============================================================================
# ROLES ROUTER -- Permission enforcement
# ============================================================================


class TestRolePermissionEnforcement:
    """Developer and viewer cannot access admin role management endpoints."""

    async def test_developer_cannot_create_role(self, client, developer_token):
        resp = await client.post(
            "/admin/roles",
            json={"name": "sneaky", "permissions": []},
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 403

    async def test_developer_cannot_update_role(self, client, developer_token):
        resp = await client.put(
            "/admin/roles/1",
            json={"description": "hacked"},
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 403

    async def test_developer_cannot_delete_role(self, client, developer_token):
        resp = await client.delete(
            "/admin/roles/1",
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 403

    async def test_developer_cannot_list_admin_roles(self, client, developer_token):
        """Developer lacks admin.roles.read."""
        resp = await client.get(
            "/admin/roles", headers=auth_header(developer_token)
        )
        assert resp.status_code == 403

    async def test_developer_cannot_get_role_by_id(self, client, developer_token):
        resp = await client.get(
            "/admin/roles/1", headers=auth_header(developer_token)
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_list_admin_roles(self, client, viewer_token):
        resp = await client.get(
            "/admin/roles", headers=auth_header(viewer_token)
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_create_role(self, client, viewer_token):
        resp = await client.post(
            "/admin/roles",
            json={"name": "nope", "permissions": []},
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_update_role(self, client, viewer_token):
        resp = await client.put(
            "/admin/roles/1",
            json={"description": "nope"},
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_delete_role(self, client, viewer_token):
        resp = await client.delete(
            "/admin/roles/1",
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_list_permissions(self, client, viewer_token):
        resp = await client.get(
            "/admin/permissions", headers=auth_header(viewer_token)
        )
        assert resp.status_code == 403
