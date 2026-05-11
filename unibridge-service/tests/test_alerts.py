"""Tests for the health-check alert system."""
from __future__ import annotations

import socket

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import ALL_PERMISSIONS
from app.models import AlertChannel, AlertRule, AlertRuleChannel, AlertHistory
from app.schemas import (
    AlertChannelCreate, AlertRuleCreate, AlertStatusResponse,
)
from app.services.alert_state import AlertStateManager


class TestAlertModels:
    def test_alert_channel_columns(self):
        ch = AlertChannel(name="test", webhook_url="http://example.com/hook", payload_template='{}')
        assert ch.name == "test"
        assert ch.webhook_url == "http://example.com/hook"
        assert ch.enabled is True

    def test_alert_rule_columns(self):
        rule = AlertRule(name="db-check", type="db_health", target="mydb")
        assert rule.type == "db_health"
        assert rule.enabled is True

    def test_alert_rule_channel_columns(self):
        arc = AlertRuleChannel(rule_id=1, channel_id=1, recipients='["a@b.com"]')
        assert arc.recipients == '["a@b.com"]'

    def test_alert_history_columns(self):
        h = AlertHistory(rule_id=1, channel_id=1, alert_type="triggered", target="mydb", message="down")
        assert h.alert_type == "triggered"
        assert h.success is None


