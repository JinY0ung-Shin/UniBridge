"""Tests for server (host) monitoring: file_sd, evaluation, and state severity."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, patch

from app.services import server_monitor
from app.services.server_monitor import (
    ServerThresholds,
    build_service_targets,
    build_targets,
    evaluate_hosts,
    evaluate_services,
    metric_query,
)
from app.services.alert_state import AlertStateManager


def _host(name="web1", address="10.0.0.1:9100", enabled=True, labels=None, **overrides):
    return SimpleNamespace(
        name=name, address=address, enabled=enabled, labels=labels,
        description=overrides.get("description", ""),
        disk_warn_pct=overrides.get("disk_warn_pct"),
        disk_crit_pct=overrides.get("disk_crit_pct"),
        cpu_warn_pct=overrides.get("cpu_warn_pct"),
        mem_warn_pct=overrides.get("mem_warn_pct"),
        disk_mountpoints=overrides.get("disk_mountpoints"),
    )


def _series(host, value):
    return {"metric": {"host": host}, "value": [0, str(value)]}


# ── file_sd ──────────────────────────────────────────────────────────────────

def test_build_targets_skips_disabled_and_merges_labels():
    hosts = [
        _host("web1", "10.0.0.1:9100", labels='{"env":"prod"}'),
        _host("web2", "10.0.0.2:9100", enabled=False),
    ]
    entries = build_targets(hosts)
    assert len(entries) == 1
    assert entries[0]["targets"] == ["10.0.0.1:9100"]
    assert entries[0]["labels"] == {"host": "web1", "env": "prod"}


def test_build_targets_user_label_cannot_clobber_host():
    entries = build_targets([_host("web1", labels='{"host":"evil"}')])
    assert entries[0]["labels"]["host"] == "web1"


@pytest.mark.asyncio
async def test_write_targets_file_is_world_readable(tmp_path):
    # Prometheus scrapes file_sd as a different UID (nobody); the targets file
    # must be readable by group/other, not the 0600 that mkstemp defaults to.
    import json
    import os
    import stat

    path = tmp_path / "nodes.json"
    await server_monitor.write_targets_file([_host("web1", "10.0.0.1:39100")], str(path))

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & stat.S_IROTH, f"targets file not other-readable: {oct(mode)}"
    assert json.loads(path.read_text())[0]["targets"] == ["10.0.0.1:39100"]


def test_metric_query_escapes_and_covers_metrics():
    assert metric_query("cpu", "web1") is not None
    assert 'host="web1"' in metric_query("disk", "web1")
    assert 'host="web1"' in server_monitor.disk_capacity_query("web1")
    assert "node_filesystem_size_bytes" in server_monitor.disk_capacity_query("web1")
    assert metric_query("unknown", "web1") is None
    assert '\\"' in metric_query("mem", 'a"b')


def test_disk_queries_have_no_mountpoint_filter_by_default():
    # Empty whitelist → historical behavior: every real filesystem counts.
    with patch.object(server_monitor.settings, "NODE_EXPORTER_DISK_MOUNTPOINTS", ""):
        assert server_monitor._mountpoint_selector() == ""
        assert "mountpoint" not in server_monitor._q_disk_pct()
        assert "mountpoint" not in server_monitor._q_disk_forecast(3600)
        assert "mountpoint=~" not in metric_query("disk", "web1")


def test_disk_queries_apply_configured_mountpoint_whitelist():
    with patch.object(
        server_monitor.settings, "NODE_EXPORTER_DISK_MOUNTPOINTS", "/, /data ,/backup",
    ):
        sel = server_monitor._mountpoint_selector()
        assert sel == r',mountpoint=~"^(/|/data|/backup)$"'
        # Filter lands on both numerator and denominator in every disk query.
        for q in (server_monitor._q_disk_pct(), metric_query("disk", "web1")):
            assert q.count(sel) == 2
        assert server_monitor._q_disk_forecast(3600).count(sel) == 1


def test_disk_mountpoint_selector_escapes_regex_metacharacters_for_promql_string():
    sel = server_monitor._mountpoint_selector("/data.prod,/mnt/cache+ssd")

    assert r"/data\\.prod" in sel
    assert r"/mnt/cache\\+ssd" in sel


def test_disk_queries_group_per_host_mountpoint_overrides():
    hosts = [
        _host("web1", disk_mountpoints="/data"),
        _host("web2", disk_mountpoints="/backup"),
        _host("web3"),
    ]
    with patch.object(server_monitor.settings, "NODE_EXPORTER_DISK_MOUNTPOINTS", "/"):
        q = server_monitor._q_disk_pct_for_hosts(hosts)
        forecast = server_monitor._q_disk_forecast_for_hosts(hosts, 3600)

    assert q.count(" or ") == 2
    assert 'host="web1"' in q and r'mountpoint=~"^(/data)$"' in q
    assert 'host="web2"' in q and r'mountpoint=~"^(/backup)$"' in q
    assert 'host="web3"' in q and r'mountpoint=~"^(/)$"' in q
    assert forecast.count(" or ") == 2
    assert 'host="web1"' in forecast and r'mountpoint=~"^(/data)$"' in forecast


def test_disk_queries_escape_grouped_host_regex_values():
    hosts = [
        _host("web.1", disk_mountpoints="/data"),
        _host("web2", disk_mountpoints="/data"),
    ]

    q = server_monitor._q_disk_pct_for_hosts(hosts)

    assert r'host=~"^(web\\.1|web2)$"' in q


def test_metric_query_uses_host_mountpoint_override():
    with patch.object(server_monitor.settings, "NODE_EXPORTER_DISK_MOUNTPOINTS", "/"):
        q = metric_query("disk", "web1", disk_mountpoints="/data")

    assert q is not None
    assert "max by (host, mountpoint)" in q
    assert r'mountpoint=~"^(/data)$"' in q
    assert r'mountpoint=~"^(/)$"' not in q


# ── evaluation ───────────────────────────────────────────────────────────────

def _patch_queries(mapping):
    """Return an async side_effect mapping query substrings to result lists."""
    async def fake(query, *a, **k):
        for needle, results in mapping.items():
            if needle in query:
                return results
        return []
    return fake


@pytest.mark.asyncio
async def test_disk_warning_and_critical_severity():
    hosts = [_host("web1")]
    th = ServerThresholds(disk_warn_pct=80, disk_crit_pct=90, forecast_hours=0)
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({
            "up{": [_series("web1", 1)],
            "node_filesystem_avail_bytes": [_series("web1", 85)],
            "node_cpu_seconds_total": [_series("web1", 10)],
            "node_memory_MemAvailable_bytes": [_series("web1", 10)],
        })
        signals = await evaluate_hosts(hosts, th)
    disk = next(s for s in signals if s.alert_type == "server_disk")
    assert disk.severity == "warning" and disk.is_healthy is False

    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({
            "up{": [_series("web1", 1)],
            "node_filesystem_avail_bytes": [_series("web1", 95)],
            "node_cpu_seconds_total": [_series("web1", 10)],
            "node_memory_MemAvailable_bytes": [_series("web1", 10)],
        })
        signals = await evaluate_hosts(hosts, th)
    disk = next(s for s in signals if s.alert_type == "server_disk")
    assert disk.severity == "critical"


@pytest.mark.asyncio
async def test_down_host_skips_other_signals():
    th = ServerThresholds(forecast_hours=0)
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({"up{": [_series("web1", 0)]})
        signals = await evaluate_hosts([_host("web1", description="Primary web node")], th)
    types = {s.alert_type for s in signals}
    assert types == {"server_down"}
    down = signals[0]
    assert down.is_healthy is False and down.severity == "critical"
    assert down.description == "Primary web node"


@pytest.mark.asyncio
async def test_missing_up_series_is_treated_as_down():
    th = ServerThresholds(forecast_hours=0)
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({"up{": []})  # host not scraped yet
        signals = await evaluate_hosts([_host("web1")], th)
    assert signals[0].alert_type == "server_down" and signals[0].is_healthy is False


@pytest.mark.asyncio
async def test_per_host_threshold_override():
    th = ServerThresholds(disk_warn_pct=80, disk_crit_pct=90, forecast_hours=0)
    host = _host("web1", disk_warn_pct=50)
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({
            "up{": [_series("web1", 1)],
            "node_filesystem_avail_bytes": [_series("web1", 60)],
        })
        signals = await evaluate_hosts([host], th)
    disk = next(s for s in signals if s.alert_type == "server_disk")
    assert disk.is_healthy is False and disk.threshold == 50


@pytest.mark.asyncio
async def test_disk_invalid_threshold_order_still_alerts_at_critical():
    """A bad persisted warn/crit order must not suppress critical disk alerts."""
    th = ServerThresholds(disk_warn_pct=95, disk_crit_pct=90, forecast_hours=0)
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({
            "up{": [_series("web1", 1)],
            "node_filesystem_avail_bytes": [_series("web1", 92)],
        })
        signals = await evaluate_hosts([_host("web1")], th)
    disk = next(s for s in signals if s.alert_type == "server_disk")
    assert disk.severity == "critical"
    assert disk.is_healthy is False
    assert disk.threshold == 90


@pytest.mark.asyncio
async def test_disk_forecast_fires_when_predicted_negative():
    th = ServerThresholds(forecast_hours=24)
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({
            "up{": [_series("web1", 1)],
            "predict_linear": [_series("web1", -100)],
            "node_filesystem_avail_bytes": [_series("web1", 50)],
        })
        signals = await evaluate_hosts([_host("web1")], th)
    forecast = next(s for s in signals if s.alert_type == "server_disk_forecast")
    assert forecast.is_healthy is False and forecast.severity == "warning"


@pytest.mark.asyncio
async def test_prometheus_failure_skips_cycle():
    with patch.object(server_monitor.prometheus_client, "instant_query", new=AsyncMock(side_effect=RuntimeError("down"))):
        signals = await evaluate_hosts([_host("web1")], ServerThresholds())
    assert signals == []


@pytest.mark.asyncio
async def test_no_enabled_hosts_returns_empty():
    assert await evaluate_hosts([_host(enabled=False)], ServerThresholds()) == []


# ── state-machine: severity escalation + repeat ───────────────────────────────

def test_severity_escalation_refires():
    state = AlertStateManager()
    # warning trigger (immediate, threshold=1)
    assert state.update("server_disk", "web1", is_healthy=False, trigger_after_failures=1, severity="warning") == "triggered"
    # still warning → no refire
    assert state.update("server_disk", "web1", is_healthy=False, trigger_after_failures=1, severity="warning") is None
    # escalates to critical → refire
    assert state.update("server_disk", "web1", is_healthy=False, trigger_after_failures=1, severity="critical") == "triggered"
    assert state.get_entry("server_disk", "web1")["severity"] == "critical"
    # de-escalates → recorded but no refire
    assert state.update("server_disk", "web1", is_healthy=False, trigger_after_failures=1, severity="warning") is None
    assert state.get_entry("server_disk", "web1")["severity"] == "warning"


def test_repeat_after_cycles_refires():
    state = AlertStateManager()
    assert state.update("server_cpu", "web1", is_healthy=False, trigger_after_failures=1, repeat_after_cycles=2) == "triggered"
    assert state.update("server_cpu", "web1", is_healthy=False, trigger_after_failures=1, repeat_after_cycles=2) is None
    assert state.update("server_cpu", "web1", is_healthy=False, trigger_after_failures=1, repeat_after_cycles=2) == "triggered"


def test_resolve_clears_severity():
    state = AlertStateManager()
    state.update("server_disk", "web1", is_healthy=False, trigger_after_failures=1, severity="critical")
    assert state.update("server_disk", "web1", is_healthy=True, trigger_after_failures=1) == "resolved"
    assert state.get_entry("server_disk", "web1")["severity"] is None


def test_binary_behaviour_unchanged_without_new_args():
    """Existing alert types pass no severity/repeat → original semantics."""
    state = AlertStateManager()
    assert state.update("db_health", "x", is_healthy=False, trigger_after_failures=2) is None
    assert state.update("db_health", "x", is_healthy=False, trigger_after_failures=2) == "triggered"
    assert state.update("db_health", "x", is_healthy=False, trigger_after_failures=2) is None
    assert state.update("db_health", "x", is_healthy=True, trigger_after_failures=2) == "resolved"


# ── external-service monitoring ───────────────────────────────────────────────

def _service(
    name="orders",
    address="10.0.0.9:8080",
    metrics_path="/metrics",
    scheme="http",
    enabled=True,
    description="",
):
    return SimpleNamespace(
        name=name, address=address, metrics_path=metrics_path, scheme=scheme,
        enabled=enabled, description=description,
    )


def _svc_series(service, value):
    return {"metric": {"service": service}, "value": [0, str(value)]}


def test_build_service_targets_skips_disabled_and_sets_metrics_path():
    services = [
        _service("orders", "10.0.0.9:8080", "/actuator/prometheus", scheme="https"),
        _service("legacy", "10.0.0.8:9000", enabled=False),
    ]
    entries = build_service_targets(services)
    assert len(entries) == 1
    assert entries[0]["targets"] == ["10.0.0.9:8080"]
    assert entries[0]["labels"] == {
        "service": "orders",
        "__metrics_path__": "/actuator/prometheus",
        "__scheme__": "https",
    }


def test_build_service_targets_defaults_scheme_to_http():
    # A service row without a scheme attribute falls back to http.
    entry = build_service_targets([SimpleNamespace(
        name="orders", address="10.0.0.9:8080", metrics_path="/metrics", enabled=True,
    )])[0]
    assert entry["labels"]["__scheme__"] == "http"


@pytest.mark.asyncio
async def test_write_service_targets_file_is_world_readable(tmp_path):
    import json
    import os
    import stat

    path = tmp_path / "services.json"
    await server_monitor.write_service_targets_file([_service("orders", "10.0.0.9:8080")], str(path))

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & stat.S_IROTH, f"targets file not other-readable: {oct(mode)}"
    entry = json.loads(path.read_text())[0]
    assert entry["targets"] == ["10.0.0.9:8080"]
    assert entry["labels"]["service"] == "orders"


@pytest.mark.asyncio
async def test_evaluate_services_up_and_down():
    services = [_service("up-svc"), _service("down-svc", description="Order API")]
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({
            'up{job="external-services"}': [_svc_series("up-svc", 1), _svc_series("down-svc", 0)],
        })
        signals = await evaluate_services(services)
    by_target = {s.target: s for s in signals}
    assert by_target["up-svc"].is_healthy is True and by_target["up-svc"].severity is None
    assert by_target["down-svc"].is_healthy is False and by_target["down-svc"].severity == "critical"
    assert by_target["down-svc"].description == "Order API"
    assert all(s.alert_type == "external_service_down" for s in signals)


@pytest.mark.asyncio
async def test_evaluate_services_missing_series_is_down():
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _patch_queries({'up{job="external-services"}': []})  # not scraped yet
        signals = await evaluate_services([_service("orders")])
    assert signals[0].alert_type == "external_service_down" and signals[0].is_healthy is False


@pytest.mark.asyncio
async def test_evaluate_services_prometheus_failure_skips_cycle():
    with patch.object(
        server_monitor.prometheus_client, "instant_query",
        new=AsyncMock(side_effect=RuntimeError("down")),
    ):
        signals = await evaluate_services([_service("orders")])
    assert signals == []


@pytest.mark.asyncio
async def test_evaluate_services_no_enabled_returns_empty():
    assert await evaluate_services([_service(enabled=False)]) == []


class TestProbeMetricsEndpoint:
    """Direct HTTP probe used by the external-services Test button."""

    @staticmethod
    def _transport(handler):
        import httpx

        return httpx.MockTransport(handler)

    @pytest.mark.asyncio
    async def test_up_with_convention_metrics(self):
        import httpx

        def handler(request):
            return httpx.Response(200, text="http_requests_total{method=\"GET\"} 42\n")

        status, detail = await server_monitor.probe_metrics_endpoint(
            "http://svc:8080/metrics", transport=self._transport(handler)
        )
        assert (status, detail) == ("up", None)

    @pytest.mark.asyncio
    async def test_up_but_convention_missing_gets_guidance(self):
        import httpx

        def handler(request):
            return httpx.Response(200, text="python_gc_objects_collected_total 10\n")

        status, detail = await server_monitor.probe_metrics_endpoint(
            "http://svc:8080/metrics", transport=self._transport(handler)
        )
        assert status == "up"
        assert "http_requests_total" in detail

    @pytest.mark.asyncio
    async def test_non_200_is_down(self):
        import httpx

        def handler(request):
            return httpx.Response(404, text="not found")

        status, detail = await server_monitor.probe_metrics_endpoint(
            "http://svc:8080/metrics", transport=self._transport(handler)
        )
        assert status == "down"
        assert "404" in detail

    @pytest.mark.asyncio
    async def test_connection_error_is_down(self):
        import httpx

        def handler(request):
            raise httpx.ConnectError("connection refused")

        status, detail = await server_monitor.probe_metrics_endpoint(
            "http://svc:8080/metrics", transport=self._transport(handler)
        )
        assert status == "down"
        assert "ConnectError" in detail
