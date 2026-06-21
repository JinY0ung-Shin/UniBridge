"""Tests for server (host) monitoring: file_sd, evaluation, and state severity."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, patch

from app.services import server_monitor
from app.services.server_monitor import ServerThresholds, build_targets, evaluate_hosts, metric_query
from app.services.alert_state import AlertStateManager


def _host(name="web1", address="10.0.0.1:9100", enabled=True, labels=None, **overrides):
    return SimpleNamespace(
        name=name, address=address, enabled=enabled, labels=labels,
        disk_warn_pct=overrides.get("disk_warn_pct"),
        disk_crit_pct=overrides.get("disk_crit_pct"),
        cpu_warn_pct=overrides.get("cpu_warn_pct"),
        mem_warn_pct=overrides.get("mem_warn_pct"),
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
    assert metric_query("unknown", "web1") is None
    assert '\\"' in metric_query("mem", 'a"b')


def test_disk_queries_have_no_mountpoint_filter_by_default():
    # Empty whitelist → historical behavior: every real filesystem counts.
    with patch.object(server_monitor.settings, "NODE_EXPORTER_DISK_MOUNTPOINTS", ""):
        assert server_monitor._mountpoint_selector() == ""
        assert "mountpoint" not in server_monitor._q_disk_pct()
        assert "mountpoint" not in server_monitor._q_disk_forecast(3600)
        assert "mountpoint" not in metric_query("disk", "web1")


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
        signals = await evaluate_hosts([_host("web1")], th)
    types = {s.alert_type for s in signals}
    assert types == {"server_down"}
    down = signals[0]
    assert down.is_healthy is False and down.severity == "critical"


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
