"""Boundary and failure-path coverage for monitored hosts and services."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers import servers
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def _isolate_prometheus_and_file_sd():
    with (
        patch(
            "app.routers.servers.server_monitor.sync_targets_from_db",
            new=AsyncMock(),
        ),
        patch(
            "app.routers.servers.server_monitor.sync_service_targets_from_db",
            new=AsyncMock(),
        ),
        patch(
            "app.routers.servers.server_monitor.host_up_map",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "app.routers.servers.server_monitor.service_up_map",
            new=AsyncMock(return_value={}),
        ),
    ):
        yield


async def _create_host(client, token, *, name="host-a", enabled=True):
    response = await client.post(
        "/admin/servers",
        json={
            "name": name,
            "address": "10.0.0.1:9100",
            "enabled": enabled,
        },
        headers=auth_header(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_service(client, token, *, name="service-a"):
    response = await client.post(
        "/admin/servers/external-services",
        json={"name": name, "address": "10.0.0.2:8080"},
        headers=auth_header(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.parametrize("raw", [None, "not-a-number"])
def test_finite_float_rejects_invalid_values(raw):
    assert servers._finite_float(raw) is None


async def test_disk_capacity_query_failure_returns_empty(monkeypatch, caplog):
    host = SimpleNamespace(name="host-a", disk_mountpoints="/")
    monkeypatch.setattr(
        servers.prometheus_client,
        "instant_query",
        AsyncMock(side_effect=RuntimeError("prometheus offline")),
    )

    assert await servers._disk_capacity_by_mountpoint(host) == {}
    assert "Server disk capacity query failed" in caplog.text


async def test_disk_capacity_skips_invalid_sample(monkeypatch):
    host = SimpleNamespace(name="host-a", disk_mountpoints="/")
    monkeypatch.setattr(
        servers.prometheus_client,
        "instant_query",
        AsyncMock(
            return_value=[
                {"metric": {"mountpoint": "/bad"}, "value": [0, "NaN"]},
                {"metric": {"mountpoint": "/"}, "value": [0, "1024"]},
            ]
        ),
    )

    assert await servers._disk_capacity_by_mountpoint(host) == {"/": 1024.0}


async def test_global_disk_thresholds_uses_configured_values():
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(
        server_disk_warn_pct=72, server_disk_crit_pct=88
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    assert await servers._global_disk_thresholds(db) == (72.0, 88.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("not-json", None),
        ('["not", "an", "object"]', None),
        ('{"region": 7, "enabled": true}', {"region": "7", "enabled": "True"}),
    ],
)
def test_parse_labels_rejects_malformed_values_and_stringifies_entries(raw, expected):
    assert servers._parse_labels(raw) == expected


@pytest.mark.parametrize("kind", ["host", "service"])
async def test_clear_alert_state_discards_memory_and_persistence(monkeypatch, kind):
    state = MagicMock()
    monkeypatch.setattr("app.routers.alerts.get_alert_state", lambda: state)
    delete_state = AsyncMock()
    monkeypatch.setattr(servers, "delete_alert_state", delete_state)

    if kind == "host":
        await servers._clear_host_alert_state(MagicMock(), "host-a")
        expected_count = len(servers.server_monitor.SERVER_ALERT_TYPES)
    else:
        await servers._clear_service_alert_state(MagicMock(), "service-a")
        expected_count = len(servers.server_monitor.EXTERNAL_SERVICE_ALERT_TYPES)

    assert state.discard.call_count == expected_count
    assert delete_state.await_count == expected_count


async def test_list_servers_maps_disabled_unreachable_up_and_down(client, admin_token):
    await _create_host(client, admin_token, name="disabled", enabled=False)
    await _create_host(client, admin_token, name="up")
    await _create_host(client, admin_token, name="down")

    with patch(
        "app.routers.servers.server_monitor.host_up_map",
        new=AsyncMock(return_value={"up": True, "down": False}),
    ):
        response = await client.get(
            "/admin/servers", headers=auth_header(admin_token)
        )
    assert {row["name"]: row["status"] for row in response.json()} == {
        "disabled": "disabled",
        "down": "down",
        "up": "up",
    }

    with patch(
        "app.routers.servers.server_monitor.host_up_map",
        new=AsyncMock(return_value=None),
    ):
        response = await client.get(
            "/admin/servers", headers=auth_header(admin_token)
        )
    assert next(row for row in response.json() if row["name"] == "up")["status"] == "unknown"


async def test_update_server_sets_address_description_and_labels(client, admin_token):
    host_id = await _create_host(client, admin_token)
    response = await client.put(
        f"/admin/servers/{host_id}",
        json={
            "address": "10.0.0.9:9100",
            "description": "Primary host",
            "labels": {"region": "ap-northeast-2"},
        },
        headers=auth_header(admin_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["address"] == "10.0.0.9:9100"
    assert response.json()["description"] == "Primary host"
    assert response.json()["labels"] == {"region": "ap-northeast-2"}


@pytest.mark.parametrize("operation", ["update", "delete", "test", "metrics"])
async def test_unknown_server_returns_404(client, admin_token, operation):
    paths = {
        "update": ("put", "/admin/servers/99999", {"description": "x"}),
        "delete": ("delete", "/admin/servers/99999", None),
        "test": ("post", "/admin/servers/99999/test", None),
        "metrics": ("get", "/admin/servers/99999/metrics", None),
    }
    method, path, body = paths[operation]
    response = await client.request(
        method.upper(),
        path,
        json=body,
        headers=auth_header(admin_token),
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("up_map", "expected_detail"),
    [
        (None, "Prometheus unreachable"),
        ({}, "No scrape data yet for this host"),
    ],
)
async def test_server_probe_reports_unknown_reason(
    client, admin_token, up_map, expected_detail
):
    host_id = await _create_host(client, admin_token)
    with patch(
        "app.routers.servers.server_monitor.host_up_map",
        new=AsyncMock(return_value=up_map),
    ):
        response = await client.post(
            f"/admin/servers/{host_id}/test",
            headers=auth_header(admin_token),
        )

    assert response.json() == {"status": "unknown", "detail": expected_detail}


async def test_server_metrics_tolerates_prometheus_failures_and_empty_disk(
    client, admin_token, caplog
):
    host_id = await _create_host(client, admin_token)
    range_query = AsyncMock(
        side_effect=[RuntimeError("cpu query failed"), [], []]
    )
    with patch("app.routers.servers.prometheus_client.range_query", range_query):
        response = await client.get(
            f"/admin/servers/{host_id}/metrics",
            headers=auth_header(admin_token),
        )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {"metric": "cpu", "mountpoint": None, "points": []},
        {"metric": "mem", "mountpoint": None, "points": []},
        {"metric": "disk", "mountpoint": None, "points": []},
    ]
    assert "Server metric query failed" in caplog.text


async def test_update_service_sets_all_mutable_fields(client, admin_token):
    service_id = await _create_service(client, admin_token)
    response = await client.put(
        f"/admin/servers/external-services/{service_id}",
        json={
            "name": "service-renamed",
            "address": "10.0.0.8:8443",
            "description": "Renamed service",
        },
        headers=auth_header(admin_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "service-renamed"
    assert response.json()["address"] == "10.0.0.8:8443"
    assert response.json()["description"] == "Renamed service"


async def test_update_service_duplicate_name_returns_conflict(client, admin_token):
    first_id = await _create_service(client, admin_token, name="first")
    await _create_service(client, admin_token, name="second")

    response = await client.put(
        f"/admin/servers/external-services/{first_id}",
        json={"name": "second"},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 409


@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_unknown_service_mutation_returns_404(client, admin_token, operation):
    if operation == "update":
        response = await client.put(
            "/admin/servers/external-services/99999",
            json={"description": "x"},
            headers=auth_header(admin_token),
        )
    else:
        response = await client.delete(
            "/admin/servers/external-services/99999",
            headers=auth_header(admin_token),
        )

    assert response.status_code == 404


async def test_external_service_probe_normalizes_legacy_metrics_path(monkeypatch):
    service = SimpleNamespace(
        scheme=None,
        address="10.0.0.7:8080",
        metrics_path="metrics",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=service))
    probe = AsyncMock(return_value=("up", None))
    monkeypatch.setattr(servers.server_monitor, "probe_metrics_endpoint", probe)

    response = await servers.test_external_service(
        1, SimpleNamespace(username="admin"), db
    )

    assert response == {"status": "up", "detail": None}
    probe.assert_awaited_once_with("http://10.0.0.7:8080/metrics")
