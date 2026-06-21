"""Tests for the monitored-server registry router (/admin/servers)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AlertState
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def _no_prometheus_or_filesd():
    """Isolate the router from Prometheus and the file_sd writer."""
    with patch("app.routers.servers.server_monitor.sync_targets_from_db", new=AsyncMock()), \
         patch("app.routers.servers.server_monitor.host_up_map", new=AsyncMock(return_value={})):
        yield


@pytest.mark.asyncio
async def test_create_list_update_delete(client, admin_token):
    h = auth_header(admin_token)

    # create
    resp = await client.post("/admin/servers", headers=h, json={
        "name": "web1", "address": "10.0.0.5:9100", "description": "edge",
        "disk_warn_pct": 70, "disk_mountpoints": " /, /data, /data ",
    })
    assert resp.status_code == 201, resp.text
    host_id = resp.json()["id"]
    assert resp.json()["disk_warn_pct"] == 70
    assert resp.json()["disk_mountpoints"] == "/,/data"

    # list (status comes from the mocked up_map → unknown)
    resp = await client.get("/admin/servers", headers=h)
    assert resp.status_code == 200
    assert [r["name"] for r in resp.json()] == ["web1"]

    # update threshold + disable
    resp = await client.put(
        f"/admin/servers/{host_id}",
        headers=h,
        json={"cpu_warn_pct": 85, "enabled": False, "disk_mountpoints": None},
    )
    assert resp.status_code == 200
    assert resp.json()["cpu_warn_pct"] == 85 and resp.json()["enabled"] is False
    assert resp.json()["disk_mountpoints"] is None

    # delete
    resp = await client.delete(f"/admin/servers/{host_id}", headers=h)
    assert resp.status_code == 204

    resp = await client.get("/admin/servers", headers=h)
    assert resp.json() == []


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(client, admin_token):
    h = auth_header(admin_token)
    body = {"name": "dup", "address": "1.2.3.4:9100"}
    assert (await client.post("/admin/servers", headers=h, json=body)).status_code == 201
    resp = await client.post("/admin/servers", headers=h, json=body)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_invalid_address_and_name_rejected(client, admin_token):
    h = auth_header(admin_token)
    assert (await client.post("/admin/servers", headers=h, json={"name": "x", "address": "no-port"})).status_code == 422
    assert (await client.post("/admin/servers", headers=h, json={"name": "bad name", "address": "1.2.3.4:9100"})).status_code == 422
    assert (await client.post("/admin/servers", headers=h, json={"name": "y", "address": "1.2.3.4:99999"})).status_code == 422


@pytest.mark.asyncio
async def test_invalid_disk_mountpoints_rejected(client, admin_token):
    h = auth_header(admin_token)
    for value in ("data", "/data/../secret", r"C:\\data"):
        resp = await client.post(
            "/admin/servers",
            headers=h,
            json={"name": f"bad-{len(value)}", "address": "1.2.3.4:9100", "disk_mountpoints": value},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_invalid_disk_threshold_order_rejected(client, admin_token):
    h = auth_header(admin_token)
    resp = await client.post(
        "/admin/servers",
        headers=h,
        json={"name": "bad-disk", "address": "1.2.3.4:9100", "disk_warn_pct": 95, "disk_crit_pct": 90},
    )
    assert resp.status_code == 422

    # A single override is also validated against the global default critical threshold.
    resp = await client.post(
        "/admin/servers",
        headers=h,
        json={"name": "bad-effective", "address": "1.2.3.5:9100", "disk_warn_pct": 95},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_requires_permission(client, user_token):
    """The seeded 'user' role lacks servers.read/write."""
    h = auth_header(user_token)
    assert (await client.get("/admin/servers", headers=h)).status_code == 403
    assert (await client.post("/admin/servers", headers=h, json={"name": "z", "address": "1.2.3.4:9100"})).status_code == 403


@pytest.mark.asyncio
async def test_test_endpoint_reports_status(client, admin_token):
    h = auth_header(admin_token)
    created = await client.post("/admin/servers", headers=h, json={"name": "web9", "address": "1.2.3.4:9100"})
    host_id = created.json()["id"]
    with patch("app.routers.servers.server_monitor.host_up_map", new=AsyncMock(return_value={"web9": True})):
        resp = await client.post(f"/admin/servers/{host_id}/test", headers=h)
    assert resp.status_code == 200 and resp.json()["status"] == "up"


@pytest.mark.asyncio
async def test_disabling_server_persists_alert_state_cleanup(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    created = await client.post(
        "/admin/servers",
        headers=h,
        json={"name": "web-state", "address": "1.2.3.4:9100"},
    )
    assert created.status_code == 201, created.text
    host_id = created.json()["id"]

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertState(alert_type="server_down", target="web-state", status="alert"))
        await db.commit()

    resp = await client.put(f"/admin/servers/{host_id}", headers=h, json={"enabled": False})
    assert resp.status_code == 200, resp.text

    async with session_factory() as db:
        row = (
            await db.execute(
                select(AlertState).where(
                    AlertState.alert_type == "server_down",
                    AlertState.target == "web-state",
                )
            )
        ).scalar_one_or_none()
    assert row is None
