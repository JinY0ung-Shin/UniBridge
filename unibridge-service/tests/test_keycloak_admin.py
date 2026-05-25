"""Tests for the Keycloak Admin REST client."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from pytest_httpx import HTTPXMock

from app.keycloak_admin import KeycloakAdminClient


BASE_URL = "https://keycloak.test"
REALM = "unibridge"
CLIENT_ID = "svc-client"
CLIENT_SECRET = "svc-secret"
TOKEN_URL = f"{BASE_URL}/realms/{REALM}/protocol/openid-connect/token"
ADMIN_URL = f"{BASE_URL}/admin/realms/{REALM}"


@pytest.fixture
def kc_client():
    return KeycloakAdminClient(BASE_URL, REALM, CLIENT_ID, CLIENT_SECRET)


def _mock_token(httpx_mock: HTTPXMock, *, status_code: int = 200, body: dict | None = None) -> None:
    payload = body if body is not None else {"access_token": "tok-abc", "expires_in": 300}
    httpx_mock.add_response(
        method="POST",
        url=TOKEN_URL,
        status_code=status_code,
        json=payload,
    )


# ── Token management ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_token_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    token = await kc_client.get_token()
    assert token == "tok-abc"
    await kc_client.close()


@pytest.mark.asyncio
async def test_get_token_caches_until_expiry(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    first = await kc_client.get_token()
    second = await kc_client.get_token()
    assert first == second == "tok-abc"
    await kc_client.close()


@pytest.mark.asyncio
async def test_get_token_failure_raises_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock, status_code=401, body={"error": "invalid_client"})
    with pytest.raises(HTTPException) as exc:
        await kc_client.get_token()
    assert exc.value.status_code == 502
    await kc_client.close()


# ── User CRUD ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/user-1",
        json={"id": "user-1", "username": "alice"},
    )
    user = await kc_client.get_user("user-1")
    assert user["username"] == "alice"
    await kc_client.close()


@pytest.mark.asyncio
async def test_get_user_404(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/missing",
        status_code=404,
        json={"error": "not found"},
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.get_user("missing")
    assert exc.value.status_code == 404
    await kc_client.close()


@pytest.mark.asyncio
async def test_list_users_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/count?search=ali",
        json=2,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users?first=0&max=10&search=ali",
        json=[{"id": "1", "username": "alice"}, {"id": "2", "username": "alistair"}],
    )
    users, total = await kc_client.list_users(search="ali", first=0, max_results=10)
    assert total == 2
    assert len(users) == 2
    await kc_client.close()


@pytest.mark.asyncio
async def test_list_users_no_search(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/count",
        json=0,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users?first=0&max=50",
        json=[],
    )
    users, total = await kc_client.list_users()
    assert users == []
    assert total == 0
    await kc_client.close()


@pytest.mark.asyncio
async def test_list_users_count_failure_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/count",
        status_code=500,
        json={"error": "boom"},
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.list_users()
    assert exc.value.status_code == 502
    await kc_client.close()


@pytest.mark.asyncio
async def test_list_users_list_failure_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/count",
        json=0,
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users?first=0&max=50",
        status_code=500,
        json={"error": "boom"},
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.list_users()
    assert exc.value.status_code == 502
    await kc_client.close()


@pytest.mark.asyncio
async def test_create_user_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"{ADMIN_URL}/users",
        status_code=201,
        headers={"Location": f"{ADMIN_URL}/users/new-id"},
    )
    user_id = await kc_client.create_user(
        username="bob",
        email="bob@example.com",
        password="pw",
        enabled=True,
    )
    assert user_id == "new-id"
    await kc_client.close()


@pytest.mark.asyncio
async def test_create_user_no_optional_fields(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"{ADMIN_URL}/users",
        status_code=201,
        headers={"Location": f"{ADMIN_URL}/users/u-2"},
    )
    user_id = await kc_client.create_user(username="charlie")
    assert user_id == "u-2"
    await kc_client.close()


@pytest.mark.asyncio
async def test_create_user_conflict(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"{ADMIN_URL}/users",
        status_code=409,
        json={"errorMessage": "exists"},
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.create_user(username="bob")
    assert exc.value.status_code == 409
    await kc_client.close()


@pytest.mark.asyncio
async def test_create_user_other_error_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=f"{ADMIN_URL}/users",
        status_code=500,
        json={"error": "boom"},
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.create_user(username="bob")
    assert exc.value.status_code == 502
    await kc_client.close()


@pytest.mark.asyncio
async def test_update_user_enabled_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="PUT",
        url=f"{ADMIN_URL}/users/u-1",
        status_code=204,
    )
    await kc_client.update_user_enabled("u-1", False)
    await kc_client.close()


@pytest.mark.asyncio
async def test_update_user_enabled_404(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="PUT",
        url=f"{ADMIN_URL}/users/missing",
        status_code=404,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.update_user_enabled("missing", True)
    assert exc.value.status_code == 404
    await kc_client.close()


@pytest.mark.asyncio
async def test_update_user_enabled_other_error_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="PUT",
        url=f"{ADMIN_URL}/users/u-1",
        status_code=500,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.update_user_enabled("u-1", True)
    assert exc.value.status_code == 502
    await kc_client.close()


@pytest.mark.asyncio
async def test_delete_user_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="DELETE",
        url=f"{ADMIN_URL}/users/u-1",
        status_code=204,
    )
    await kc_client.delete_user("u-1")
    await kc_client.close()


@pytest.mark.asyncio
async def test_delete_user_404(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="DELETE",
        url=f"{ADMIN_URL}/users/missing",
        status_code=404,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.delete_user("missing")
    assert exc.value.status_code == 404
    await kc_client.close()


@pytest.mark.asyncio
async def test_delete_user_other_error_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="DELETE",
        url=f"{ADMIN_URL}/users/u-1",
        status_code=500,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.delete_user("u-1")
    assert exc.value.status_code == 502
    await kc_client.close()


@pytest.mark.asyncio
async def test_reset_password_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="PUT",
        url=f"{ADMIN_URL}/users/u-1/reset-password",
        status_code=204,
    )
    await kc_client.reset_password("u-1", "newpass", temporary=False)
    await kc_client.close()


@pytest.mark.asyncio
async def test_reset_password_404(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="PUT",
        url=f"{ADMIN_URL}/users/missing/reset-password",
        status_code=404,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.reset_password("missing", "newpass")
    assert exc.value.status_code == 404
    await kc_client.close()


@pytest.mark.asyncio
async def test_reset_password_other_error_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="PUT",
        url=f"{ADMIN_URL}/users/u-1/reset-password",
        status_code=500,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.reset_password("u-1", "newpass")
    assert exc.value.status_code == 502
    await kc_client.close()


# ── Realm Roles ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_realm_roles_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/roles",
        json=[{"id": "r1", "name": "admin"}, {"id": "r2", "name": "viewer"}],
    )
    roles = await kc_client.get_realm_roles()
    assert {r["name"] for r in roles} == {"admin", "viewer"}
    await kc_client.close()


@pytest.mark.asyncio
async def test_get_realm_roles_failure_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(method="GET", url=f"{ADMIN_URL}/roles", status_code=500)
    with pytest.raises(HTTPException) as exc:
        await kc_client.get_realm_roles()
    assert exc.value.status_code == 502
    await kc_client.close()


@pytest.mark.asyncio
async def test_get_user_realm_roles_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/u-1/role-mappings/realm",
        json=[{"id": "r1", "name": "admin"}],
    )
    roles = await kc_client.get_user_realm_roles("u-1")
    assert roles[0]["name"] == "admin"
    await kc_client.close()


@pytest.mark.asyncio
async def test_get_user_realm_roles_404(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/missing/role-mappings/realm",
        status_code=404,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.get_user_realm_roles("missing")
    assert exc.value.status_code == 404
    await kc_client.close()


@pytest.mark.asyncio
async def test_get_user_realm_roles_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/users/u-1/role-mappings/realm",
        status_code=500,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.get_user_realm_roles("u-1")
    assert exc.value.status_code == 502
    await kc_client.close()


@pytest.mark.asyncio
async def test_assign_realm_role_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/roles",
        json=[{"id": "r1", "name": "admin"}],
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{ADMIN_URL}/users/u-1/role-mappings/realm",
        status_code=204,
    )
    await kc_client.assign_realm_role("u-1", "admin")
    await kc_client.close()


@pytest.mark.asyncio
async def test_assign_realm_role_unknown_role_404(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/roles",
        json=[{"id": "r1", "name": "admin"}],
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.assign_realm_role("u-1", "no-such-role")
    assert exc.value.status_code == 404
    await kc_client.close()


@pytest.mark.asyncio
async def test_assign_realm_role_post_failure_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/roles",
        json=[{"id": "r1", "name": "admin"}],
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{ADMIN_URL}/users/u-1/role-mappings/realm",
        status_code=500,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.assign_realm_role("u-1", "admin")
    assert exc.value.status_code == 502
    await kc_client.close()


@pytest.mark.asyncio
async def test_remove_realm_role_success(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/roles",
        json=[{"id": "r1", "name": "admin"}],
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{ADMIN_URL}/users/u-1/role-mappings/realm",
        status_code=204,
    )
    await kc_client.remove_realm_role("u-1", "admin")
    await kc_client.close()


@pytest.mark.asyncio
async def test_remove_realm_role_unknown_role_404(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/roles",
        json=[{"id": "r1", "name": "admin"}],
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.remove_realm_role("u-1", "missing")
    assert exc.value.status_code == 404
    await kc_client.close()


@pytest.mark.asyncio
async def test_remove_realm_role_delete_failure_502(kc_client, httpx_mock: HTTPXMock):
    _mock_token(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=f"{ADMIN_URL}/roles",
        json=[{"id": "r1", "name": "admin"}],
    )
    httpx_mock.add_response(
        method="DELETE",
        url=f"{ADMIN_URL}/users/u-1/role-mappings/realm",
        status_code=500,
    )
    with pytest.raises(HTTPException) as exc:
        await kc_client.remove_realm_role("u-1", "admin")
    assert exc.value.status_code == 502
    await kc_client.close()