class TestAlertSchemas:
    def test_channel_create_valid(self):
        ch = AlertChannelCreate(
            name="email",
            webhook_url="http://mail.internal/api/send",
            payload_template='{"to":"{{recipients}}","subject":"{{alert_type}}"}',
        )
        assert ch.name == "email"
        assert ch.headers is None
        assert ch.enabled is True

    def test_rule_create_db_health(self):
        rule = AlertRuleCreate(
            name="order-db-check",
            type="db_health",
            target="order-db",
            channels=[{"channel_id": 1, "recipients": ["team@co.com"]}],
        )
        assert rule.threshold is None
        assert len(rule.channels) == 1

    def test_rule_create_error_rate_requires_threshold(self):
        rule = AlertRuleCreate(
            name="error-check",
            type="error_rate",
            target="*",
            threshold=10.0,
            channels=[{"channel_id": 1, "recipients": ["ops@co.com"]}],
        )
        assert rule.threshold == 10.0

    def test_rule_create_route_error_rate(self):
        rule = AlertRuleCreate(
            name="route-err-check",
            type="route_error_rate",
            target="*",
            threshold=5.0,
            channels=[{"channel_id": 1, "recipients": ["ops@co.com"]}],
        )
        assert rule.type == "route_error_rate"
        assert rule.threshold == 5.0

    def test_rule_create_rejects_unknown_type(self):
        with pytest.raises(Exception):
            AlertRuleCreate(
                name="bogus", type="does_not_exist", target="*",
                channels=[],
            )

    def test_channel_create_rejects_userinfo_in_webhook_url(self):
        with pytest.raises(Exception):
            AlertChannelCreate(
                name="userinfo-leak",
                webhook_url="https://token:secret@hooks.example.com/path",
                payload_template="{}",
            )

    def test_channel_create_rejects_hostname_that_resolves_private(self, monkeypatch):
        def fake_getaddrinfo(*_args, **_kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.10.5", 443))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        with pytest.raises(Exception):
            AlertChannelCreate(
                name="private-dns",
                webhook_url="https://hooks.example.com/private",
                payload_template="{}",
            )

    def test_alert_status_response(self):
        s = AlertStatusResponse(target="mydb", type="db_health", status="alert", since="2026-04-11T12:00:00")
        assert s.status == "alert"


class TestMaskWebhookUrl:
    def test_strips_userinfo(self):
        from app.routers.alerts import _mask_webhook_url
        masked = _mask_webhook_url("https://token:secret@hooks.example.com/path/X?q=1")
        assert "token" not in masked
        assert "secret" not in masked
        assert masked == "https://hooks.example.com/***"

    def test_preserves_port(self):
        from app.routers.alerts import _mask_webhook_url
        assert _mask_webhook_url("https://hooks.example.com:8443/svc/abc") == "https://hooks.example.com:8443/***"

    def test_strips_path_query_fragment(self):
        from app.routers.alerts import _mask_webhook_url
        assert _mask_webhook_url("https://hooks.example.com/p?q=1#f") == "https://hooks.example.com/***"

    def test_unparseable_url_falls_back_to_stars(self):
        from app.routers.alerts import _mask_webhook_url
        assert _mask_webhook_url("not a url") == "***"


class TestAlertPermissions:
    def test_alerts_read_in_all_permissions(self):
        assert "alerts.read" in ALL_PERMISSIONS

    def test_alerts_write_in_all_permissions(self):
        assert "alerts.write" in ALL_PERMISSIONS

class TestAlertState:
    """N-strike behavior. Defaults to N=2 (matching production default)."""

    def test_initial_state_is_ok(self):
        mgr = AlertStateManager()
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_n2_cold_start_first_failure_is_silent(self):
        mgr = AlertStateManager()
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=2,
        )
        assert transition is None
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_n2_cold_start_second_failure_triggers(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=2,
        )
        assert transition == "triggered"
        assert mgr.get_status("db_health", "mydb") == "alert"

    def test_n1_cold_start_first_failure_triggers_immediately(self):
        mgr = AlertStateManager()
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=1,
        )
        assert transition == "triggered"
        assert mgr.get_status("db_health", "mydb") == "alert"

    def test_already_alert_stays_silent_on_more_failures(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=2,
        )
        assert transition is None

    def test_resolved_emitted_on_healthy_after_alert(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=True, trigger_after_failures=2,
        )
        assert transition == "resolved"
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_healthy_after_unnotified_failure_does_not_resolve(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=True, trigger_after_failures=2,
        )
        assert transition is None
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_flap_resets_counter_no_emission(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=3)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=3)
        # Healthy mid-streak resets counter
        mgr.update("db_health", "mydb", is_healthy=True, trigger_after_failures=3)
        # Now must take 3 more failures to fire
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=3)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=3)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=3,
        )
        assert transition == "triggered"

    def test_no_transition_when_still_ok(self):
        mgr = AlertStateManager()
        transition = mgr.update(
            "db_health", "mydb", is_healthy=True, trigger_after_failures=2,
        )
        assert transition is None

    def test_get_all_alerts(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "db1", is_healthy=False, trigger_after_failures=1)
        mgr.update("upstream_health", "svc1", is_healthy=False, trigger_after_failures=1)
        mgr.update("db_health", "db2", is_healthy=True, trigger_after_failures=1)
        alerts = mgr.get_all_alerts()
        assert len(alerts) == 2
        targets = {a["target"] for a in alerts}
        assert targets == {"db1", "svc1"}

    def test_get_all_statuses_includes_known_ok_and_alert_states(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "db1", is_healthy=False, trigger_after_failures=1)
        mgr.update("db_health", "db2", is_healthy=True, trigger_after_failures=1)
        statuses = mgr.get_all_statuses()
        status_by_target = {entry["target"]: entry["status"] for entry in statuses}
        assert status_by_target == {"db1": "alert", "db2": "ok"}

    def test_reset_clears_all(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=1)
        mgr.reset()
        assert mgr.get_status("db_health", "mydb") == "ok"
        assert mgr.get_all_alerts() == []

    def test_n_lowered_mid_flight_fires_on_next_failure(self):
        mgr = AlertStateManager()
        # Build up to fail_count=2 under N=5
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=5)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=5)
        # N tightened to 3; next failure should fire (counter becomes 3)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=3,
        )
        assert transition == "triggered"

    @pytest.mark.asyncio
    async def test_persist_and_restore_alert_state(self, seeded_db):
        from app.services.alert_state import (
            load_alert_state_from_db,
            save_alert_state_to_db,
        )

        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        mgr = AlertStateManager()
        mgr.update("db_health", "main-db", is_healthy=False, trigger_after_failures=2)
        mgr.update("db_health", "main-db", is_healthy=False, trigger_after_failures=2)

        async with session_factory() as db:
            await save_alert_state_to_db(db, mgr, "db_health", "main-db")

        restored = AlertStateManager()
        async with session_factory() as db:
            await load_alert_state_from_db(db, restored)

        assert restored.get_status("db_health", "main-db") == "alert"
        statuses = restored.get_all_statuses()
        assert statuses[0]["target"] == "main-db"
        assert statuses[0]["status"] == "alert"
        # fail_count must round-trip
        entry = restored.get_entry("db_health", "main-db")
        assert entry is not None
        assert entry["fail_count"] == 2
