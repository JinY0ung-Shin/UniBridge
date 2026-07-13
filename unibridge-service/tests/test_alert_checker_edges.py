"""Edge and failure-path coverage for the periodic alert checker."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import alert_checker
from app.services.alert_state import AlertStateManager


class _Result:
    def __init__(self, value=None, *, rows=None) -> None:
        self.value = value
        self.rows = list(rows or [])

    def scalar_one_or_none(self):
        return self.value

    def one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _SessionContext:
    def __init__(self, db=None, *, enter_error: Exception | None = None) -> None:
        self.db = db
        self.enter_error = enter_error

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(None, 60), (5, 30), (7200, 3600)],
)
async def test_get_check_interval_uses_default_and_clamps(
    monkeypatch, stored, expected
) -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(stored)))
    monkeypatch.setattr(alert_checker, "async_session", lambda: _SessionContext(db))

    assert await alert_checker._get_check_interval_seconds() == expected


async def test_get_check_interval_falls_back_when_database_fails(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        alert_checker,
        "async_session",
        lambda: _SessionContext(enter_error=RuntimeError("database offline")),
    )

    assert await alert_checker._get_check_interval_seconds() == alert_checker.CHECK_INTERVAL
    assert "Failed to load alert check interval" in caplog.text


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(None, 2), (0, 1), (4, 4), (99, 10)],
)
async def test_get_trigger_after_failures_uses_default_and_clamps(
    monkeypatch, stored, expected
) -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(stored)))
    monkeypatch.setattr(alert_checker, "async_session", lambda: _SessionContext(db))

    assert await alert_checker._get_trigger_after_failures() == expected


async def test_get_trigger_after_failures_falls_back_when_database_fails(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        alert_checker,
        "async_session",
        lambda: _SessionContext(enter_error=RuntimeError("database offline")),
    )

    assert await alert_checker._get_trigger_after_failures() == 2
    assert "Failed to load alert trigger_after_failures" in caplog.text


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, (10.0, 20)),
        ((None, None), (10.0, 0)),
        ((-5, -1), (0.0, 0)),
        ((150, 25), (100.0, 25)),
    ],
)
async def test_load_route_error_settings_defaults_and_clamps(row, expected) -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(row)))

    assert await alert_checker._load_route_error_settings(db) == expected


async def test_refresh_route_labels_skips_item_without_id_and_uses_uris(monkeypatch):
    from app.services import apisix_client

    monkeypatch.setattr(alert_checker, "_ROUTE_LABEL_CACHE", {})
    monkeypatch.setattr(alert_checker, "_ROUTE_ID_BY_NAME", {})
    monkeypatch.setattr(alert_checker, "_ROUTE_LABEL_CACHE_TS", 0.0)
    monkeypatch.setattr(
        apisix_client,
        "list_resources",
        AsyncMock(
            return_value={
                "items": [
                    {"name": "missing-id", "uri": "/ignored"},
                    {"id": "multi", "uris": ["/first", "/second"]},
                ]
            }
        ),
    )

    await alert_checker._refresh_route_labels()

    assert alert_checker._ROUTE_LABEL_CACHE == {"multi": "/first"}


async def test_check_db_health_isolates_each_connection_failure(
    monkeypatch, caplog
) -> None:
    from app.services.connection_manager import connection_manager

    monkeypatch.setattr(connection_manager, "list_aliases", lambda: ["ok", "broken"])
    monkeypatch.setattr(
        connection_manager,
        "test_connection",
        AsyncMock(side_effect=[(True, "connected"), RuntimeError("dial failed")]),
    )

    assert await alert_checker._check_db_health() == [("ok", True), ("broken", False)]
    assert "DB health check failed for 'broken'" in caplog.text


async def test_check_nas_health_isolates_each_connection_failure(
    monkeypatch, caplog
) -> None:
    from app.services.nas_manager import nas_manager

    monkeypatch.setattr(nas_manager, "list_aliases", lambda: ["ok", "broken"])
    monkeypatch.setattr(
        nas_manager,
        "test_connection",
        AsyncMock(side_effect=[(True, "mounted"), RuntimeError("mount vanished")]),
    )

    assert await alert_checker._check_nas_health() == [("ok", True), ("broken", False)]
    assert "NAS health check failed for 'broken'" in caplog.text


async def test_check_upstream_health_classifies_nodes_and_caches_names(monkeypatch):
    from app.services import apisix_client

    monkeypatch.setattr(alert_checker, "_UPSTREAM_NAME_BY_ID", {})
    monkeypatch.setattr(
        apisix_client,
        "list_resources",
        AsyncMock(
            return_value={
                "items": [
                    {"id": "healthy", "name": "Orders", "nodes": {"a:80": 1}},
                    {"id": 7, "nodes": {"b:80": 0}},
                    {"nodes": ["unsupported-shape"]},
                ]
            }
        ),
    )

    assert await alert_checker._check_upstream_health() == [
        ("healthy", True),
        ("7", False),
        ("unknown", False),
    ]
    assert alert_checker._UPSTREAM_NAME_BY_ID == {"healthy": "Orders"}


async def test_check_upstream_health_returns_empty_when_apisix_fails(
    monkeypatch, caplog
) -> None:
    from app.services import apisix_client

    monkeypatch.setattr(
        apisix_client,
        "list_resources",
        AsyncMock(side_effect=RuntimeError("apisix offline")),
    )

    assert await alert_checker._check_upstream_health() == []
    assert "Upstream health check failed" in caplog.text


async def test_check_route_error_rate_skips_malformed_prometheus_rows(monkeypatch):
    from app.services import prometheus_client

    totals = [
        {"metric": {}, "value": [0, "10"]},
        {"metric": {"route": "bad"}, "value": [0, "invalid"]},
        {"metric": {"route": "zero"}, "value": [0, "0"]},
        {"metric": {"route": "nan-total"}, "value": [0, "nan"]},
        {"metric": {"route": "good"}, "value": [0, "20"]},
    ]
    errors = [
        {"metric": {}, "value": [0, "2"]},
        {"metric": {"route": "bad"}, "value": [0, "invalid"]},
        {"metric": {"route": "nan-error"}, "value": [0, "nan"]},
        {"metric": {"route": "good"}, "value": [0, "4"]},
    ]
    monkeypatch.setattr(
        prometheus_client,
        "instant_query",
        AsyncMock(side_effect=[totals, errors]),
    )

    assert await alert_checker._check_route_error_rate() == [("good", 20.0, 20.0)]


def _settings_row(**overrides):
    values = {
        "server_disk_warn_pct": 70,
        "server_disk_crit_pct": 85,
        "server_cpu_warn_pct": 75,
        "server_mem_warn_pct": 80,
        "server_disk_forecast_hours": 12,
        "repeat_alert_after_cycles": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("settings", [None, _settings_row()])
async def test_load_server_monitoring_handles_default_and_configured_settings(
    monkeypatch, settings
) -> None:
    host = SimpleNamespace(name="host-a", enabled=True)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(settings), _Result(rows=[host])])
    )
    monkeypatch.setattr(alert_checker, "async_session", lambda: _SessionContext(db))

    hosts, thresholds, repeat = await alert_checker._load_server_monitoring()

    assert hosts == [host]
    if settings is None:
        assert thresholds == alert_checker.ServerThresholds()
        assert repeat == 0
    else:
        assert thresholds.disk_warn_pct == 70
        assert thresholds.disk_crit_pct == 85
        assert thresholds.cpu_warn_pct == 75
        assert thresholds.mem_warn_pct == 80
        assert thresholds.forecast_hours == 12
        assert repeat == 3


async def test_check_server_health_isolates_config_load_failure(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        alert_checker,
        "_load_server_monitoring",
        AsyncMock(side_effect=RuntimeError("bad configuration")),
    )
    evaluate = AsyncMock()
    monkeypatch.setattr(alert_checker.server_monitor, "evaluate_hosts", evaluate)

    await alert_checker._check_server_health(
        AlertStateManager(), trigger_after_failures=1
    )

    evaluate.assert_not_awaited()
    assert "Server health check skipped" in caplog.text


async def test_check_server_health_skips_when_no_host_is_enabled(monkeypatch) -> None:
    disabled = SimpleNamespace(name="host-a", enabled=False)
    monkeypatch.setattr(
        alert_checker,
        "_load_server_monitoring",
        AsyncMock(return_value=([disabled], alert_checker.ServerThresholds(), 0)),
    )
    evaluate = AsyncMock()
    monkeypatch.setattr(alert_checker.server_monitor, "evaluate_hosts", evaluate)

    await alert_checker._check_server_health(
        AlertStateManager(), trigger_after_failures=1
    )

    evaluate.assert_not_awaited()


async def test_check_server_health_persists_and_dispatches_transition(monkeypatch) -> None:
    host = SimpleNamespace(name="host-a", enabled=True)
    signal = SimpleNamespace(
        alert_type="server_cpu_high",
        target="host-a",
        display="Host A",
        is_healthy=False,
        severity="warning",
        value=95.0,
        threshold=90.0,
        message="CPU is high",
        monitor_label="CPU 사용률",
        description="Primary server",
    )
    monkeypatch.setattr(
        alert_checker,
        "_load_server_monitoring",
        AsyncMock(return_value=([host], alert_checker.ServerThresholds(), 2)),
    )
    monkeypatch.setattr(
        alert_checker.server_monitor,
        "evaluate_hosts",
        AsyncMock(return_value=[signal]),
    )
    persist = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(alert_checker, "_persist_state_safely", persist)
    monkeypatch.setattr(alert_checker, "dispatch_alert", dispatch)
    state = AlertStateManager()

    await alert_checker._check_server_health(state, trigger_after_failures=1)

    persist.assert_awaited_once_with(state, "server_cpu_high", "host-a")
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs == {
        "resource_type": "server",
        "resource_id": "host-a",
        "alert_type": "triggered",
        "target": "host-a",
        "message": "CPU is high",
        "display_target": "Host A",
        "rate": 95.0,
        "threshold": 90.0,
        "monitor_label": "CPU 사용률",
        "severity": "warning",
        "target_description": "Primary server",
    }


async def test_load_service_monitoring_reads_services_and_repeat(monkeypatch) -> None:
    service = SimpleNamespace(name="orders", enabled=True)
    settings = SimpleNamespace(repeat_alert_after_cycles=4)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(settings), _Result(rows=[service])])
    )
    monkeypatch.setattr(alert_checker, "async_session", lambda: _SessionContext(db))

    services, repeat = await alert_checker._load_service_monitoring()

    assert services == [service]
    assert repeat == 4


async def test_check_service_health_isolates_config_load_failure(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        alert_checker,
        "_load_service_monitoring",
        AsyncMock(side_effect=RuntimeError("bad configuration")),
    )
    evaluate = AsyncMock()
    monkeypatch.setattr(alert_checker.server_monitor, "evaluate_services", evaluate)

    await alert_checker._check_service_health(
        AlertStateManager(), trigger_after_failures=1
    )

    evaluate.assert_not_awaited()
    assert "External-service health check skipped" in caplog.text


async def test_run_single_check_stops_route_step_when_prometheus_is_unavailable(
    monkeypatch
) -> None:
    monkeypatch.setattr(alert_checker, "_check_db_health", AsyncMock(return_value=[]))
    monkeypatch.setattr(alert_checker, "_check_nas_health", AsyncMock(return_value=[]))
    monkeypatch.setattr(alert_checker, "_check_upstream_health", AsyncMock(return_value=[]))
    monkeypatch.setattr(alert_checker, "_check_server_health", AsyncMock())
    monkeypatch.setattr(alert_checker, "_check_service_health", AsyncMock())
    monkeypatch.setattr(
        alert_checker, "_check_route_error_rate", AsyncMock(return_value=None)
    )
    session = MagicMock()
    monkeypatch.setattr(alert_checker, "async_session", session)

    await alert_checker.run_single_check(
        AlertStateManager(), trigger_after_failures=2
    )

    session.assert_not_called()


async def test_run_single_check_does_not_resolve_processed_active_route(
    monkeypatch
) -> None:
    state = AlertStateManager()
    state.update("route_error_rate", "route-a", is_healthy=False, trigger_after_failures=1)
    monkeypatch.setattr(alert_checker, "_check_db_health", AsyncMock(return_value=[]))
    monkeypatch.setattr(alert_checker, "_check_nas_health", AsyncMock(return_value=[]))
    monkeypatch.setattr(alert_checker, "_check_upstream_health", AsyncMock(return_value=[]))
    monkeypatch.setattr(alert_checker, "_check_server_health", AsyncMock())
    monkeypatch.setattr(alert_checker, "_check_service_health", AsyncMock())
    monkeypatch.setattr(
        alert_checker,
        "_check_route_error_rate",
        AsyncMock(return_value=[("route-a", 25.0, 40.0)]),
    )
    monkeypatch.setattr(
        alert_checker, "_resolve_route_id", AsyncMock(return_value="route-a")
    )
    evaluate = AsyncMock()
    monkeypatch.setattr(alert_checker, "_evaluate_route_error_rule", evaluate)
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result((10.0, 20))))
    monkeypatch.setattr(alert_checker, "async_session", lambda: _SessionContext(db))

    await alert_checker.run_single_check(state, trigger_after_failures=1)

    evaluate.assert_awaited_once()
    assert evaluate.await_args.kwargs["route_id"] == "route-a"
    assert evaluate.await_args.kwargs["rate"] == 25.0


async def test_start_checker_logs_cycle_failure_and_keeps_scheduling(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        alert_checker, "_get_check_interval_seconds", AsyncMock(return_value=30)
    )
    monkeypatch.setattr(
        alert_checker, "_get_trigger_after_failures", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        alert_checker,
        "run_single_check",
        AsyncMock(side_effect=RuntimeError("unexpected check failure")),
    )
    monkeypatch.setattr(alert_checker, "_monotonic", MagicMock(side_effect=[10.0, 12.0]))

    async def _stop_after_sleep(delay):
        assert delay == 28.0
        raise RuntimeError("stop loop")

    monkeypatch.setattr(alert_checker.asyncio, "sleep", _stop_after_sleep)

    task = await alert_checker.start_checker(AlertStateManager())
    with pytest.raises(RuntimeError, match="stop loop"):
        await task

    assert "Alert checker cycle failed" in caplog.text
