"""Tests for the health-check alert system."""
from __future__ import annotations

import pytest

from app.models import AlertChannel, AlertRule, AlertRuleChannel, AlertHistory


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
