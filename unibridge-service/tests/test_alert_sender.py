"""Tests for alert_sender module."""
from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from app.services.alert_sender import render_template, send_webhook


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
