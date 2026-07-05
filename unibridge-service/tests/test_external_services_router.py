"""Tests for the external-service registry router (/admin/servers/external-services)."""
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
    with patch("app.routers.servers.server_monitor.sync_service_targets_from_db", new=AsyncMock()), \
         patch("app.routers.servers.server_monitor.service_up_map", new=AsyncMock(return_value={})):
        yield


@pytest.mark.asyncio
async def test_create_list_update_delete(client, admin_token):
    h = auth_header(admin_token)

    # create (metrics_path defaults to /metrics)
    resp = await client.post("/admin/servers/external-services", headers=h, json={
        "name": "orders-api", "address": "10.0.0.7:8080", "description": "orders",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    service_id = body["id"]
    assert body["metrics_path"] == "/metrics"
    assert body["status"] == "unknown"  # up_map mocked empty
    assert body["enabled"] is True

    # list
    resp = await client.get("/admin/servers/external-services", headers=h)
    assert resp.status_code == 200
    assert [r["name"] for r in resp.json()] == ["orders-api"]

    # update path + disable
    resp = await client.put(
        f"/admin/servers/external-services/{service_id}",
        headers=h,
        json={"metrics_path": "/actuator/prometheus", "enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["metrics_path"] == "/actuator/prometheus"
    assert resp.json()["enabled"] is False

    # delete
    resp = await client.delete(f"/admin/servers/external-services/{service_id}", headers=h)
    assert resp.status_code == 204

    resp = await client.get("/admin/servers/external-services", headers=h)
    assert resp.json() == []


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(client, admin_token):
    h = auth_header(admin_token)
    body = {"name": "dup-svc", "address": "1.2.3.4:9000"}
    assert (await client.post("/admin/servers/external-services", headers=h, json=body)).status_code == 201
    resp = await client.post("/admin/servers/external-services", headers=h, json=body)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_invalid_address_name_and_metrics_path_rejected(client, admin_token):
    h = auth_header(admin_token)
    # bad address (no port)
    assert (await client.post("/admin/servers/external-services", headers=h,
            json={"name": "x", "address": "no-port"})).status_code == 422
    # bad name (space)
    assert (await client.post("/admin/servers/external-services", headers=h,
            json={"name": "bad name", "address": "1.2.3.4:9000"})).status_code == 422
    # bad port
    assert (await client.post("/admin/servers/external-services", headers=h,
            json={"name": "y", "address": "1.2.3.4:99999"})).status_code == 422
    # bad metrics_path (no leading slash)
    assert (await client.post("/admin/servers/external-services", headers=h,
            json={"name": "z", "address": "1.2.3.4:9000", "metrics_path": "metrics"})).status_code == 422


@pytest.mark.asyncio
async def test_requires_permission(client, user_token):
    """The seeded 'user' role lacks servers.read/write."""
    h = auth_header(user_token)
    assert (await client.get("/admin/servers/external-services", headers=h)).status_code == 403
    assert (await client.post("/admin/servers/external-services", headers=h,
            json={"name": "z", "address": "1.2.3.4:9000"})).status_code == 403


@pytest.mark.asyncio
async def test_create_writes_file_sd_targets(client, admin_token):
    h = auth_header(admin_token)
    sync_mock = AsyncMock()
    with patch("app.routers.servers.server_monitor.sync_service_targets_from_db", sync_mock):
        resp = await client.post("/admin/servers/external-services", headers=h,
                                 json={"name": "billing", "address": "1.2.3.4:9000"})
    assert resp.status_code == 201
    sync_mock.assert_awaited()  # registry mutation rewrote the file_sd targets


@pytest.mark.asyncio
async def test_status_up_down_unknown_mapping(client, admin_token):
    h = auth_header(admin_token)
    await client.post("/admin/servers/external-services", headers=h, json={"name": "up-svc", "address": "1.1.1.1:9000"})
    await client.post("/admin/servers/external-services", headers=h, json={"name": "down-svc", "address": "1.1.1.2:9000"})
    await client.post("/admin/servers/external-services", headers=h, json={"name": "ghost-svc", "address": "1.1.1.3:9000"})

    # up-svc scraped up, down-svc scraped down, ghost-svc absent from scrape data.
    with patch("app.routers.servers.server_monitor.service_up_map",
               new=AsyncMock(return_value={"up-svc": True, "down-svc": False})):
        resp = await client.get("/admin/servers/external-services", headers=h)
    statuses = {r["name"]: r["status"] for r in resp.json()}
    assert statuses == {"up-svc": "up", "down-svc": "down", "ghost-svc": "unknown"}

    # Prometheus unreachable → all unknown.
    with patch("app.routers.servers.server_monitor.service_up_map", new=AsyncMock(return_value=None)):
        resp = await client.get("/admin/servers/external-services", headers=h)
    assert {r["status"] for r in resp.json()} == {"unknown"}


@pytest.mark.asyncio
async def test_disable_clears_alert_state(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    created = await client.post("/admin/servers/external-services", headers=h,
                                json={"name": "svc-state", "address": "1.2.3.4:9000"})
    service_id = created.json()["id"]

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertState(alert_type="external_service_down", target="svc-state", status="alert"))
        await db.commit()

    resp = await client.put(f"/admin/servers/external-services/{service_id}", headers=h, json={"enabled": False})
    assert resp.status_code == 200, resp.text

    async with session_factory() as db:
        row = (
            await db.execute(
                select(AlertState).where(
                    AlertState.alert_type == "external_service_down",
                    AlertState.target == "svc-state",
                )
            )
        ).scalar_one_or_none()
    assert row is None
