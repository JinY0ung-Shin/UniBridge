"""Tests for API Keys CRUD router."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_list_api_keys_empty(client, admin_token):
    resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_api_keys_requires_permission(client, viewer_token):
    resp = await client.get("/admin/api-keys", headers=auth_header(viewer_token))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_api_key(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "test-app",
            "plugins": {"key-auth": {"key": "key-abc123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "test-app",
                "description": "Test application",
                "api_key": "key-abc123",
                "allowed_databases": ["mydb"],
                "allowed_routes": [],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-app"
        assert data["description"] == "Test application"
        assert data["api_key"] == "key-abc123"
        assert data["key_created"] is True
        assert data["allowed_databases"] == ["mydb"]


@pytest.mark.asyncio
async def test_create_api_key_duplicate(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "dup-app",
            "plugins": {"key-auth": {"key": "key-1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))

        await client.post(
            "/admin/api-keys",
            json={"name": "dup-app", "api_key": "key-1"},
            headers=auth_header(admin_token),
        )
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "dup-app", "api_key": "key-2"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_api_key_access(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "update-app",
            "plugins": {"key-auth": {"key": "key-u1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))

        await client.post(
            "/admin/api-keys",
            json={"name": "update-app", "api_key": "key-u1"},
            headers=auth_header(admin_token),
        )

        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "update-app",
            "plugins": {"key-auth": {"key": "key-u1"}},
        })
        resp = await client.put(
            "/admin/api-keys/update-app",
            json={"allowed_databases": ["db1", "db2"], "description": "Updated"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed_databases"] == ["db1", "db2"]
        assert data["description"] == "Updated"


@pytest.mark.asyncio
async def test_delete_api_key(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "del-app",
            "plugins": {"key-auth": {"key": "key-d1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()

        await client.post(
            "/admin/api-keys",
            json={"name": "del-app", "api_key": "key-d1"},
            headers=auth_header(admin_token),
        )
        resp = await client.delete(
            "/admin/api-keys/del-app",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204

        resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))
        assert all(k["name"] != "del-app" for k in resp.json())


@pytest.mark.asyncio
async def test_query_execute_via_apikey_header(client, admin_token):
    """Simulate APISIX-forwarded request with X-Consumer-Username."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "query-app",
            "plugins": {"key-auth": {"key": "qk-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        await client.post(
            "/admin/api-keys",
            json={"name": "query-app", "api_key": "qk-123", "allowed_databases": ["testdb"]},
            headers=auth_header(admin_token),
        )

    # APISIX-forwarded request (no Bearer token, just header)
    resp = await client.post(
        "/query/execute",
        json={"database": "testdb", "sql": "SELECT 1"},
        headers={"X-Consumer-Username": "query-app"},
    )
    # 404 because "testdb" engine doesn't exist in connection_manager, but auth passes
    assert resp.status_code == 404
    assert "not registered" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_query_execute_apikey_db_not_allowed(client, admin_token):
    """API key user cannot query databases not in their allowed list."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "restricted-app",
            "plugins": {"key-auth": {"key": "rk-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        await client.post(
            "/admin/api-keys",
            json={"name": "restricted-app", "api_key": "rk-123", "allowed_databases": ["allowed-db"]},
            headers=auth_header(admin_token),
        )

    resp = await client.post(
        "/query/execute",
        json={"database": "forbidden-db", "sql": "SELECT 1"},
        headers={"X-Consumer-Username": "restricted-app"},
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()
