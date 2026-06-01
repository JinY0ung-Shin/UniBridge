"""Tests for NAS connection admin CRUD endpoints (app.routers.nas)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import auth_header

# A base_path that satisfies the schema's allowed-root validator
# (settings.NAS_ALLOWED_ROOTS defaults to "/mnt").
GOOD_BASE = "/mnt/share1"
GOOD_BASE_2 = "/mnt/share2"


# ── Admin CRUD ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_nas_connection_success(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        resp = await client.post(
            "/admin/nas/connections",
            json={
                "alias": "nas-1",
                "base_path": GOOD_BASE,
                "max_download_bytes": 1048576,
                "show_hidden": False,
                "follow_symlinks": False,
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["alias"] == "nas-1"
    assert data["status"] == "registered"
    assert data["base_path"] == GOOD_BASE
    assert data["read_only"] is True
    assert data["show_hidden"] is False
    assert data["follow_symlinks"] is False


@pytest.mark.asyncio
async def test_create_nas_connection_rejects_base_outside_roots(client, admin_token):
    """Schema validator rejects a base_path not under NAS_ALLOWED_ROOTS."""
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True
        resp = await client.post(
            "/admin/nas/connections",
            json={"alias": "bad", "base_path": "/etc"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_nas_connection_rejects_traversal(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True
        resp = await client.post(
            "/admin/nas/connections",
            json={"alias": "bad2", "base_path": "/mnt/../etc/passwd"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_nas_connection_rejects_read_only_false(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True
        resp = await client.post(
            "/admin/nas/connections",
            json={"alias": "rw", "base_path": GOOD_BASE, "read_only": False},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_nas_connection_duplicate(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/nas/connections",
            json={"alias": "dup", "base_path": GOOD_BASE},
            headers=auth_header(admin_token),
        )
        resp2 = await client.post(
            "/admin/nas/connections",
            json={"alias": "dup", "base_path": GOOD_BASE},
            headers=auth_header(admin_token),
        )
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_create_nas_connection_add_failure_returns_error_status(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock(side_effect=Exception("mount gone"))
        mgr.has_connection.return_value = False
        resp = await client.post(
            "/admin/nas/connections",
            json={"alias": "broken", "base_path": GOOD_BASE},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201
    assert resp.json()["status"] == "error"


@pytest.mark.asyncio
async def test_list_nas_connections(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.side_effect = lambda alias: alias == "active"

        for alias, bp in (("active", GOOD_BASE), ("inactive", GOOD_BASE_2)):
            await client.post(
                "/admin/nas/connections",
                json={"alias": alias, "base_path": bp},
                headers=auth_header(admin_token),
            )
        resp = await client.get(
            "/admin/nas/connections",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    items = {c["alias"]: c["status"] for c in resp.json()}
    assert items["active"] == "registered"
    assert items["inactive"] == "disconnected"


@pytest.mark.asyncio
async def test_get_nas_connection_404(client, admin_token):
    resp = await client.get("/admin/nas/connections/missing", headers=auth_header(admin_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_nas_connection_success(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/nas/connections",
            json={"alias": "fetch", "base_path": GOOD_BASE},
            headers=auth_header(admin_token),
        )
        resp = await client.get(
            "/admin/nas/connections/fetch",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["alias"] == "fetch"
    assert body["base_path"] == GOOD_BASE


@pytest.mark.asyncio
async def test_update_nas_connection_partial(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/nas/connections",
            json={
                "alias": "upd",
                "base_path": GOOD_BASE,
                "show_hidden": False,
                "follow_symlinks": False,
            },
            headers=auth_header(admin_token),
        )

        resp = await client.put(
            "/admin/nas/connections/upd",
            json={
                "base_path": GOOD_BASE_2,
                "max_download_bytes": 2048,
                "show_hidden": True,
                "follow_symlinks": True,
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["base_path"] == GOOD_BASE_2
    assert body["max_download_bytes"] == 2048
    assert body["show_hidden"] is True
    assert body["follow_symlinks"] is True


@pytest.mark.asyncio
async def test_update_nas_connection_404(client, admin_token):
    resp = await client.put(
        "/admin/nas/connections/missing",
        json={"show_hidden": True},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_nas_connection_recreate_failure(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.has_connection.return_value = True
        await client.post(
            "/admin/nas/connections",
            json={"alias": "recreate", "base_path": GOOD_BASE},
            headers=auth_header(admin_token),
        )

    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock(side_effect=Exception("boom"))
        mgr.has_connection.return_value = False
        resp = await client.put(
            "/admin/nas/connections/recreate",
            json={"show_hidden": True},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


@pytest.mark.asyncio
async def test_delete_nas_connection(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.add_connection = AsyncMock()
        mgr.remove_connection = AsyncMock()
        mgr.has_connection.return_value = True

        await client.post(
            "/admin/nas/connections",
            json={"alias": "delme", "base_path": GOOD_BASE},
            headers=auth_header(admin_token),
        )
        resp = await client.delete(
            "/admin/nas/connections/delme",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 204
    mgr.remove_connection.assert_awaited_once_with("delme")


@pytest.mark.asyncio
async def test_delete_nas_connection_404(client, admin_token):
    resp = await client.delete(
        "/admin/nas/connections/missing",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_nas_connection_not_registered(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = False
        resp = await client.post(
            "/admin/nas/connections/notregistered/test",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_nas_connection_ok_and_error(client, admin_token):
    with patch("app.routers.nas.nas_manager") as mgr:
        mgr.has_connection.return_value = True
        mgr.test_connection = AsyncMock(return_value=(True, "Connection successful"))
        ok = await client.post(
            "/admin/nas/connections/x/test",
            headers=auth_header(admin_token),
        )
        assert ok.status_code == 200
        assert ok.json() == {"status": "ok", "message": "Connection successful"}

        mgr.test_connection = AsyncMock(return_value=(False, "mount gone"))
        bad = await client.post(
            "/admin/nas/connections/x/test",
            headers=auth_header(admin_token),
        )
        assert bad.status_code == 200
        assert bad.json() == {"status": "error", "message": "mount gone"}


# ── Permission gating ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_crud_requires_permission(client, user_token):
    """The seeded ``user`` role has neither nas.connections.read nor
    nas.connections.write, so every admin endpoint must 403."""
    read_resp = await client.get(
        "/admin/nas/connections",
        headers=auth_header(user_token),
    )
    assert read_resp.status_code == 403

    write_resp = await client.post(
        "/admin/nas/connections",
        json={"alias": "x", "base_path": GOOD_BASE},
        headers=auth_header(user_token),
    )
    assert write_resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_endpoints_require_auth(client):
    resp = await client.get("/admin/nas/connections")
    assert resp.status_code in (401, 403)
