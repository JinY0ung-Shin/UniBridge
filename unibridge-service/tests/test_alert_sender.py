"""Tests for alert_sender module."""
from __future__ import annotations

import json
import logging
import socket

import httpx
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
        template = (
            '{"rate":"{{rate}}","threshold":"{{threshold}}",'
            '"rule":"{{rule_name}}","description":"{{target_description}}"}'
        )
        result = render_template(
            template, alert_type="triggered", target_name="x",
            status="ok", message="m", timestamp="t", recipients="r",
            rate="12.3", threshold="10.0", rule_name="route-alert",
            target_description="Primary API host",
        )
        assert '"rate":"12.3"' in result
        assert '"threshold":"10.0"' in result
        assert '"rule":"route-alert"' in result
        assert '"description":"Primary API host"' in result

    def test_new_placeholders_default_to_empty(self):
        template = (
            '{"rate":"{{rate}}","rule":"{{rule_name}}",'
            '"description":"{{target_description}}"}'
        )
        result = render_template(
            template, alert_type="t", target_name="x",
            status="s", message="m", timestamp="t", recipients="r",
        )
        assert '"rate":""' in result
        assert '"rule":""' in result
        assert '"description":""' in result


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

    def test_render_recipient_items_escapes_quotes_and_backslashes(self):
        template = '{"emailAddress":"{{email}}","recipientType":"TO"}'
        email = 'kim"\\alerts@example.com'
        result = render_recipient_items(template, [email])
        parsed = json.loads(result)
        assert parsed == [{"emailAddress": email, "recipientType": "TO"}]

    def test_render_recipient_items_keeps_injection_as_email_value(self):
        template = '{"emailAddress":"{{email}}","recipientType":"TO"}'
        email = 'victim@example.com","recipientType":"BCC'
        result = render_recipient_items(template, [email])
        parsed = json.loads(result)
        assert parsed == [{"emailAddress": email, "recipientType": "TO"}]

    def test_render_recipient_items_rejects_invalid_json_template(self):
        template = '{"emailAddress":"{{email}}","recipientType":"TO"'
        with pytest.raises(ValueError, match="invalid JSON"):
            render_recipient_items(template, ["kim@company.com"])

    def test_render_recipient_items_rejects_unquoted_email_placeholder(self):
        template = '{"emailAddress":{{email}},"recipientType":"TO"}'
        with pytest.raises(ValueError, match="JSON string value"):
            render_recipient_items(template, ["kim@company.com"])

    def test_render_recipient_items_requires_email_placeholder(self):
        template = '{"recipientType":"TO"}'
        with pytest.raises(ValueError, match="must include"):
            render_recipient_items(template, ["kim@company.com"])

    def test_render_recipient_items_empty_email_list_returns_empty_array(self):
        template = '{"emailAddress":{{email}},"recipientType":"TO"}'
        assert render_recipient_items(template, []) == "[]"

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
    async def test_send_webhook_failure_detail_names_the_exception(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url="http://example.com/hook", status_code=500)
        _, err = await send_webhook(
            url="http://example.com/hook",
            payload='{"msg":"test"}',
            headers=None,
        )
        assert err.startswith("HTTPStatusError: ")
        assert "500" in err

    @pytest.mark.asyncio
    async def test_send_webhook_failure_hides_url_token(self, httpx_mock: HTTPXMock, caplog):
        """The detail is persisted on the history row that alerts.read can list."""
        url = "https://hooks.example.com/services/T1/B2?token=SUPERSECRETTOKEN"
        httpx_mock.add_response(url=url, status_code=403)

        with caplog.at_level(logging.WARNING):
            ok, err = await send_webhook(url=url, payload='{"msg":"test"}', headers=None)

        assert ok is False
        assert "SUPERSECRETTOKEN" not in err
        assert "token=" not in err
        assert "/services/T1/B2" not in err
        assert "https://hooks.example.com/***" in err
        assert "SUPERSECRETTOKEN" not in caplog.text
        assert "/services/T1/B2" not in caplog.text

    @pytest.mark.asyncio
    async def test_send_webhook_failure_hides_token_in_transport_error(self, httpx_mock: HTTPXMock):
        url = "https://hooks.example.com/hook?token=SUPERSECRETTOKEN"
        httpx_mock.add_exception(
            httpx.ConnectError(f"connection to {url} failed"), url=url
        )
        ok, err = await send_webhook(url=url, payload='{"msg":"test"}', headers=None)
        assert ok is False
        assert "SUPERSECRETTOKEN" not in err
        assert err == "ConnectError: connection to https://hooks.example.com/*** failed"

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
