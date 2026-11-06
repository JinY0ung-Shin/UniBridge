"""Server-monitoring follow-ups (2026-11-06): stale state after a disable,
announced-peak escalation, failed re-announcements, user labels, unmatched
disk mountpoints and the file_sd reconcile."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.schemas import MonitoredHostCreate, MonitoredHostUpdate
from app.services import alert_checker, server_monitor
from app.services.alert_mutes import MuteIndex
from app.services.alert_state import AlertStateManager
from app.services.server_monitor import HostSignal, ServerThresholds, build_targets, evaluate_hosts


def _host(name="web1", address="10.0.0.1:9100", enabled=True, labels=None, **overrides):
    return SimpleNamespace(
        name=name, address=address, enabled=enabled, labels=labels,
        description="",
        disk_warn_pct=overrides.get("disk_warn_pct"),
        disk_crit_pct=overrides.get("disk_crit_pct"),
        cpu_warn_pct=None, mem_warn_pct=None,
        disk_mountpoints=overrides.get("disk_mountpoints"),
        gpu_address=None, gpu_util_warn_pct=None, gpu_mem_warn_pct=None,
    )


def _series(host, value):
    return {"metric": {"host": host}, "value": [0, str(value)]}


def _queries(mapping, failing=()):
    async def fake(query, *a, **k):
        if any(needle in query for needle in failing):
            raise RuntimeError("query timed out")
        for needle, results in mapping.items():
            if needle in query:
                return results
        return []
    return fake


# ── Escalation: announce each severity once per incident ─────────────────────


def test_disk_hovering_at_critical_mails_critical_once_per_incident():
    """89.9% ↔ 90.1% must not re-announce "critical" every other cycle."""
    state = AlertStateManager()
    kw = dict(trigger_after_failures=1)
    assert state.update("server_disk", "web1", is_healthy=False, severity="warning", **kw) == "triggered"
    assert state.update("server_disk", "web1", is_healthy=False, severity="critical", **kw) == "triggered"
    for _ in range(3):
        assert state.update("server_disk", "web1", is_healthy=False, severity="warning", **kw) is None
        assert state.update("server_disk", "web1", is_healthy=False, severity="critical", **kw) is None
    assert state.get_entry("server_disk", "web1")["severity"] == "critical"
    # A new incident announces critical again.
    assert state.update("server_disk", "web1", is_healthy=True, **kw) == "resolved"
    assert state.update("server_disk", "web1", is_healthy=False, severity="critical", **kw) == "triggered"


def test_incident_opened_at_critical_does_not_refire_after_a_dip():
    state = AlertStateManager()
    kw = dict(trigger_after_failures=1)
    assert state.update("server_disk", "web1", is_healthy=False, severity="critical", **kw) == "triggered"
    assert state.update("server_disk", "web1", is_healthy=False, severity="warning", **kw) is None
    assert state.update("server_disk", "web1", is_healthy=False, severity="critical", **kw) is None


def test_peak_severity_survives_a_reload():
    """The announced peak lives in the persisted details, so a failover keeps it."""
    state = AlertStateManager()
    state.update("server_disk", "web1", is_healthy=False, severity="critical", trigger_after_failures=1)
    state.update("server_disk", "web1", is_healthy=False, severity="warning", trigger_after_failures=1)
    entry = state.get_entry("server_disk", "web1")
    reloaded = AlertStateManager()
    reloaded.set_entry(
        "server_disk", "web1", status=entry["status"], since=entry["since"],
        fail_count=entry["fail_count"], severity=entry["severity"],
        details=json.loads(json.dumps(entry["details"])),
    )
    assert reloaded.update(
        "server_disk", "web1", is_healthy=False, severity="critical", trigger_after_failures=1,
    ) is None


# ── Failed re-announcement must not swallow the recovery ─────────────────────


def _disk_signal(healthy: bool, severity: str | None) -> HostSignal:
    return HostSignal(
        alert_type="server_disk", target="web1", display="web1", is_healthy=healthy,
        severity=severity, value=95.0, threshold=80.0, message="disk", monitor_label="disk",
    )


_CYCLE = dict(trigger_after_failures=1, resolve_after_successes=1, repeat=0)


@pytest.mark.asyncio
async def test_failed_reannouncement_keeps_the_recovery(monkeypatch):
    """warning paged → critical escalation fails to send → recovery still goes out."""
    monkeypatch.setattr(alert_checker, "_persist_state_safely", AsyncMock())
    state = AlertStateManager()
    with patch.object(alert_checker, "dispatch_alert", AsyncMock(return_value=True)):
        await alert_checker._process_host_signal(
            state, _disk_signal(False, "warning"), mutes=MuteIndex(), **_CYCLE)
    with patch.object(alert_checker, "dispatch_alert", AsyncMock(return_value=False)):
        await alert_checker._process_host_signal(
            state, _disk_signal(False, "critical"), mutes=MuteIndex(), **_CYCLE)
    assert state.get_pending_notify("server_disk", "web1") is False
    with patch.object(alert_checker, "dispatch_alert", AsyncMock(return_value=True)) as d:
        await alert_checker._process_host_signal(
            state, _disk_signal(True, None), mutes=MuteIndex(), **_CYCLE)
    assert d.await_args.kwargs["alert_type"] == "resolved"


@pytest.mark.asyncio
async def test_failed_first_announcement_is_still_rearmed(monkeypatch):
    monkeypatch.setattr(alert_checker, "_persist_state_safely", AsyncMock())
    state = AlertStateManager()
    with patch.object(alert_checker, "dispatch_alert", AsyncMock(return_value=False)):
        await alert_checker._process_host_signal(
            state, _disk_signal(False, "warning"), mutes=MuteIndex(), **_CYCLE)
    assert state.get_pending_notify("server_disk", "web1") is True
    # The retry on the next failing cycle is a first announcement too: re-armed again.
    with patch.object(alert_checker, "dispatch_alert", AsyncMock(return_value=False)) as d:
        await alert_checker._process_host_signal(
            state, _disk_signal(False, "warning"), mutes=MuteIndex(), **_CYCLE)
    assert d.await_args.kwargs["alert_type"] == "triggered"
    assert state.get_pending_notify("server_disk", "web1") is True


# ── Stale state after a disable/delete race ──────────────────────────────────


@pytest.mark.asyncio
async def test_state_written_back_for_a_disabled_host_is_dropped_next_cycle(monkeypatch):
    """A cycle that loaded the registry before a disable can recreate the state
    the router just cleared; the next cycle drops it again, silently."""
    state = AlertStateManager()
    state.update("server_down", "web1", is_healthy=False, trigger_after_failures=1, severity="critical")
    state.update("server_cpu", "gone", is_healthy=False, trigger_after_failures=1, severity="warning")
    state.update("db_health", "web1", is_healthy=False, trigger_after_failures=1)
    hosts = [_host("web1", enabled=False), _host("web2")]
    monkeypatch.setattr(
        alert_checker, "_load_server_monitoring",
        AsyncMock(return_value=(hosts, ServerThresholds(forecast_hours=0), 0)),
    )
    monkeypatch.setattr(alert_checker.server_monitor, "evaluate_hosts", AsyncMock(return_value=[]))
    monkeypatch.setattr(alert_checker, "_persist_state_safely", AsyncMock())
    monkeypatch.setattr(alert_checker, "delete_alert_state", AsyncMock())
    with patch.object(alert_checker, "dispatch_alert", new_callable=AsyncMock) as dispatch:
        await alert_checker._check_server_health(state, trigger_after_failures=1)
    dispatch.assert_not_awaited()
    assert state.get_entry("server_down", "web1") is None
    assert state.get_entry("server_cpu", "gone") is None
    assert state.get_entry("db_health", "web1") is not None


@pytest.mark.asyncio
async def test_state_of_a_disabled_service_is_dropped(monkeypatch):
    state = AlertStateManager()
    state.update("external_service_down", "orders", is_healthy=False, trigger_after_failures=1)
    state.update("external_service_down", "billing", is_healthy=False, trigger_after_failures=1)
    monkeypatch.setattr(
        alert_checker, "_load_service_monitoring",
        AsyncMock(return_value=([SimpleNamespace(name="orders", enabled=False)], 0)),
    )
    monkeypatch.setattr(alert_checker, "delete_alert_state", AsyncMock())
    await alert_checker._check_service_health(state, trigger_after_failures=1)
    assert state.get_entry("external_service_down", "orders") is None
    assert state.get_entry("external_service_down", "billing") is None


@pytest.mark.asyncio
async def test_registry_load_failure_keeps_state(monkeypatch):
    state = AlertStateManager()
    state.update("server_down", "web1", is_healthy=False, trigger_after_failures=1)
    monkeypatch.setattr(
        alert_checker, "_load_server_monitoring", AsyncMock(side_effect=RuntimeError("db down")),
    )
    await alert_checker._check_server_health(state, trigger_after_failures=1)
    assert state.get_entry("server_down", "web1") is not None


# ── User labels ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["job", "instance", "mode", "mountpoint", "__scheme__", "", "1x", "a-b"])
def test_build_targets_drops_reserved_and_invalid_labels(key):
    entry = build_targets([_host("web1", labels=json.dumps({key: "x", "env": "prod"}))])[0]
    assert entry["labels"] == {"host": "web1", "env": "prod"}


@pytest.mark.parametrize("key", ["job", "host", "mountpoint", "__metrics_path__", "", "9a", "a.b"])
def test_host_schema_rejects_reserved_and_invalid_labels(key):
    with pytest.raises(ValidationError):
        MonitoredHostCreate(name="web1", address="10.0.0.1:9100", labels={key: "x"})
    with pytest.raises(ValidationError):
        MonitoredHostUpdate(labels={key: "x"})


def test_host_schema_accepts_ordinary_labels():
    body = MonitoredHostCreate(
        name="web1", address="10.0.0.1:9100", labels={"env": "prod", "team_a": "x"},
    )
    assert body.labels == {"env": "prod", "team_a": "x"}


# ── Unmatched disk mountpoints ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unmatched_mountpoints_raise_a_disk_warning():
    """A typo'd whitelist must not switch disk alerting off silently."""
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _queries({'up{job="nodes"}': [_series("web1", 1)]})
        signals = await evaluate_hosts(
            [_host("web1", disk_mountpoints="/Data")], ServerThresholds(forecast_hours=0)
        )
    disk = next(s for s in signals if s.alert_type == "server_disk")
    assert (disk.is_healthy, disk.severity, disk.value) == (False, "warning", None)
    assert "/Data" in disk.message


