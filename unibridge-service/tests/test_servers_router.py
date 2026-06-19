"""Tests for the monitored-server registry router (/admin/servers)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

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
        "disk_warn_pct": 70,
    })
    assert resp.status_code == 201, resp.text
    host_id = resp.json()["id"]
    assert resp.json()["disk_warn_pct"] == 70

    # list (status comes from the mocked up_map → unknown)
    resp = await client.get("/admin/servers", headers=h)
    assert resp.status_code == 200
    assert [r["name"] for r in resp.json()] == ["web1"]

    # update threshold + disable
    resp = await client.put(f"/admin/servers/{host_id}", headers=h, json={"cpu_warn_pct": 85, "enabled": False})
    assert resp.status_code == 200
    assert resp.json()["cpu_warn_pct"] == 85 and resp.json()["enabled"] is False

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
