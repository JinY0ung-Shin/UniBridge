"""Tests for user management endpoints (Keycloak Admin proxy)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import auth_header


# ── Mock helpers ─────────────────────────────────────────────────────────────

def _make_kc_mock() -> AsyncMock:
    """Create an AsyncMock that simulates KeycloakAdminClient."""
    kc = AsyncMock()
    # Default: list_users returns one user
    kc.list_users.return_value = (
        [
            {
                "id": "user-1",
                "username": "alice",
                "email": "alice@example.com",
                "enabled": True,
                "createdTimestamp": 1700000000000,
            }
        ],
        1,
    )
    # Default: user realm roles
    kc.get_user_realm_roles.return_value = [
        {"id": "role-id-dev", "name": "developer"},
    ]
    # Default: create_user returns a new user ID
    kc.create_user.return_value = "new-user-id"
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
    mock = _make_kc_mock()
    with patch("app.routers.users._get_kc_admin", return_value=mock):
        yield mock


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
        assert data["id"] == "new-user-id"
        assert data["username"] == "newuser"
        assert data["role"] == "viewer"

        kc_mock.create_user.assert_awaited_once_with(
            username="newuser",
            email="new@example.com",
            password="securepass123",
            enabled=True,
        )
        kc_mock.assign_realm_role.assert_awaited_once_with("new-user-id", "viewer")

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
        resp = await client.put(
            "/admin/users/user-1/role",
            json={"role": "admin"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "admin"

        # Should have removed old "developer" role
        kc_mock.remove_realm_role.assert_awaited_once_with("user-1", "developer")
        # Should have assigned new "admin" role
        kc_mock.assign_realm_role.assert_awaited_once_with("user-1", "admin")


# ═══════════════════════════════════════════════════════════════════════════════
# PUT /admin/users/{user_id}/reset-password
# ═══════════════════════════════════════════════════════════════════════════════


class TestResetPassword:
    async def test_reset_password_success(self, client, admin_token, kc_mock):
        resp = await client.put(
            "/admin/users/user-1/reset-password",
            json={"password": "newpassword123", "temporary": True},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204

        kc_mock.reset_password.assert_awaited_once_with("user-1", "newpassword123", True)


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE /admin/users/{user_id}
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeleteUser:
    async def test_delete_user_success(self, client, admin_token, kc_mock):
        resp = await client.delete(
            "/admin/users/user-1",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204
        kc_mock.delete_user.assert_awaited_once_with("user-1")

    async def test_cannot_delete_self(self, client, admin_token, kc_mock):
        # The admin token username is "testadmin"
        kc_mock.list_users.return_value = (
            [
                {
                    "id": "self-user-id",
                    "username": "testadmin",
                    "email": "admin@example.com",
                    "enabled": True,
                }
            ],
            1,
        )
        resp = await client.delete(
            "/admin/users/self-user-id",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "Cannot delete your own account" in resp.json()["detail"]
