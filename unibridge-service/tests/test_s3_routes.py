"""Tests for S3 browse authorization."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import S3Connection
from tests.conftest import auth_header


S3_API_KEY = "s3-key-abcdefghijklmnopqrstuvwxyz123456"
S3_ALLOWED_API_KEY = "s3-key-allowed-abcdefghijklmnopqrstuvwxyz"


@pytest.mark.asyncio
async def test_s3_browse_apikey_rejects_unallowed_alias(client, admin_token):
    """API key consumers must not browse S3 aliases outside their allowed list."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "s3-app",
            "plugins": {"key-auth": {"key": S3_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "s3-app",
                "api_key": S3_API_KEY,
                "allowed_databases": ["allowed-s3"],
                "allowed_routes": ["s3-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    with patch("app.routers.s3.s3_manager") as mock_s3_manager:
        mock_s3_manager.has_connection.return_value = True
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
            "plugins": {"key-auth": {"key": S3_ALLOWED_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "s3-allowed-app",
                "api_key": S3_ALLOWED_API_KEY,
                "allowed_databases": ["allowed-s3"],
                "allowed_routes": ["s3-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    with patch("app.routers.s3.s3_manager") as mock_s3_manager:
        mock_s3_manager.has_connection.return_value = True
        mock_s3_manager.list_buckets = AsyncMock(return_value=[{"name": "allowed"}])

        resp = await client.get(
            "/s3/allowed-s3/buckets",
            headers={"X-Consumer-Username": "s3-allowed-app"},
        )

    assert resp.status_code == 200
    assert resp.json() == [{"name": "allowed"}]
    mock_s3_manager.list_buckets.assert_awaited_once_with("allowed-s3")


@pytest.mark.asyncio
async def test_s3_private_endpoint_opt_in_is_persisted_and_reused(
    client,
    admin_token,
    seeded_db,
):
    with patch("app.routers.s3.s3_manager") as mock_s3_manager:
        mock_s3_manager.add_connection = AsyncMock()
        mock_s3_manager.has_connection.return_value = True

        create_resp = await client.post(
            "/admin/s3/connections",
            json={
                "alias": "minio",
                "endpoint_url": "http://10.0.0.5:9000",
                "allow_private_endpoints": True,
                "region": "us-east-1",
                "access_key_id": "access",
                "secret_access_key": "secret",
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201
        assert create_resp.json()["allow_private_endpoints"] is True

        update_resp = await client.put(
            "/admin/s3/connections/minio",
            json={
                "endpoint_url": "http://10.0.0.5:9000",
                "region": "us-west-2",
            },
            headers=auth_header(admin_token),
        )

    assert update_resp.status_code == 200
    assert update_resp.json()["allow_private_endpoints"] is True
    assert update_resp.json()["region"] == "us-west-2"

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession)
    async with session_factory() as session:
        conn = (
            await session.execute(
                select(S3Connection).where(S3Connection.alias == "minio")
            )
        ).scalar_one()

    assert conn.allow_private_endpoints is True
