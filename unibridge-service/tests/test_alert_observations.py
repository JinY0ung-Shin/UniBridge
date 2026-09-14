from datetime import timedelta

import pytest

from app.db_types import utcnow
from app.routers import alerts
from app.services.alert_state import AlertStateManager, load_alert_state_from_db, save_alert_state_to_db


def observe(state, healthy=False, value=95):
    return state.update("server_cpu", "worker", is_healthy=healthy,
                        trigger_after_failures=1, resolve_after_successes=5,
                        observation={"message": "CPU utilisation", "value": value, "threshold": 90, "unit": "%"})


@pytest.mark.asyncio
async def test_incident_evidence_survives_recovery_and_restart(db_session):
    state = AlertStateManager()
    observe(state)
    for _ in range(3):
        observe(state, healthy=True, value=54)
    state.mark_unavailable("server_cpu", "worker", "Prometheus query failed")
    await save_alert_state_to_db(db_session, state, "server_cpu", "worker")
    restored = AlertStateManager()
    await load_alert_state_from_db(db_session, restored)
    entry = restored.get_entry("server_cpu", "worker")
    assert entry["status"] == "alert"
    assert entry["success_count"] == 3
    assert entry["details"]["incident"]["value"] == 95
    assert entry["details"]["current"]["value"] == 54
    assert entry["details"]["collection_error"] == "Prometheus query failed"
    observe(restored, healthy=True, value=50)
    assert "collection_error" not in restored.get_entry("server_cpu", "worker")["details"]
    assert observe(restored, healthy=True) == "resolved"
    observe(restored, value=99)
    assert restored.get_entry("server_cpu", "worker")["details"]["incident"]["value"] == 99


@pytest.mark.asyncio
async def test_status_distinguishes_recovering_stale_and_legacy(db_session, monkeypatch):
    state = AlertStateManager()
    observe(state)
    observe(state, healthy=True, value=54)
    monkeypatch.setattr(alerts, "_alert_state", state)
    result = await alerts.alert_status(_user=None, db=db_session)
    row = result.items[0]
    assert row.current.value == 54
    assert row.incident.value == 95
    assert row.current.healthy and row.status == "alert"
    assert row.success_count == 1 and row.resolve_after_successes == 5
    assert not row.stale
    state._states[("server_cpu", "worker")]["details"]["current"]["checked_at"] = (utcnow() - timedelta(minutes=3)).isoformat()
    assert (await alerts.alert_status(_user=None, db=db_session)).items[0].stale
    state.set_entry("db_health", "legacy", status="alert", since=utcnow().isoformat())
    row = (await alerts.alert_status(_user=None, db=db_session)).items[1]
    assert row.current is None and row.incident is None and row.stale


@pytest.mark.parametrize("raw,expected", [
    ("password authentication failed for user private-user", "authentication_failed"),
    ("connection refused at private-host", "connection_refused"),
    ("getaddrinfo failed", "dns_failed"),
    ("certificate verify failed", "tls_failed"),
    ("Timeout connecting with secret=password", "timeout"),
    ("unknown error with secret=password", None),
])
def test_connection_diagnostics_do_not_expose_raw_errors(raw, expected):
    from app.services.alert_checker import _connection_failure_reason
    assert _connection_failure_reason(raw) == expected


@pytest.mark.asyncio
async def test_route_observation_refreshes_without_resending_alert(monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import alert_checker
    state = AlertStateManager()
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(alert_checker, "dispatch_alert", dispatch)
    monkeypatch.setattr(alert_checker, "_persist_state_safely", AsyncMock())
    monkeypatch.setattr(alert_checker, "_get_route_label", AsyncMock(return_value="Query API"))
    for rate in (18.2, 12.4):
        await alert_checker._evaluate_route_error_rule(
            state, route_id="route-1", rate=rate, threshold=10,
            trigger_after_failures=1, resolve_after_successes=5,
            sample_count=250, min_requests=20,
        )
    details = state.get_entry("route_error_rate", "route-1")["details"]
    assert details["incident"]["value"] == 18.2
    assert details["current"]["value"] == 12.4
    assert details["current"]["requests"] == 250
    dispatch.assert_awaited_once()
