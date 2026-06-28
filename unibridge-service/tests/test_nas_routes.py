"""Tests for NAS browse endpoints + authorization (app.routers.nas).

Covers the dual-path `_require_nas_browse` gate (API-key allowed_databases vs
JWT nas.browse), the 403/404/400/413/503 error mappings, and the list/stat
JSON response-shape contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.nas_security import (
    NasSecurityError,
    NasTooLargeError,
    NasUnavailableError,
)
from tests.conftest import auth_header


async def _create_apikey(client, admin_token, *, name, key, allowed_databases, allowed_routes):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": name,
            "plugins": {"key-auth": {"key": key}},
        })
        mock_apisix.patch_resource = mock_apisix.put_resource
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": name,
                "api_key": key,
                "allowed_databases": allowed_databases,
                "allowed_routes": allowed_routes,
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text


# A sample list response that follows the FROZEN shape from contract §6.
_LIST_PAYLOAD = {
    "path": "a",
    "folders": [
        {
            "name": "sub",
            "path": "a/sub",
            "is_dir": True,
            "size": None,
            "modified_time": "2026-06-01T12:00:00+00:00",
        }
    ],
    "files": [
        {
            "name": "f.csv",
            "path": "a/f.csv",
            "is_dir": False,
            "size": 1234,
            "modified_time": "2026-06-01T12:00:00+00:00",
        }
    ],
    "total_count": 2,
    "has_more": False,
    "next_cursor": None,
    "truncated": False,
}


# ── _require_nas_browse: API-key path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_browse_apikey_rejects_unallowed_alias(client, admin_token):
    await _create_apikey(
        client,
        admin_token,
        name="nas-app",
        key="nas-key",
        allowed_databases=["allowed-nas"],
        allowed_routes=["nas-api"],
    )

    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(return_value=_LIST_PAYLOAD)
        resp = await client.get(
            "/nas/forbidden-nas/entries",
            headers={"X-Consumer-Username": "nas-app"},
        )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()
    mgr.list_entries.assert_not_awaited()


@pytest.mark.asyncio
async def test_browse_apikey_allows_configured_alias(client, admin_token):
    await _create_apikey(
        client,
        admin_token,
        name="nas-allowed-app",
        key="nas-key-allowed",
        allowed_databases=["allowed-nas"],
        allowed_routes=["nas-api"],
    )

    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(return_value=_LIST_PAYLOAD)
        resp = await client.get(
            "/nas/allowed-nas/entries?path=a",
            headers={"X-Consumer-Username": "nas-allowed-app"},
        )
    assert resp.status_code == 200
    assert resp.json() == _LIST_PAYLOAD
    mgr.list_entries.assert_awaited_once()


@pytest.mark.asyncio
async def test_browse_apikey_wildcard_allows_any_alias(client, admin_token):
    await _create_apikey(
        client,
        admin_token,
        name="nas-wild",
        key="nas-wild-key",
        allowed_databases=["*"],
        allowed_routes=["nas-api"],
    )

    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(return_value=_LIST_PAYLOAD)
        resp = await client.get(
            "/nas/anything/entries",
            headers={"X-Consumer-Username": "nas-wild"},
        )
    assert resp.status_code == 200


# ── _require_nas_browse: JWT path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browse_jwt_lacking_permission_403(client, user_token):
    """The seeded ``user`` role does NOT have nas.browse (admin-only)."""
    resp = await client.get(
        "/nas/some/entries",
        headers=auth_header(user_token),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_browse_admin_has_permission_but_alias_missing(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.get(
            "/nas/missing/entries",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


# ── /entries: success + response-shape contract ───────────────────────────────


@pytest.mark.asyncio
async def test_list_entries_success_shape(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(return_value=_LIST_PAYLOAD)
        resp = await client.get(
            "/nas/x/entries?path=a&offset=0&limit=10",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "path",
        "folders",
        "files",
        "total_count",
        "has_more",
        "next_cursor",
        "truncated",
    }
    entry = body["files"][0]
    assert set(entry.keys()) == {"name", "path", "is_dir", "size", "modified_time"}
    # No absolute path / mode / nlink leakage anywhere in the payload.
    assert "mode" not in entry
    assert "nlink" not in entry


@pytest.mark.asyncio
async def test_list_entries_default_path_empty(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(return_value=_LIST_PAYLOAD)
        resp = await client.get(
            "/nas/x/entries",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    # path defaults to "" and offset to 0.
    call = mgr.list_entries.await_args
    assert call.args[0] == "x"


@pytest.mark.asyncio
async def test_list_entries_forwards_query(client, admin_token):
    """The `q` search term is forwarded to the manager as the `query` kwarg."""
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(return_value=_LIST_PAYLOAD)
        resp = await client.get(
            "/nas/x/entries?path=a&q=report",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    call = mgr.list_entries.await_args
    assert call.kwargs["query"] == "report"


@pytest.mark.asyncio
async def test_list_entries_invalid_limit_422(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        resp = await client.get(
            "/nas/x/entries?limit=0",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422


# ── error mappings (§7) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_entries_security_error_400(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(side_effect=NasSecurityError("bad path"))
        resp = await client.get(
            "/nas/x/entries?path=../etc",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400
    # The error detail must NOT leak an absolute path.
    assert "/mnt" not in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_entries_unavailable_503(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(side_effect=NasUnavailableError("mount gone"))
        resp = await client.get(
            "/nas/x/entries",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_list_entries_not_a_directory_404(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(side_effect=NotADirectoryError("not a dir"))
        resp = await client.get(
            "/nas/x/entries?path=file.txt",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_entries_permission_error_403(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.list_entries = AsyncMock(side_effect=PermissionError("denied"))
        resp = await client.get(
            "/nas/x/entries?path=locked",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 403


# ── /metadata ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metadata_success_shape(client, admin_token):
    meta = {
        "name": "f.csv",
        "path": "a/f.csv",
        "is_dir": False,
        "size": 1234,
        "modified_time": "2026-06-01T12:00:00+00:00",
        "content_type": "text/csv",
    }
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.stat_path = AsyncMock(return_value=meta)
        resp = await client.get(
            "/nas/x/metadata?path=a/f.csv",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "name",
        "path",
        "is_dir",
        "size",
        "modified_time",
        "content_type",
    }


@pytest.mark.asyncio
async def test_metadata_requires_path_query(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        resp = await client.get(
            "/nas/x/metadata",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_metadata_hidden_target_404(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.stat_path = AsyncMock(side_effect=FileNotFoundError("hidden"))
        resp = await client.get(
            "/nas/x/metadata?path=.secret",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metadata_alias_missing_404(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.get(
            "/nas/x/metadata?path=a",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metadata_is_a_directory_400(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.stat_path = AsyncMock(side_effect=IsADirectoryError("dir"))
        resp = await client.get(
            "/nas/x/metadata?path=somedir",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400


# ── /download ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_streams_body(client, admin_token):
    chunks = [b"hello ", b"world"]

    async def _gen():
        for c in chunks:
            yield c

    meta = {"size": 11, "content_type": "text/plain", "filename": "file.txt"}
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.open_read_stream = AsyncMock(return_value=(_gen(), meta))
        resp = await client.get(
            "/nas/x/download?path=a/file.txt",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert resp.headers["content-type"].startswith("text/plain")
    assert "file.txt" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_too_large_413(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.open_read_stream = AsyncMock(side_effect=NasTooLargeError("too big"))
        resp = await client.get(
            "/nas/x/download?path=big.bin",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_download_hidden_or_missing_404(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.open_read_stream = AsyncMock(side_effect=FileNotFoundError("missing"))
        resp = await client.get(
            "/nas/x/download?path=.secret",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_special_file_400(client, admin_token):
    """A FIFO/socket/device surfaces as NasSecurityError → 400 (no path leak)."""
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.open_read_stream = AsyncMock(
            side_effect=NasSecurityError("unsupported file type")
        )
        resp = await client.get(
            "/nas/x/download?path=pipe",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400
    assert "/mnt" not in resp.json()["detail"]


@pytest.mark.asyncio
async def test_download_unavailable_503(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.open_read_stream = AsyncMock(side_effect=NasUnavailableError("mount gone"))
        resp = await client.get(
            "/nas/x/download?path=a.txt",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_download_alias_missing_404(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.get(
            "/nas/x/download?path=a.txt",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_requires_path_query(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        resp = await client.get(
            "/nas/x/download",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422


# ── no write/upload/delete/rename endpoints exist ─────────────────────────────


@pytest.mark.asyncio
async def test_no_write_endpoints(client, admin_token):
    """NAS is strictly read-only: mutating verbs on browse paths must not be
    routed (405/404), never 2xx."""
    for method in ("post", "put", "delete", "patch"):
        resp = await getattr(client, method)(
            "/nas/x/entries",
            headers=auth_header(admin_token),
        )
        assert resp.status_code in (404, 405), f"{method} /nas/x/entries -> {resp.status_code}"
