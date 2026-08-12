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
         patch("app.routers.servers.server_monitor.gpu_up_map", new=AsyncMock(return_value={})), \
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
async def test_metrics_returns_disk_series_per_mountpoint(client, admin_token):
    h = auth_header(admin_token)
    created = await client.post(
        "/admin/servers",
        headers=h,
        json={"name": "web-metrics", "address": "1.2.3.4:9100", "disk_mountpoints": "/,/data"},
    )
    assert created.status_code == 201, created.text
    host_id = created.json()["id"]

    async def fake_range_query(query, **_kwargs):
        if "node_cpu_seconds_total" in query:
            return [{"metric": {"host": "web-metrics"}, "values": [[1, "10"]]}]
        if "node_memory_MemAvailable_bytes" in query:
            return [{"metric": {"host": "web-metrics"}, "values": [[1, "20"]]}]
        if "node_filesystem_avail_bytes" in query:
            assert "max by (host, mountpoint)" in query
            return [
                {"metric": {"host": "web-metrics", "mountpoint": "/"}, "values": [[1, "55"], [2, "56"]]},
                {"metric": {"host": "web-metrics", "mountpoint": "/data"}, "values": [[1, "75"], [2, "NaN"]]},
            ]
        return []

    async def fake_instant_query(query, **_kwargs):
        if "node_filesystem_size_bytes" in query:
            assert "max by (host, mountpoint)" in query
            return [
                {"metric": {"host": "web-metrics", "mountpoint": "/"}, "value": [2, "1073741824"]},
                {"metric": {"host": "web-metrics", "mountpoint": "/data"}, "value": [2, "2147483648"]},
            ]
        return []

    with patch("app.routers.servers.prometheus_client.range_query", new=fake_range_query), \
         patch("app.routers.servers.prometheus_client.instant_query", new=fake_instant_query):
        resp = await client.get(f"/admin/servers/{host_id}/metrics", headers=h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [(s["metric"], s["mountpoint"]) for s in body] == [
        ("cpu", None),
        ("mem", None),
        ("disk", "/"),
        ("disk", "/data"),
    ]
    assert body[2]["points"] == [
        {
            "t": 1.0,
            "v": 55.0,
            "total_bytes": 1073741824.0,
            "used_bytes": 590558003.0,
            "available_bytes": 483183821.0,
        },
        {
            "t": 2.0,
            "v": 56.0,
            "total_bytes": 1073741824.0,
            "used_bytes": 601295421.0,
            "available_bytes": 472446403.0,
        },
    ]
    assert body[3]["points"] == [
        {
            "t": 1.0,
            "v": 75.0,
            "total_bytes": 2147483648.0,
            "used_bytes": 1610612736.0,
            "available_bytes": 536870912.0,
        },
        {"t": 2.0, "v": None, "total_bytes": 2147483648.0},
    ]


@pytest.mark.asyncio
async def test_gpu_fields_roundtrip_and_clear(client, admin_token):
    h = auth_header(admin_token)
    created = await client.post("/admin/servers", headers=h, json={
        "name": "gpu1", "address": "1.2.3.4:9100", "gpu_address": "1.2.3.4:9400",
        "gpu_util_warn_pct": 85, "gpu_mem_warn_pct": 0,
    })
    assert created.status_code == 201, created.text
    host_id = created.json()["id"]
    assert created.json()["gpu_address"] == "1.2.3.4:9400"
    assert created.json()["gpu_util_warn_pct"] == 85
    assert created.json()["gpu_mem_warn_pct"] == 0

    resp = await client.put(
        f"/admin/servers/{host_id}",
        headers=h,
        json={"gpu_address": "1.2.3.4:9401", "gpu_mem_warn_pct": 70},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gpu_address"] == "1.2.3.4:9401"
    assert resp.json()["gpu_mem_warn_pct"] == 70

    # Blank input is how the UI turns GPU monitoring off.
    resp = await client.put(f"/admin/servers/{host_id}", headers=h, json={"gpu_address": "   "})
    assert resp.status_code == 200 and resp.json()["gpu_address"] is None

    assert (await client.post("/admin/servers", headers=h, json={
        "name": "gpu-bad", "address": "1.2.3.9:9100", "gpu_address": "no-port",
    })).status_code == 422


@pytest.mark.asyncio
async def test_clearing_gpu_address_clears_only_gpu_alert_state(client, admin_token, seeded_db):
    h = auth_header(admin_token)
    created = await client.post("/admin/servers", headers=h, json={
        "name": "gpu-state", "address": "1.2.3.4:9100", "gpu_address": "1.2.3.4:9400",
    })
    assert created.status_code == 201, created.text
    host_id = created.json()["id"]

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertState(alert_type="server_gpu_down", target="gpu-state", status="alert"))
        db.add(AlertState(alert_type="server_gpu_util", target="gpu-state", status="alert"))
        db.add(AlertState(alert_type="server_down", target="gpu-state", status="alert"))
        await db.commit()

    resp = await client.put(f"/admin/servers/{host_id}", headers=h, json={"gpu_address": None})
    assert resp.status_code == 200 and resp.json()["gpu_address"] is None

    async with session_factory() as db:
        rows = (
            await db.execute(select(AlertState).where(AlertState.target == "gpu-state"))
        ).scalars().all()
    # The node-level signal survives: only the GPU target left the scrape set.
    assert [r.alert_type for r in rows] == ["server_down"]


@pytest.mark.asyncio
async def test_metrics_returns_one_gpu_series_per_card(client, admin_token):
    h = auth_header(admin_token)
    created = await client.post("/admin/servers", headers=h, json={
        "name": "gpu-metrics", "address": "1.2.3.4:9100", "gpu_address": "1.2.3.4:9400",
    })
    assert created.status_code == 201, created.text
    host_id = created.json()["id"]

    async def fake_range_query(query, **_kwargs):
        if "DCGM_FI_DEV_GPU_UTIL" in query:
            assert "max by (host, gpu, modelName)" in query
            return [
                {"metric": {"host": "gpu-metrics", "gpu": "1", "modelName": "NVIDIA A100"},
                 "values": [[1, "80"]]},
                {"metric": {"host": "gpu-metrics", "gpu": "0", "modelName": "NVIDIA A100"},
                 "values": [[1, "40"]]},
            ]
        if "DCGM_FI_DEV_FB_USED" in query:
            return [
                {"metric": {"host": "gpu-metrics", "gpu": "0", "modelName": "NVIDIA A100"},
                 "values": [[1, "55"]]},
            ]
        return []

    async def fake_instant_query(_query, **_kwargs):
        return []

    with patch("app.routers.servers.prometheus_client.range_query", new=fake_range_query), \
         patch("app.routers.servers.prometheus_client.instant_query", new=fake_instant_query):
        resp = await client.get(f"/admin/servers/{host_id}/metrics", headers=h)

    assert resp.status_code == 200, resp.text
    gpu_series = [s for s in resp.json() if s["metric"].startswith("gpu_")]
    assert [(s["metric"], s["gpu"], s["gpu_model"]) for s in gpu_series] == [
        ("gpu_util", "0", "NVIDIA A100"),
        ("gpu_util", "1", "NVIDIA A100"),
        ("gpu_mem", "0", "NVIDIA A100"),
    ]
    # No disk-style byte enrichment on GPU points.
    assert gpu_series[0]["points"] == [{"t": 1.0, "v": 40.0}]


@pytest.mark.asyncio
async def test_metrics_skips_gpu_queries_without_gpu_address(client, admin_token):
    h = auth_header(admin_token)
    created = await client.post(
        "/admin/servers", headers=h, json={"name": "no-gpu", "address": "1.2.3.4:9100"}
    )
    host_id = created.json()["id"]
    queries: list[str] = []

    async def fake_range_query(query, **_kwargs):
        queries.append(query)
        return []

    with patch("app.routers.servers.prometheus_client.range_query", new=fake_range_query):
        resp = await client.get(f"/admin/servers/{host_id}/metrics", headers=h)

    assert resp.status_code == 200
    assert not any("DCGM" in q for q in queries)
    assert not any(s["metric"].startswith("gpu_") for s in resp.json())


@pytest.mark.asyncio
async def test_test_endpoint_reports_gpu_status_only_when_configured(client, admin_token):
    h = auth_header(admin_token)
    plain = (await client.post(
        "/admin/servers", headers=h, json={"name": "cpu-only", "address": "1.2.3.4:9100"}
    )).json()
    gpu = (await client.post("/admin/servers", headers=h, json={
        "name": "gpu-node", "address": "1.2.3.5:9100", "gpu_address": "1.2.3.5:9400",
    })).json()

    up_map = {"cpu-only": True, "gpu-node": True}
    with patch("app.routers.servers.server_monitor.host_up_map", new=AsyncMock(return_value=up_map)), \
         patch("app.routers.servers.server_monitor.gpu_up_map", new=AsyncMock(return_value={"gpu-node": False})):
        plain_resp = await client.post(f"/admin/servers/{plain['id']}/test", headers=h)
        gpu_resp = await client.post(f"/admin/servers/{gpu['id']}/test", headers=h)

    assert "gpu_status" not in plain_resp.json()
    assert gpu_resp.json()["status"] == "up"
    assert gpu_resp.json()["gpu_status"] == "down"

    # No dcgm sample yet (or Prometheus unreachable) reads as unknown, not down.
    with patch("app.routers.servers.server_monitor.host_up_map", new=AsyncMock(return_value=up_map)), \
         patch("app.routers.servers.server_monitor.gpu_up_map", new=AsyncMock(return_value=None)):
        unknown = await client.post(f"/admin/servers/{gpu['id']}/test", headers=h)
    assert unknown.json()["gpu_status"] == "unknown"


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