@pytest.mark.asyncio
async def test_unmatched_mountpoints_stay_quiet_when_disk_is_off():
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _queries({'up{job="nodes"}': [_series("web1", 1)]})
        signals = await evaluate_hosts(
            [_host("web1", disk_warn_pct=0, disk_crit_pct=0)], ServerThresholds(forecast_hours=0)
        )
    assert all(s.alert_type != "server_disk" for s in signals)


@pytest.mark.asyncio
async def test_failed_disk_query_is_not_read_as_unmatched_mountpoints():
    failed: list[str] = []
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _queries(
            {'up{job="nodes"}': [_series("web1", 1)]}, failing=["node_filesystem_avail_bytes"],
        )
        signals = await evaluate_hosts(
            [_host("web1")], ServerThresholds(forecast_hours=0), failed_queries=failed,
        )
    assert all(s.alert_type != "server_disk" for s in signals)
    assert failed == ["disk"]


@pytest.mark.asyncio
async def test_down_host_gets_no_unmatched_mountpoint_warning():
    with patch.object(server_monitor.prometheus_client, "instant_query") as q:
        q.side_effect = _queries({'up{job="nodes"}': [_series("web1", 0)]})
        signals = await evaluate_hosts([_host("web1")], ServerThresholds(forecast_hours=0))
    assert [s.alert_type for s in signals] == ["server_down"]


