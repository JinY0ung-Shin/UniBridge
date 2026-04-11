"""Tests for the health-check alert system."""
from __future__ import annotations

import pytest

from app.models import AlertChannel, AlertRule, AlertRuleChannel, AlertHistory
from app.schemas import (
    AlertChannelCreate, AlertChannelUpdate, AlertChannelResponse,
    AlertRuleCreate, AlertRuleUpdate, AlertRuleResponse,
    AlertHistoryResponse, AlertStatusResponse,
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

    def test_alert_status_response(self):
        s = AlertStatusResponse(target="mydb", type="db_health", status="alert", since="2026-04-11T12:00:00")
        assert s.status == "alert"
