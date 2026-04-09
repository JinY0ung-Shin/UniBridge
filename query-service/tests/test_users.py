"""Tests for user management endpoints (Keycloak Admin proxy)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import auth_header

# Valid UUIDs for test user IDs
USER_1_ID = "00000000-0000-4000-a000-000000000001"
NEW_USER_ID = "00000000-0000-4000-a000-000000000002"
SELF_USER_ID = "00000000-0000-4000-a000-000000000003"


# ── Mock helpers ─────────────────────────────────────────────────────────────

def _make_kc_mock() -> AsyncMock:
    """Create an AsyncMock that simulates KeycloakAdminClient."""
    kc = AsyncMock()
    # Default: list_users returns one user
    kc.list_users.return_value = (
        [
            {
                "id": USER_1_ID,
                "username": "alice",
                "email": "alice@example.com",
                "enabled": True,
                "createdTimestamp": 1700000000000,
            }
        ],
        1,
    )
    # Default: get_user returns a user dict
    kc.get_user.return_value = {
        "id": USER_1_ID,
        "username": "alice",
        "email": "alice@example.com",
        "enabled": True,
        "createdTimestamp": 1700000000000,
    }
    # Default: user realm roles
    kc.get_user_realm_roles.return_value = [
        {"id": "role-id-dev", "name": "developer"},
    ]
    # Default: create_user returns a new user ID
    kc.create_user.return_value = NEW_USER_ID
    # Default: realm roles available
    kc.get_realm_roles.return_value = [
        {"id": "role-id-admin", "name": "admin"},
        {"id": "role-id-dev", "name": "developer"},
        {"id": "role-id-viewer", "name": "viewer"},
    ]
    return kc


@pytest.fixture
def kc_mock():
    """Patch _get_kc_admin to return an AsyncMock KeycloakAdminClient."""
    import app.routers.users as users_mod
    # Reset singleton so tests are isolated
    users_mod._kc_admin = None
    mock = _make_kc_mock()
    with patch("app.routers.users._get_kc_admin", return_value=mock):
        yield mock
    # Clean up singleton after test
    users_mod._kc_admin = None


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/users
# ═══════════════════════════════════════════════════════════════════════════════


class TestListUsers:
    async def test_list_users_success(self, client, admin_token, kc_mock):
        resp = await client.get("/admin/users", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["users"]) == 1
        assert data["users"][0]["username"] == "alice"
        assert data["users"][0]["role"] == "developer"

    async def test_list_users_forbidden_for_viewer(self, client, viewer_token, kc_mock):
        resp = await client.get("/admin/users", headers=auth_header(viewer_token))
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# POST /admin/users
# ═══════════════════════════════════════════════════════════════════════════════


class TestCreateUser:
    async def test_create_user_success(self, client, admin_token, kc_mock):
        body = {
            "username": "newuser",
            "email": "new@example.com",
            "password": "securepass123",
            "role": "viewer",
        }
        resp = await client.post(
            "/admin/users", json=body, headers=auth_header(admin_token)
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == NEW_USER_ID
        assert data["username"] == "newuser"
        assert data["role"] == "viewer"

        kc_mock.create_user.assert_awaited_once_with(
            username="newuser",
            email="new@example.com",
            password="securepass123",
            enabled=True,
        )
        kc_mock.assign_realm_role.assert_awaited_once_with(NEW_USER_ID, "viewer")

    async def test_create_user_short_password_422(self, client, admin_token, kc_mock):
        body = {
            "username": "newuser",
            "email": "new@example.com",
            "password": "short",
            "role": "viewer",
        }
        resp = await client.post(
            "/admin/users", json=body, headers=auth_header(admin_token)
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# PUT /admin/users/{user_id}/role
# ═══════════════════════════════════════════════════════════════════════════════


class TestChangeRole:
    async def test_change_role_success(self, client, admin_token, kc_mock):
        # After role change, get_user_realm_roles returns the new role (admin)
        # The first call is for removing old roles, the second is for _enrich_user
        kc_mock.get_user_realm_roles.side_effect = [
            [{"id": "role-id-dev", "name": "developer"}],  # current roles check
            [{"id": "role-id-admin", "name": "admin"}],     # _enrich_user call
        ]
        resp = await client.put(
            f"/admin/users/{USER_1_ID}/role",
            json={"role": "admin"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "admin"

        # Should have assigned new "admin" role FIRST
        kc_mock.assign_realm_role.assert_awaited_once_with(USER_1_ID, "admin")
        # Should have removed old "developer" role
        kc_mock.remove_realm_role.assert_awaited_once_with(USER_1_ID, "developer")

    async def test_change_role_invalid_uuid_422(self, client, admin_token, kc_mock):
        resp = await client.put(
            "/admin/users/not-a-uuid/role",
            json={"role": "admin"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# PUT /admin/users/{user_id}/reset-password
# ═══════════════════════════════════════════════════════════════════════════════


class TestResetPassword:
    async def test_reset_password_success(self, client, admin_token, kc_mock):
        resp = await client.put(
            f"/admin/users/{USER_1_ID}/reset-password",
            json={"password": "newpassword123", "temporary": True},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204

        kc_mock.reset_password.assert_awaited_once_with(USER_1_ID, "newpassword123", True)

    async def test_reset_password_invalid_uuid_422(self, client, admin_token, kc_mock):
        resp = await client.put(
            "/admin/users/not-a-uuid/reset-password",
            json={"password": "newpassword123", "temporary": True},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE /admin/users/{user_id}
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeleteUser:
    async def test_delete_user_success(self, client, admin_token, kc_mock):
        resp = await client.delete(
            f"/admin/users/{USER_1_ID}",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204
        kc_mock.delete_user.assert_awaited_once_with(USER_1_ID)

    async def test_cannot_delete_self(self, client, admin_token, kc_mock):
        # The admin token username is "testadmin"
        kc_mock.get_user.return_value = {
            "id": SELF_USER_ID,
            "username": "testadmin",
            "email": "admin@example.com",
            "enabled": True,
        }
        resp = await client.delete(
            f"/admin/users/{SELF_USER_ID}",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "Cannot delete your own account" in resp.json()["detail"]

    async def test_delete_user_invalid_uuid_422(self, client, admin_token, kc_mock):
        resp = await client.delete(
            "/admin/users/not-a-uuid",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 422
