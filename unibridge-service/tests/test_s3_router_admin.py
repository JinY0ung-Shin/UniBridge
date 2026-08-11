"""Tests for S3 connection admin CRUD and browse endpoints."""
from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError

from tests.conftest import auth_header


def _client_error(code: str, op: str = "ListObjectsV2") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code} message"}},
        op,
    )


# ── Admin CRUD ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_s3_connection_success(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json={
                "alias": "minio-1",
                "endpoint_url": "https://minio.example.com",
                "region": "us-east-1",
                "access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "default_bucket": "my-bucket",
                "use_ssl": True,
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["alias"] == "minio-1"
    assert data["status"] == "registered"
    assert data["access_key_id_masked"].endswith("MPLE")


@pytest.mark.asyncio
async def test_create_s3_connection_allows_private_dns_endpoint(client, admin_token, monkeypatch):
    def fake_getaddrinfo(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 9000))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json={
                "alias": "private-minio",
                "endpoint_url": "https://minio.corp.local:9000",
                "region": "us-east-1",
                "access_key_id": "AKIAIOSFODNN7EXAMPLE",
                "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "use_ssl": True,
            },
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["endpoint_url"] == "https://minio.corp.local:9000"


@pytest.mark.asyncio
async def test_create_s3_connection_short_key_masked(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json={
                "alias": "tiny",
                "region": "us-east-1",
                "access_key_id": "abc",
                "secret_access_key": "shortsecret",
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201
    assert resp.json()["access_key_id_masked"] == "***"


@pytest.mark.asyncio
async def test_create_s3_connection_duplicate(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json={
                "alias": "dup",
                "region": "us-east-1",
                "access_key_id": "AKIA-DUP",
                "secret_access_key": "secret",
            },
            headers=auth_header(admin_token),
        )
        resp2 = await client.post(
            "/admin/s3/connections",
            json={
                "alias": "dup",
                "region": "us-east-1",
                "access_key_id": "AKIA-DUP",
                "secret_access_key": "secret",
            },
            headers=auth_header(admin_token),
        )
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_create_s3_connection_add_failure_returns_error_status(client, admin_token):
    """If s3_manager.add_connection blows up, response status should be 'error'."""
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock(side_effect=Exception("creds bad"))
        mgr.has_connection.return_value = False

        resp = await client.post(
            "/admin/s3/connections",
            json={
                "alias": "broken",
                "region": "us-east-1",
                "access_key_id": "AKIA-BROKEN-KEY",
                "secret_access_key": "secret",
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201
    assert resp.json()["status"] == "error"


@pytest.mark.asyncio
async def test_create_s3_connection_decrypt_failure_masks(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr, \
            patch("app.routers.s3.decrypt_password", side_effect=ValueError("boom")):
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json={
                "alias": "nodecrypt",
                "region": "us-east-1",
                "access_key_id": "AKIA-NODECRYPT",
                "secret_access_key": "secret",
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201
    assert resp.json()["access_key_id_masked"] == "***"


@pytest.mark.asyncio
async def test_list_s3_connections(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.side_effect = lambda alias: alias == "active"

        for alias in ("active", "inactive"):
            await client.post(
                "/admin/s3/connections",
                json={
                    "alias": alias,
                    "region": "us-east-1",
                    "access_key_id": f"AKIA-{alias.upper()}",
                    "secret_access_key": "secret",
                },
                headers=auth_header(admin_token),
            )
        resp = await client.get(
            "/admin/s3/connections",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    items = {c["alias"]: c["status"] for c in resp.json()}
    assert items["active"] == "registered"
    assert items["inactive"] == "disconnected"


@pytest.mark.asyncio
async def test_get_s3_connection_404(client, admin_token):
    resp = await client.get("/admin/s3/connections/missing", headers=auth_header(admin_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_s3_connection_success(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json={
                "alias": "fetch",
                "region": "us-east-1",
                "access_key_id": "AKIA-FETCH-KEY",
                "secret_access_key": "secret",
            },
            headers=auth_header(admin_token),
        )
        resp = await client.get(
            "/admin/s3/connections/fetch",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["alias"] == "fetch"


@pytest.mark.asyncio
async def test_update_s3_connection_partial(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json={
                "alias": "upd",
                "region": "us-east-1",
                "access_key_id": "AKIA-UPD-OLD",
                "secret_access_key": "old-secret",
                "use_ssl": True,
            },
            headers=auth_header(admin_token),
        )

        resp = await client.put(
            "/admin/s3/connections/upd",
            json={
                "endpoint_url": "https://new.example.com",
                "region": "eu-west-1",
                "access_key_id": "AKIA-UPD-NEW",
                "secret_access_key": "new-secret",
                "default_bucket": "newbucket",
                "use_ssl": False,
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["region"] == "eu-west-1"
    assert body["default_bucket"] == "newbucket"
    assert body["use_ssl"] is False
    assert body["endpoint_url"] == "https://new.example.com"


@pytest.mark.asyncio
async def test_update_s3_connection_404(client, admin_token):
    resp = await client.put(
        "/admin/s3/connections/missing",
        json={"region": "us-east-1"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_s3_connection_recreate_failure(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json={
                "alias": "recreate",
                "region": "us-east-1",
                "access_key_id": "AKIA-REC",
                "secret_access_key": "secret",
            },
            headers=auth_header(admin_token),
        )

    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock(side_effect=Exception("boom"))
        mgr.has_connection.return_value = False

        resp = await client.put(
            "/admin/s3/connections/recreate",
            json={"region": "ap-northeast-2"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


@pytest.mark.asyncio
async def test_delete_s3_connection(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.remove_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json={
                "alias": "delme",
                "region": "us-east-1",
                "access_key_id": "AKIA-DEL",
                "secret_access_key": "secret",
            },
            headers=auth_header(admin_token),
        )

        resp = await client.delete(
            "/admin/s3/connections/delme",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 204
    mgr.remove_connection.assert_awaited_once_with("delme")


@pytest.mark.asyncio
async def test_delete_s3_connection_404(client, admin_token):
    resp = await client.delete(
        "/admin/s3/connections/missing",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_s3_connection_not_registered(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.post(
            "/admin/s3/connections/notregistered/test",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_s3_connection_ok_and_error(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.test_connection = AsyncMock(return_value=(True, "Connection successful"))
        ok = await client.post(
            "/admin/s3/connections/x/test",
            headers=auth_header(admin_token),
        )
        assert ok.status_code == 200
        assert ok.json() == {"status": "ok", "message": "Connection successful"}

        mgr.test_connection = AsyncMock(return_value=(False, "boom"))
        bad = await client.post(
            "/admin/s3/connections/x/test",
            headers=auth_header(admin_token),
        )
        assert bad.status_code == 200
        assert bad.json() == {"status": "error", "message": "boom"}


# ── Browse endpoints (JWT path) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_browse_jwt_lacking_permission_403(client, user_token):
    """User role does NOT have s3.browse permission per seeded_db."""
    resp = await client.get(
        "/s3/some/buckets",
        headers=auth_header(user_token),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_browse_admin_has_permission_but_alias_missing(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.get(
            "/s3/missing/buckets",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_buckets_success(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.list_buckets = AsyncMock(return_value=[{"name": "a"}, {"name": "b"}])
        resp = await client.get(
            "/s3/x/buckets",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json() == [{"name": "a"}, {"name": "b"}]


@pytest.mark.asyncio
async def test_list_buckets_client_error_404(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.list_buckets = AsyncMock(side_effect=_client_error("NoSuchBucket"))
        resp = await client.get("/s3/x/buckets", headers=auth_header(admin_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_buckets_client_error_403(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.list_buckets = AsyncMock(side_effect=_client_error("AccessDenied"))
        resp = await client.get("/s3/x/buckets", headers=auth_header(admin_token))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_buckets_client_error_other_502(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.list_buckets = AsyncMock(side_effect=_client_error("InternalError"))
        resp = await client.get("/s3/x/buckets", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_list_buckets_unexpected_error_502(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.list_buckets = AsyncMock(side_effect=RuntimeError("kaboom"))
        resp = await client.get("/s3/x/buckets", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_list_objects_success(client, admin_token):
    payload = {
        "folders": [{"prefix": "logs/"}],
        "objects": [{"key": "x", "size": 1, "last_modified": "now", "storage_class": None}],
        "is_truncated": False,
        "next_continuation_token": None,
        "key_count": 1,
    }
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.list_objects = AsyncMock(return_value=payload)
        resp = await client.get(
            "/s3/x/objects?bucket=b&prefix=p/&max_keys=10",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json() == payload
    mgr.list_objects.assert_awaited_once_with("x", "b", "p/", "/", 10, None)


@pytest.mark.asyncio
async def test_list_objects_alias_404(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.get("/s3/x/objects?bucket=b", headers=auth_header(admin_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_objects_client_error(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.list_objects = AsyncMock(side_effect=_client_error("AccessDenied"))
        resp = await client.get("/s3/x/objects?bucket=b", headers=auth_header(admin_token))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_objects_unexpected_error(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.list_objects = AsyncMock(side_effect=RuntimeError("oops"))
        resp = await client.get("/s3/x/objects?bucket=b", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_get_object_metadata_success(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(return_value={"key": "k", "size": 99})
        resp = await client.get(
            "/s3/x/objects/metadata?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["key"] == "k"


@pytest.mark.asyncio
async def test_get_object_metadata_alias_missing(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.get(
            "/s3/x/objects/metadata?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_object_metadata_nosuchkey(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(side_effect=_client_error("NoSuchKey"))
        resp = await client.get(
            "/s3/x/objects/metadata?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_object_metadata_unexpected(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(side_effect=RuntimeError("nope"))
        resp = await client.get(
            "/s3/x/objects/metadata?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_presigned_url_success(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.generate_presigned_url = AsyncMock(return_value="https://signed/url")
        resp = await client.get(
            "/s3/x/objects/presigned-url?bucket=b&key=k&expires_in=120",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json() == {"url": "https://signed/url", "expires_in": 120}


@pytest.mark.asyncio
async def test_presigned_url_alias_missing(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.get(
            "/s3/x/objects/presigned-url?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_presigned_url_client_error(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.generate_presigned_url = AsyncMock(side_effect=_client_error("AccessDenied"))
        resp = await client.get(
            "/s3/x/objects/presigned-url?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_presigned_url_unexpected(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.generate_presigned_url = AsyncMock(side_effect=RuntimeError("oops"))
        resp = await client.get(
            "/s3/x/objects/presigned-url?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_download_object_too_large(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(return_value={"size": 600 * 1024 * 1024})
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_download_object_unknown_size(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(return_value={"size": None})
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_download_object_metadata_clienterror(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(side_effect=_client_error("NoSuchKey"))
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_object_metadata_unexpected(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(side_effect=RuntimeError("oops"))
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_download_object_alias_missing(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_object_get_clienterror(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(return_value={"size": 100})
        mgr.get_object = AsyncMock(side_effect=_client_error("AccessDenied"))
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_download_object_get_unexpected(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(return_value={"size": 100})
        mgr.get_object = AsyncMock(side_effect=RuntimeError("nope"))
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_download_object_rechecks_get_object_content_length(client, admin_token):
    class FakeBody:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    body = FakeBody()
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(return_value={"size": 100})
        mgr.get_object = AsyncMock(return_value={
            "Body": body,
            "ContentType": "application/octet-stream",
            "ContentLength": 600 * 1024 * 1024,
        })
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 413
    assert body.closed


@pytest.mark.asyncio
async def test_download_object_streams_body(client, admin_token):
    chunks = [b"hello ", b"world", b""]

    class FakeBody:
        def __init__(self):
            self._iter = iter(chunks)
            self.closed = False

        def read(self, n):
            return next(self._iter)

        def close(self):
            self.closed = True

    body = FakeBody()
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = None
        mgr.get_object_metadata = AsyncMock(return_value={"size": 11})
        mgr.get_object = AsyncMock(return_value={
            "Body": body,
            "ContentType": "text/plain",
            "ContentLength": 11,
        })
        resp = await client.get(
            "/s3/x/objects/download?bucket=b&key=path/to/file.txt",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert resp.headers["content-type"].startswith("text/plain")
    assert "file.txt" in resp.headers["content-disposition"]
    assert body.closed


# ── Bucket allow-list ───────────────────────────────────────────────────────

def _restricted_payload(alias: str, **overrides) -> dict:
    payload = {
        "alias": alias,
        "region": "us-east-1",
        "access_key_id": f"AKIA-{alias.upper()}",
        "secret_access_key": "secret",
    }
    payload.update(overrides)
    return payload


async def _stored_allowed_buckets_json(seeded_db, alias: str) -> str | None:
    """Raw column value, to prove the list is persisted as JSON text."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models import S3Connection

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        result = await db.execute(
            select(S3Connection.allowed_buckets).where(S3Connection.alias == alias)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_create_with_allowed_buckets_persists_json(client, admin_token, seeded_db):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json=_restricted_payload(
                "allow-json",
                default_bucket="bucket-a",
                allowed_buckets=["bucket-a", "bucket-b"],
            ),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["allowed_buckets"] == ["bucket-a", "bucket-b"]
    assert await _stored_allowed_buckets_json(seeded_db, "allow-json") == '["bucket-a", "bucket-b"]'


@pytest.mark.asyncio
async def test_create_normalizes_allowed_buckets(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json=_restricted_payload(
                "allow-normalize",
                allowed_buckets=["  bucket-a  ", "", "bucket-b", "bucket-a", "   "],
            ),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["allowed_buckets"] == ["bucket-a", "bucket-b"]


@pytest.mark.asyncio
async def test_create_empty_allowed_buckets_means_unrestricted(client, admin_token, seeded_db):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json=_restricted_payload("allow-empty", allowed_buckets=[]),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["allowed_buckets"] is None
    assert await _stored_allowed_buckets_json(seeded_db, "allow-empty") is None


@pytest.mark.asyncio
async def test_create_rejects_too_many_allowed_buckets(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json=_restricted_payload(
                "allow-toomany",
                allowed_buckets=[f"bucket-{i}" for i in range(101)],
            ),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_overlong_bucket_name(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json=_restricted_payload("allow-longname", allowed_buckets=["b" * 256]),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_default_bucket_outside_allowlist(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/s3/connections",
            json=_restricted_payload(
                "allow-mismatch",
                default_bucket="other-bucket",
                allowed_buckets=["bucket-a"],
            ),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "default_bucket must be one of allowed_buckets"


@pytest.mark.asyncio
async def test_update_sets_and_clears_allowed_buckets(client, admin_token, seeded_db):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json=_restricted_payload("allow-upd"),
            headers=auth_header(admin_token),
        )

        set_resp = await client.put(
            "/admin/s3/connections/allow-upd",
            json={"allowed_buckets": ["bucket-a", "bucket-b"]},
            headers=auth_header(admin_token),
        )
        assert set_resp.status_code == 200, set_resp.text
        assert set_resp.json()["allowed_buckets"] == ["bucket-a", "bucket-b"]
        assert await _stored_allowed_buckets_json(seeded_db, "allow-upd") == '["bucket-a", "bucket-b"]'

        clear_resp = await client.put(
            "/admin/s3/connections/allow-upd",
            json={"allowed_buckets": None},
            headers=auth_header(admin_token),
        )
    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json()["allowed_buckets"] is None
    assert await _stored_allowed_buckets_json(seeded_db, "allow-upd") is None


@pytest.mark.asyncio
async def test_update_empty_allowed_buckets_clears_restriction(client, admin_token, seeded_db):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json=_restricted_payload("allow-clear", allowed_buckets=["bucket-a"]),
            headers=auth_header(admin_token),
        )
        resp = await client.put(
            "/admin/s3/connections/allow-clear",
            json={"allowed_buckets": []},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["allowed_buckets"] is None
    assert await _stored_allowed_buckets_json(seeded_db, "allow-clear") is None


@pytest.mark.asyncio
async def test_update_allowlist_excluding_stored_default_bucket_422(client, admin_token, seeded_db):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json=_restricted_payload("allow-keepdefault", default_bucket="bucket-a"),
            headers=auth_header(admin_token),
        )
        resp = await client.put(
            "/admin/s3/connections/allow-keepdefault",
            json={"allowed_buckets": ["bucket-b"]},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "default_bucket must be one of allowed_buckets"
    # The rejected request must not have leaked a partial write.
    assert await _stored_allowed_buckets_json(seeded_db, "allow-keepdefault") is None


@pytest.mark.asyncio
async def test_update_default_bucket_outside_stored_allowlist_422(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json=_restricted_payload(
                "allow-keeplist",
                default_bucket="bucket-a",
                allowed_buckets=["bucket-a"],
            ),
            headers=auth_header(admin_token),
        )
        resp = await client.put(
            "/admin/s3/connections/allow-keeplist",
            json={"default_bucket": "bucket-z"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 422

        after = await client.get(
            "/admin/s3/connections/allow-keeplist",
            headers=auth_header(admin_token),
        )
    assert after.json()["default_bucket"] == "bucket-a"


@pytest.mark.asyncio
async def test_update_default_bucket_and_allowlist_together(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/s3/connections",
            json=_restricted_payload(
                "allow-both",
                default_bucket="bucket-a",
                allowed_buckets=["bucket-a"],
            ),
            headers=auth_header(admin_token),
        )
        resp = await client.put(
            "/admin/s3/connections/allow-both",
            json={"default_bucket": "bucket-z", "allowed_buckets": ["bucket-z"]},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_bucket"] == "bucket-z"
    assert body["allowed_buckets"] == ["bucket-z"]


@pytest.mark.asyncio
async def test_list_buckets_restricted_returns_allowlist_only(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = ["bucket-b", "bucket-a"]
        mgr.list_buckets = AsyncMock(return_value=[{"name": "secret-bucket"}])
        resp = await client.get("/s3/x/buckets", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == [
        {"name": "bucket-b", "creation_date": None},
        {"name": "bucket-a", "creation_date": None},
    ]
    mgr.list_buckets.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_objects_bucket_not_allowed_403(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = ["bucket-a"]
        mgr.list_objects = AsyncMock()
        resp = await client.get(
            "/s3/x/objects?bucket=forbidden",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 403
    assert "bucket-a" not in resp.text
    mgr.list_objects.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_objects_allowed_bucket_succeeds(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = ["bucket-a"]
        mgr.list_objects = AsyncMock(return_value={"objects": [], "folders": []})
        resp = await client.get(
            "/s3/x/objects?bucket=bucket-a",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    mgr.list_objects.assert_awaited_once_with("x", "bucket-a", "", "/", 200, None)


@pytest.mark.asyncio
async def test_object_metadata_bucket_not_allowed_403(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = ["bucket-a"]
        mgr.get_object_metadata = AsyncMock()
        resp = await client.get(
            "/s3/x/objects/metadata?bucket=forbidden&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 403
    mgr.get_object_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_presigned_url_bucket_not_allowed_403(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = ["bucket-a"]
        mgr.generate_presigned_url = AsyncMock()
        resp = await client.get(
            "/s3/x/objects/presigned-url?bucket=forbidden&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 403
    mgr.generate_presigned_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_bucket_not_allowed_403(client, admin_token):
    with patch("app.routers.s3.s3_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.allowed_buckets.return_value = ["bucket-a"]
        mgr.get_object_metadata = AsyncMock()
        mgr.get_object = AsyncMock()
        resp = await client.get(
            "/s3/x/objects/download?bucket=forbidden&key=k",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 403
    mgr.get_object_metadata.assert_not_awaited()
    mgr.get_object.assert_not_awaited()
