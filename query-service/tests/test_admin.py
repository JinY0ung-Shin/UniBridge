"""Comprehensive integration tests for the admin router."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import auth_header

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DB_PAYLOAD = {
    "alias": "testdb",
    "db_type": "postgres",
    "host": "localhost",
    "port": 5432,
    "database": "mydb",
    "username": "pguser",
    "password": "secret",
}


def _make_db_payload(**overrides) -> dict:
    """Return a fresh DB connection payload with optional overrides."""
    payload = {**DB_PAYLOAD, **overrides}
    return payload


PERMISSION_PAYLOAD = {
    "role": "developer",
    "db_alias": "testdb",
    "allow_select": True,
    "allow_insert": False,
    "allow_update": False,
    "allow_delete": False,
}


def _cm_patch():
    """Return a context-manager that patches the connection_manager singleton.

    Mocked methods:
      - add_connection  (async, no-op)
      - remove_connection (async, no-op)
      - get_status  -> {"status": "registered"}
      - get_engine  -> MagicMock
      - test_connection (async) -> True
    """
    mock_cm = MagicMock()
    mock_cm.add_connection = AsyncMock()
    mock_cm.remove_connection = AsyncMock()
    mock_cm.get_status = MagicMock(return_value={"status": "registered"})
    mock_cm.get_engine = MagicMock(return_value=MagicMock())
    mock_cm.test_connection = AsyncMock(return_value=True)
    return patch("app.routers.admin.connection_manager", mock_cm)


# ===========================================================================
# Database Connection CRUD
# ===========================================================================


class TestCreateConnection:
    """POST /admin/query/databases"""

    @pytest.mark.asyncio
    async def test_create_connection_success(self, client, admin_token):
        with _cm_patch():
            resp = await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["alias"] == "testdb"
        assert data["db_type"] == "postgres"
        assert data["host"] == "localhost"
        assert data["port"] == 5432
        assert data["database"] == "mydb"
        assert data["username"] == "pguser"
        assert data["pool_size"] == 5
        assert data["max_overflow"] == 3
        assert data["query_timeout"] == 30
        assert data["status"] == "registered"
        # password must not be present in the response
        assert "password" not in data
        assert "password_encrypted" not in data

    @pytest.mark.asyncio
    async def test_create_duplicate_alias_returns_409(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
            resp = await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_connection_custom_pool_settings(self, client, admin_token):
        with _cm_patch():
            resp = await client.post(
                "/admin/query/databases",
                json=_make_db_payload(
                    alias="custom_pool",
                    pool_size=10,
                    max_overflow=5,
                    query_timeout=60,
                ),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["pool_size"] == 10
        assert data["max_overflow"] == 5
        assert data["query_timeout"] == 60

    @pytest.mark.asyncio
    async def test_create_connection_engine_failure_still_creates(
        self, client, admin_token
    ):
        """Even if connection_manager.add_connection raises, the DB record is
        persisted and the response shows status='error'."""
        with _cm_patch() as ctx:
            ctx.add_connection = AsyncMock(side_effect=RuntimeError("boom"))
            resp = await client.post(
                "/admin/query/databases",
                json=_make_db_payload(alias="failengine"),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 201
        assert resp.json()["status"] == "error"


class TestListConnections:
    """GET /admin/query/databases"""

    @pytest.mark.asyncio
    async def test_list_empty(self, client, admin_token):
        with _cm_patch():
            resp = await client.get(
                "/admin/query/databases",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_created_entries(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(alias="db1"),
                headers=auth_header(admin_token),
            )
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(alias="db2"),
                headers=auth_header(admin_token),
            )
            resp = await client.get(
                "/admin/query/databases",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        aliases = {c["alias"] for c in resp.json()}
        assert aliases == {"db1", "db2"}


class TestGetConnection:
    """GET /admin/query/databases/{alias}"""

    @pytest.mark.asyncio
    async def test_get_existing_connection(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
            resp = await client.get(
                "/admin/query/databases/testdb",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["alias"] == "testdb"
        assert data["status"] == "registered"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_404(self, client, admin_token):
        with _cm_patch():
            resp = await client.get(
                "/admin/query/databases/nope",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]


class TestUpdateConnection:
    """PUT /admin/query/databases/{alias}"""

    @pytest.mark.asyncio
    async def test_update_connection_fields(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
            resp = await client.put(
                "/admin/query/databases/testdb",
                json={"host": "newhost", "port": 5433, "pool_size": 20},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["host"] == "newhost"
        assert data["port"] == 5433
        assert data["pool_size"] == 20
        # Unchanged fields persist
        assert data["alias"] == "testdb"
        assert data["db_type"] == "postgres"

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_404(self, client, admin_token):
        with _cm_patch():
            resp = await client.put(
                "/admin/query/databases/nope",
                json={"host": "x"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_password(self, client, admin_token):
        """Password update should succeed without leaking the password."""
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
            resp = await client.put(
                "/admin/query/databases/testdb",
                json={"password": "new-secret-pw"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "password" not in data
        assert "password_encrypted" not in data


class TestDeleteConnection:
    """DELETE /admin/query/databases/{alias}"""

    @pytest.mark.asyncio
    async def test_delete_connection(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
            resp = await client.delete(
                "/admin/query/databases/testdb",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client, admin_token):
        with _cm_patch():
            resp = await client.delete(
                "/admin/query/databases/nope",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_then_get_returns_404(self, client, admin_token):
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
            await client.delete(
                "/admin/query/databases/testdb",
                headers=auth_header(admin_token),
            )
            resp = await client.get(
                "/admin/query/databases/testdb",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 404


class TestTestConnection:
    """POST /admin/query/databases/{alias}/test"""

    @pytest.mark.asyncio
    async def test_connection_ok(self, client, admin_token):
        with _cm_patch():
            resp = await client.post(
                "/admin/query/databases/anydb/test",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["alias"] == "anydb"

    @pytest.mark.asyncio
    async def test_connection_not_registered(self, client, admin_token):
        with _cm_patch() as ctx:
            ctx.get_engine = MagicMock(side_effect=KeyError("nope"))
            resp = await client.post(
                "/admin/query/databases/nope/test",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 404
        assert "not registered" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_connection_fails(self, client, admin_token):
        with _cm_patch() as ctx:
            ctx.test_connection = AsyncMock(return_value=False)
            resp = await client.post(
                "/admin/query/databases/baddb/test",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"
        assert resp.json()["message"] == "Connection failed"


# ===========================================================================
# Permission Checks (RBAC)
# ===========================================================================


class TestPermissionChecks:
    """Verify that role-based permission enforcement works correctly."""

    @pytest.mark.asyncio
    async def test_create_connection_viewer_forbidden(self, client, viewer_token):
        """Viewer role lacks query.databases.write -> 403."""
        with _cm_patch():
            resp = await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(viewer_token),
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_connections_developer_allowed(self, client, developer_token):
        """Developer has query.databases.read -> 200."""
        with _cm_patch():
            resp = await client.get(
                "/admin/query/databases",
                headers=auth_header(developer_token),
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_connection_developer_forbidden(
        self, client, admin_token, developer_token
    ):
        """Developer lacks query.databases.write -> 403 on DELETE."""
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
            resp = await client.delete(
                "/admin/query/databases/testdb",
                headers=auth_header(developer_token),
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_update_connection_developer_forbidden(self, client, developer_token):
        """Developer lacks query.databases.write -> 403 on PUT."""
        with _cm_patch():
            resp = await client.put(
                "/admin/query/databases/testdb",
                json={"host": "newhost"},
                headers=auth_header(developer_token),
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_no_token_returns_403(self, client):
        """FastAPI HTTPBearer returns 403 when Authorization header is missing."""
        with _cm_patch():
            resp = await client.get("/admin/query/databases")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_get_single_connection_developer_allowed(
        self, client, admin_token, developer_token
    ):
        """Developer has query.databases.read -> 200 on GET single."""
        with _cm_patch():
            await client.post(
                "/admin/query/databases",
                json=_make_db_payload(),
                headers=auth_header(admin_token),
            )
            resp = await client.get(
                "/admin/query/databases/testdb",
                headers=auth_header(developer_token),
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_test_connection_developer_allowed(self, client, developer_token):
        """Developer has query.databases.read -> 200 on test endpoint."""
        with _cm_patch():
            resp = await client.post(
                "/admin/query/databases/anydb/test",
                headers=auth_header(developer_token),
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_test_connection_viewer_forbidden(self, client, viewer_token):
        """Viewer lacks query.databases.read -> 403 on test endpoint."""
        with _cm_patch():
            resp = await client.post(
                "/admin/query/databases/anydb/test",
                headers=auth_header(viewer_token),
            )
        assert resp.status_code == 403


# ===========================================================================
# DB Permissions CRUD
# ===========================================================================


class TestListPermissions:
    """GET /admin/query/permissions"""

    @pytest.mark.asyncio
    async def test_list_permissions_empty(self, client, admin_token):
        resp = await client.get(
            "/admin/query/permissions",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_permissions_after_upsert(self, client, admin_token):
        await client.put(
            "/admin/query/permissions",
            json=PERMISSION_PAYLOAD,
            headers=auth_header(admin_token),
        )
        resp = await client.get(
            "/admin/query/permissions",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        perms = resp.json()
        assert len(perms) == 1
        assert perms[0]["role"] == "developer"
        assert perms[0]["db_alias"] == "testdb"


class TestUpsertPermission:
    """PUT /admin/query/permissions"""

    @pytest.mark.asyncio
    async def test_upsert_creates_new(self, client, admin_token):
        resp = await client.put(
            "/admin/query/permissions",
            json=PERMISSION_PAYLOAD,
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "developer"
        assert data["db_alias"] == "testdb"
        assert data["allow_select"] is True
        assert data["allow_insert"] is False
        assert data["allow_update"] is False
        assert data["allow_delete"] is False
        assert "id" in data

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, client, admin_token):
        """PUT with same role+db_alias updates rather than creating a duplicate."""
        resp1 = await client.put(
            "/admin/query/permissions",
            json=PERMISSION_PAYLOAD,
            headers=auth_header(admin_token),
        )
        perm_id = resp1.json()["id"]

        updated_payload = {
            **PERMISSION_PAYLOAD,
            "allow_insert": True,
            "allow_update": True,
        }
        resp2 = await client.put(
            "/admin/query/permissions",
            json=updated_payload,
            headers=auth_header(admin_token),
        )
        assert resp2.status_code == 200
        data = resp2.json()
        # Same ID (updated, not duplicated)
        assert data["id"] == perm_id
        assert data["allow_insert"] is True
        assert data["allow_update"] is True
        assert data["allow_select"] is True

    @pytest.mark.asyncio
    async def test_upsert_different_role_creates_new(self, client, admin_token):
        """Different role + same db_alias creates a separate permission."""
        await client.put(
            "/admin/query/permissions",
            json=PERMISSION_PAYLOAD,
            headers=auth_header(admin_token),
        )
        resp = await client.put(
            "/admin/query/permissions",
            json={**PERMISSION_PAYLOAD, "role": "viewer"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "viewer"

        list_resp = await client.get(
            "/admin/query/permissions",
            headers=auth_header(admin_token),
        )
        assert len(list_resp.json()) == 2


class TestDeletePermission:
    """DELETE /admin/query/permissions/{id}"""

    @pytest.mark.asyncio
    async def test_delete_permission(self, client, admin_token):
        create_resp = await client.put(
            "/admin/query/permissions",
            json=PERMISSION_PAYLOAD,
            headers=auth_header(admin_token),
        )
        perm_id = create_resp.json()["id"]

        resp = await client.delete(
            f"/admin/query/permissions/{perm_id}",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client, admin_token):
        resp = await client.delete(
            "/admin/query/permissions/99999",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_then_list_is_empty(self, client, admin_token):
        create_resp = await client.put(
            "/admin/query/permissions",
            json=PERMISSION_PAYLOAD,
            headers=auth_header(admin_token),
        )
        perm_id = create_resp.json()["id"]
        await client.delete(
            f"/admin/query/permissions/{perm_id}",
            headers=auth_header(admin_token),
        )

        list_resp = await client.get(
            "/admin/query/permissions",
            headers=auth_header(admin_token),
        )
        assert list_resp.json() == []


class TestPermissionsRBAC:
    """Permission endpoint RBAC checks."""

    @pytest.mark.asyncio
    async def test_viewer_cannot_list_permissions(self, client, viewer_token):
        """Viewer lacks query.permissions.read -> 403."""
        resp = await client.get(
            "/admin/query/permissions",
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_developer_can_list_permissions(self, client, developer_token):
        """Developer has query.permissions.read -> 200."""
        resp = await client.get(
            "/admin/query/permissions",
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_developer_cannot_upsert_permissions(self, client, developer_token):
        """Developer lacks query.permissions.write -> 403."""
        resp = await client.put(
            "/admin/query/permissions",
            json=PERMISSION_PAYLOAD,
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_developer_cannot_delete_permissions(self, client, developer_token):
        """Developer lacks query.permissions.write -> 403."""
        resp = await client.delete(
            "/admin/query/permissions/1",
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_upsert_permissions(self, client, viewer_token):
        resp = await client.put(
            "/admin/query/permissions",
            json=PERMISSION_PAYLOAD,
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_no_token_permissions_returns_403(self, client):
        resp = await client.get("/admin/query/permissions")
        assert resp.status_code == 403


# ===========================================================================
# Audit Logs
# ===========================================================================


class TestAuditLogs:
    """GET /admin/query/audit-logs"""

    @pytest.mark.asyncio
    async def test_list_audit_logs_empty(self, client, admin_token):
        resp = await client.get(
            "/admin/query/audit-logs",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_viewer_can_read_audit_logs(self, client, viewer_token):
        """Viewer has query.audit.read -> 200."""
        resp = await client.get(
            "/admin/query/audit-logs",
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_developer_can_read_audit_logs(self, client, developer_token):
        """Developer has query.audit.read -> 200."""
        resp = await client.get(
            "/admin/query/audit-logs",
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_token_audit_logs_returns_403(self, client):
        resp = await client.get("/admin/query/audit-logs")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_filter_by_database(self, client, admin_token, _seed_audit_logs):
        resp = await client.get(
            "/admin/query/audit-logs",
            params={"database": "proddb"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        logs = resp.json()
        assert all(log["database_alias"] == "proddb" for log in logs)

    @pytest.mark.asyncio
    async def test_filter_by_user(self, client, admin_token, _seed_audit_logs):
        resp = await client.get(
            "/admin/query/audit-logs",
            params={"user": "alice"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        logs = resp.json()
        assert all(log["user"] == "alice" for log in logs)

    @pytest.mark.asyncio
    async def test_filter_by_from_date(self, client, admin_token, _seed_audit_logs):
        resp = await client.get(
            "/admin/query/audit-logs",
            params={"from_date": "2026-01-02T00:00:00"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        logs = resp.json()
        # Only the log at 2026-01-02 and 2026-01-03 should be included
        assert len(logs) >= 1

    @pytest.mark.asyncio
    async def test_filter_by_to_date(self, client, admin_token, _seed_audit_logs):
        resp = await client.get(
            "/admin/query/audit-logs",
            params={"to_date": "2026-01-01T23:59:59"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        logs = resp.json()
        assert len(logs) >= 1

    @pytest.mark.asyncio
    async def test_filter_combined_from_and_to(
        self, client, admin_token, _seed_audit_logs
    ):
        resp = await client.get(
            "/admin/query/audit-logs",
            params={
                "from_date": "2026-01-01T00:00:00",
                "to_date": "2026-01-02T23:59:59",
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_limit_and_offset(self, client, admin_token, _seed_audit_logs):
        resp = await client.get(
            "/admin/query/audit-logs",
            params={"limit": 1, "offset": 0},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        assert len(resp.json()) <= 1

        resp2 = await client.get(
            "/admin/query/audit-logs",
            params={"limit": 1, "offset": 1},
            headers=auth_header(admin_token),
        )
        assert resp2.status_code == 200
        # Different page should return different (or empty) results
        if resp.json() and resp2.json():
            assert resp.json()[0]["id"] != resp2.json()[0]["id"]

    @pytest.mark.asyncio
    async def test_invalid_from_date_returns_400(self, client, admin_token):
        resp = await client.get(
            "/admin/query/audit-logs",
            params={"from_date": "not-a-date"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "Invalid from_date format" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_to_date_returns_400(self, client, admin_token):
        resp = await client.get(
            "/admin/query/audit-logs",
            params={"to_date": "nope"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "Invalid to_date format" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Fixture: seed audit log rows directly into the DB
# ---------------------------------------------------------------------------


@pytest.fixture
async def _seed_audit_logs(app):
    """Insert sample audit log rows into the test database."""
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.database import get_db
    from app.models import AuditLog

    # Obtain a session from the overridden dependency
    override = app.dependency_overrides[get_db]

    # The override is an async generator function; we need a raw session
    # from the same factory. Build one from the engine used in the override.
    # Easier: call the override and extract the session.
    gen = override()
    session: AsyncSession = await gen.__anext__()

    logs = [
        AuditLog(
            timestamp=datetime(2026, 1, 1, 10, 0, 0),
            user="alice",
            database_alias="proddb",
            sql="SELECT 1",
            status="success",
            row_count=1,
            elapsed_ms=5,
        ),
        AuditLog(
            timestamp=datetime(2026, 1, 2, 12, 0, 0),
            user="bob",
            database_alias="devdb",
            sql="SELECT 2",
            status="success",
            row_count=1,
            elapsed_ms=10,
        ),
        AuditLog(
            timestamp=datetime(2026, 1, 3, 14, 0, 0),
            user="alice",
            database_alias="proddb",
            sql="SELECT 3",
            status="error",
            error_message="timeout",
            elapsed_ms=30000,
        ),
    ]
    session.add_all(logs)
    await session.commit()

    yield

    # Cleanup: no action needed; the in-memory DB is torn down with the fixture.
    try:
        await gen.aclose()
    except StopAsyncIteration:
        pass