# ── file_sd ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unchanged_targets_file_is_not_rewritten(tmp_path):
    path = tmp_path / "nodes.json"
    await server_monitor.write_targets_file([_host("web1")], str(path))
    os.utime(path, (1, 1))
    await server_monitor.write_targets_file([_host("web1")], str(path))
    assert os.stat(path).st_mtime == 1
    await server_monitor.write_targets_file(
        [_host("web1"), _host("web2", "10.0.0.2:9100")], str(path)
    )
    assert os.stat(path).st_mtime != 1
    assert len(json.loads(path.read_text())) == 2


@pytest.mark.asyncio
async def test_reconcile_rewrites_a_lost_targets_file(tmp_path, monkeypatch, db_session):
    from app.config import settings
    from app.models import MonitoredHost, MonitoredService

    db_session.add(MonitoredHost(name="web1", address="10.0.0.1:9100", enabled=True))
    db_session.add(MonitoredService(name="orders", address="10.0.0.9:8080", enabled=True))
    await db_session.commit()
    nodes, gpus, services = tmp_path / "nodes.json", tmp_path / "gpus.json", tmp_path / "svc.json"
    monkeypatch.setattr(settings, "PROMETHEUS_FILE_SD_PATH", str(nodes))
    monkeypatch.setattr(settings, "PROMETHEUS_GPU_FILE_SD_PATH", str(gpus))
    monkeypatch.setattr(settings, "PROMETHEUS_SERVICES_FILE_SD_PATH", str(services))
    nodes.write_text("[]")  # an older snapshot that lost the host

    class _Session:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.database.async_session", lambda: _Session())
    await server_monitor.reconcile_scrape_targets()
    assert json.loads(nodes.read_text())[0]["labels"]["host"] == "web1"
    assert json.loads(services.read_text())[0]["labels"]["service"] == "orders"
