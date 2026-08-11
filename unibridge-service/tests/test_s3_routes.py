"""Tests for S3 browse authorization."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_s3_browse_apikey_rejects_unallowed_alias(client, admin_token):
    """API key consumers must not browse S3 aliases outside their allowed list."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "s3-app",
            "plugins": {"key-auth": {"key": "s3-key"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "s3-app",
                "api_key": "s3-key",
                "allowed_databases": ["allowed-s3"],
                "allowed_routes": ["s3-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    with patch("app.routers.s3.s3_manager") as mock_s3_manager:
        mock_s3_manager.has_connection.return_value = True
        mock_s3_manager.allowed_buckets.return_value = None
        mock_s3_manager.list_buckets = AsyncMock(return_value=[{"name": "private"}])

        resp = await client.get(
            "/s3/forbidden-s3/buckets",
            headers={"X-Consumer-Username": "s3-app"},
        )

    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()
    mock_s3_manager.list_buckets.assert_not_awaited()


@pytest.mark.asyncio
async def test_s3_browse_apikey_allows_configured_alias(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "s3-allowed-app",
            "plugins": {"key-auth": {"key": "s3-key-allowed"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "s3-allowed-app",
                "api_key": "s3-key-allowed",
                "allowed_databases": ["allowed-s3"],
                "allowed_routes": ["s3-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    with patch("app.routers.s3.s3_manager") as mock_s3_manager:
        mock_s3_manager.has_connection.return_value = True
        mock_s3_manager.allowed_buckets.return_value = None
        mock_s3_manager.list_buckets = AsyncMock(return_value=[{"name": "allowed"}])

        resp = await client.get(
            "/s3/allowed-s3/buckets",
            headers={"X-Consumer-Username": "s3-allowed-app"},
        )

    assert resp.status_code == 200
    assert resp.json() == [{"name": "allowed"}]
    mock_s3_manager.list_buckets.assert_awaited_once_with("allowed-s3")


@pytest.mark.asyncio
async def test_s3_browse_apikey_rejects_bucket_outside_connection_allowlist(client, admin_token):
    """The per-connection bucket allow-list also binds API-key consumers."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "s3-bucket-app",
            "plugins": {"key-auth": {"key": "s3-key-bucket"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "s3-bucket-app",
                "api_key": "s3-key-bucket",
                "allowed_databases": ["allowed-s3"],
                "allowed_routes": ["s3-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    with patch("app.routers.s3.s3_manager") as mock_s3_manager:
        mock_s3_manager.has_connection.return_value = True
        mock_s3_manager.allowed_buckets.return_value = ["public"]
        mock_s3_manager.generate_presigned_url = AsyncMock(return_value="https://signed/url")

        resp = await client.get(
            "/s3/allowed-s3/objects/presigned-url?bucket=private&key=k",
            headers={"X-Consumer-Username": "s3-bucket-app"},
        )

    assert resp.status_code == 403
    assert "public" not in resp.text
    mock_s3_manager.generate_presigned_url.assert_not_awaited()
