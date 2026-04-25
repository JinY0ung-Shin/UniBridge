"""Tests for the health-check alert system."""
from __future__ import annotations

import pytest

from app.models import AlertChannel, AlertRule, AlertRuleChannel, AlertHistory
from app.schemas import (
    AlertChannelCreate, AlertChannelUpdate, AlertChannelResponse,
    AlertRuleCreate, AlertRuleUpdate, AlertRuleResponse,
    AlertHistoryResponse, AlertStatusResponse,
    S3ConnectionCreate,
)


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

    @pytest.mark.parametrize(
        "url",
        [
            "http://0177.0.0.1/",
            "http://0x7f.0.0.1/",
            "http://127.1/",
            "http://017700000001/",
            "http://0x7f000001/",
            "http://%6C%6F%63%61%6C%68%6F%73%74/",
            "http://169.254.169.254/",
            "http://[::ffff:127.0.0.1]/",
        ],
    )
    def test_channel_create_rejects_ssrf_bypass_hosts(self, url):
        with pytest.raises(Exception):
            AlertChannelCreate(
                name="blocked",
                webhook_url=url,
                payload_template="{}",
            )

    def test_s3_private_endpoint_requires_explicit_opt_in(self):
        with pytest.raises(Exception):
            S3ConnectionCreate(
                alias="minio",
                endpoint_url="http://10.0.0.5:9000",
                access_key_id="access",
                secret_access_key="secret",
            )

        conn = S3ConnectionCreate(
            alias="minio",
            endpoint_url="http://10.0.0.5:9000",
            allow_private_endpoints=True,
            access_key_id="access",
            secret_access_key="secret",
        )
        assert conn.endpoint_url == "http://10.0.0.5:9000"

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
        import pytest
        with pytest.raises(Exception):
            AlertRuleCreate(
                name="bogus", type="does_not_exist", target="*",
                channels=[],
            )

    def test_alert_status_response(self):
        s = AlertStatusResponse(target="mydb", type="db_health", status="alert", since="2026-04-11T12:00:00")
        assert s.status == "alert"


from app.auth import ALL_PERMISSIONS


class TestAlertPermissions:
    def test_alerts_read_in_all_permissions(self):
        assert "alerts.read" in ALL_PERMISSIONS

    def test_alerts_write_in_all_permissions(self):
        assert "alerts.write" in ALL_PERMISSIONS


from app.services.alert_state import AlertStateManager


class TestAlertState:
    def test_initial_state_is_ok(self):
        mgr = AlertStateManager()
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_transition_ok_to_alert(self):
        mgr = AlertStateManager()
        transition = mgr.update("db_health", "mydb", is_healthy=False)
        assert transition == "triggered"
        assert mgr.get_status("db_health", "mydb") == "alert"

    def test_no_transition_when_still_alert(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False)
        transition = mgr.update("db_health", "mydb", is_healthy=False)
        assert transition is None

    def test_transition_alert_to_ok(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False)
        transition = mgr.update("db_health", "mydb", is_healthy=True)
        assert transition == "resolved"
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_no_transition_when_still_ok(self):
        mgr = AlertStateManager()
        transition = mgr.update("db_health", "mydb", is_healthy=True)
        assert transition is None

    def test_get_all_alerts(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "db1", is_healthy=False)
        mgr.update("upstream_health", "svc1", is_healthy=False)
        mgr.update("db_health", "db2", is_healthy=True)
        alerts = mgr.get_all_alerts()
        assert len(alerts) == 2
        targets = {a["target"] for a in alerts}
        assert targets == {"db1", "svc1"}

    def test_reset_clears_all(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False)
        mgr.reset()
        assert mgr.get_status("db_health", "mydb") == "ok"
        assert mgr.get_all_alerts() == []
