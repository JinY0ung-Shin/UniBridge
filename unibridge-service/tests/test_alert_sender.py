"""Tests for alert_sender module."""
from __future__ import annotations

import json
import socket

import pytest
from pytest_httpx import HTTPXMock

from app.services.alert_sender import render_recipient_items, render_template, send_webhook


class TestRenderTemplate:
    def test_renders_all_placeholders(self):
        template = '{"to":"{{recipients}}","subject":"[UniBridge] {{alert_type}}: {{target_name}}","body":"{{message}} at {{timestamp}}"}'
        result = render_template(
            template,
            alert_type="triggered",
            target_name="order-db",
            status="error",
            message="Connection failed",
            timestamp="2026-04-11T14:30:00",
            recipients="team@co.com",
        )
        assert '"to":"team@co.com"' in result
        assert "order-db" in result
        assert "Connection failed" in result

    def test_unknown_placeholder_left_as_is(self):
        template = '{"note":"{{unknown_var}}"}'
        result = render_template(template, alert_type="triggered", target_name="x",
                                 status="ok", message="m", timestamp="t", recipients="r")
        assert "{{unknown_var}}" in result

    def test_renders_new_placeholders(self):
        template = '{"rate":"{{rate}}","threshold":"{{threshold}}","rule":"{{rule_name}}"}'
        result = render_template(
            template, alert_type="triggered", target_name="x",
            status="ok", message="m", timestamp="t", recipients="r",
            rate="12.3", threshold="10.0", rule_name="route-alert",
        )
        assert '"rate":"12.3"' in result
        assert '"threshold":"10.0"' in result
        assert '"rule":"route-alert"' in result

    def test_new_placeholders_default_to_empty(self):
        template = '{"rate":"{{rate}}","rule":"{{rule_name}}"}'
        result = render_template(
            template, alert_type="t", target_name="x",
            status="s", message="m", timestamp="t", recipients="r",
        )
        assert '"rate":""' in result
        assert '"rule":""' in result


class TestRecipientItemRendering:
    def test_render_recipient_items_builds_json_array(self):
        template = '{"emailAddress":"{{email}}","recipientType":"TO"}'
        result = render_recipient_items(template, ["kim@company.com", "lee@company.com"])
        parsed = json.loads(result)
        assert parsed == [
            {"emailAddress": "kim@company.com", "recipientType": "TO"},
            {"emailAddress": "lee@company.com", "recipientType": "TO"},
        ]

    def test_render_recipient_items_rejects_non_object_template(self):
        template = '"{{email}}"'
        with pytest.raises(ValueError, match="JSON object"):
            render_recipient_items(template, ["kim@company.com"])

    def test_render_template_injects_recipients_json_raw(self):
        payload = render_template(
            '{"recipients":{{recipients_json}},"to":"{{recipients}}"}',
            alert_type="triggered",
            target_name="payment-db",
            status="장애 발생",
            message="Database failed",
            timestamp="2026-05-08T00:00:00+00:00",
            recipients="kim@company.com, lee@company.com",
            recipients_json='[{"emailAddress":"kim@company.com","recipientType":"TO"}]',
        )
        assert json.loads(payload)["recipients"] == [
            {"emailAddress": "kim@company.com", "recipientType": "TO"}
        ]


class TestSendWebhook:
    @pytest.mark.asyncio
    async def test_send_webhook_success(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="http://example.com/hook", status_code=200)
        ok, err = await send_webhook(
            url="http://example.com/hook",
            payload='{"msg":"test"}',
            headers=None,
        )
        assert ok is True
        assert err is None

    @pytest.mark.asyncio
    async def test_send_webhook_failure(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="http://example.com/hook", status_code=500)
        ok, err = await send_webhook(
            url="http://example.com/hook",
            payload='{"msg":"test"}',
            headers=None,
        )
        assert ok is False
        assert err is not None

    @pytest.mark.asyncio
    async def test_send_webhook_with_custom_headers(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="http://example.com/hook", status_code=200)
        ok, _ = await send_webhook(
            url="http://example.com/hook",
            payload='{"msg":"test"}',
            headers={"X-Token": "secret"},
        )
        assert ok is True
        req = httpx_mock.get_request()
        assert req.headers["X-Token"] == "secret"

    @pytest.mark.asyncio
    async def test_send_webhook_rejects_hostname_that_resolves_private(self, monkeypatch):
        def fake_getaddrinfo(*_args, **_kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        ok, err = await send_webhook(
            url="https://hooks.example.com/private",
            payload='{"msg":"test"}',
            headers=None,
        )

        assert ok is False
        assert err is not None
        assert "private/internal" in err
