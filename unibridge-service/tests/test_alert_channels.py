"""Integration tests for alert channels API."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from tests.conftest import auth_header


class TestAlertChannelsAPI:
    @pytest.mark.asyncio
    async def test_create_channel(self, client, admin_token):
        resp = await client.post("/admin/alerts/channels", json={
            "name": "email-api",
            "webhook_url": "http://mail.internal/api/send",
            "payload_template": '{"to":"{{recipients}}","subject":"{{alert_type}}: {{target_name}}"}',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "email-api"
        assert data["enabled"] is True

    @pytest.mark.asyncio
    async def test_list_channels(self, client, admin_token):
        await client.post("/admin/alerts/channels", json={
            "name": "ch1",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        resp = await client.get("/admin/alerts/channels", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    @pytest.mark.asyncio
    async def test_list_channels_masks_webhook_and_headers_for_non_writers(
        self, client, admin_token, viewer_token
    ):
        secret_path = "/services/T123/B456/SECRETTOKEN"
        await client.post("/admin/alerts/channels", json={
            "name": "secret-ch",
            "webhook_url": f"https://hooks.example.com{secret_path}",
            "payload_template": "{}",
            "headers": {"Authorization": "Bearer super-secret"},
        }, headers=auth_header(admin_token))

        # viewer: alerts.read but not alerts.write — must see masked URL and no headers
        resp = await client.get("/admin/alerts/channels", headers=auth_header(viewer_token))
        assert resp.status_code == 200
        viewer_rows = [c for c in resp.json() if c["name"] == "secret-ch"]
        assert len(viewer_rows) == 1
        viewer_ch = viewer_rows[0]
        assert "SECRETTOKEN" not in viewer_ch["webhook_url"]
        assert secret_path not in viewer_ch["webhook_url"]
        assert viewer_ch["webhook_url"] == "https://hooks.example.com/***"
        assert viewer_ch["headers"] is None

        # admin: alerts.write — full URL and headers preserved
        resp = await client.get("/admin/alerts/channels", headers=auth_header(admin_token))
        admin_rows = [c for c in resp.json() if c["name"] == "secret-ch"]
        assert len(admin_rows) == 1
        admin_ch = admin_rows[0]
        assert admin_ch["webhook_url"] == f"https://hooks.example.com{secret_path}"
        assert admin_ch["headers"] == {"Authorization": "Bearer super-secret"}

    @pytest.mark.asyncio
    async def test_update_channel(self, client, admin_token):
        create = await client.post("/admin/alerts/channels", json={
            "name": "ch-update",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        ch_id = create.json()["id"]
        resp = await client.put(f"/admin/alerts/channels/{ch_id}", json={
            "name": "ch-updated",
            "enabled": False,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["name"] == "ch-updated"
        assert resp.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_channel_round_trips_recipient_item_template(self, client, admin_token):
        template = '{"emailAddress":"{{email}}","recipientType":"TO"}'
        resp = await client.post("/admin/alerts/channels", json={
            "name": "mail-api",
            "webhook_url": "http://mail.internal/api/send",
            "payload_template": '{"recipients":{{recipients_json}}}',
            "recipient_item_template": template,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        assert resp.json()["recipient_item_template"] == template

        ch_id = resp.json()["id"]
        update = await client.put(f"/admin/alerts/channels/{ch_id}", json={
            "recipient_item_template": '{"mail":"{{email}}"}',
        }, headers=auth_header(admin_token))
        assert update.status_code == 200
        assert update.json()["recipient_item_template"] == '{"mail":"{{email}}"}'

    @pytest.mark.asyncio
    async def test_channel_test_injects_rendered_recipients_json(self, client, admin_token):
        resp = await client.post("/admin/alerts/channels", json={
            "name": "mail-json",
            "webhook_url": "http://mail.internal/api/send",
            "payload_template": '{"recipients":{{recipients_json}}}',
            "recipient_item_template": '{"emailAddress":"{{email}}","recipientType":"TO"}',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        ch_id = resp.json()["id"]

        with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as mock_send:
            test_resp = await client.post(
                f"/admin/alerts/channels/{ch_id}/test",
                headers=auth_header(admin_token),
            )

        assert test_resp.status_code == 200
        payload = json.loads(mock_send.await_args.kwargs["payload"])
        assert payload["recipients"][0]["emailAddress"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_channel_test_with_malformed_recipient_item_template_does_not_send(self, client, admin_token):
        resp = await client.post("/admin/alerts/channels", json={
            "name": "mail-json-bad-template",
            "webhook_url": "http://mail.internal/api/send",
            "payload_template": '{"recipients":{{recipients_json}}}',
            "recipient_item_template": '{"emailAddress":{{email}}}',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        ch_id = resp.json()["id"]

        with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as mock_send:
            test_resp = await client.post(
                f"/admin/alerts/channels/{ch_id}/test",
                headers=auth_header(admin_token),
            )

        assert test_resp.status_code == 200
        assert test_resp.json()["success"] is False
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_mail_channel_test_requires_recipient_item_template(self, client, admin_token):
        resp = await client.post("/admin/alerts/channels", json={
            "name": "mail-json-missing-template",
            "webhook_url": "http://mail.internal/api/send",
            "payload_template": '{"recipients":{{recipients_json}}}',
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        ch_id = resp.json()["id"]
        settings = await client.put("/admin/alerts/settings", json={
            "mail_channel_id": ch_id,
        }, headers=auth_header(admin_token))
        assert settings.status_code == 200

        with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as mock_send:
            test_resp = await client.post(
                f"/admin/alerts/channels/{ch_id}/test",
                headers=auth_header(admin_token),
            )

        assert test_resp.status_code == 200
        assert test_resp.json()["success"] is False
        assert "recipient_item_template" in test_resp.json()["error"]
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_channel(self, client, admin_token):
        create = await client.post("/admin/alerts/channels", json={
            "name": "ch-delete",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        ch_id = create.json()["id"]
        resp = await client.delete(f"/admin/alerts/channels/{ch_id}", headers=auth_header(admin_token))
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_viewer_cannot_create_channel(self, client, viewer_token):
        resp = await client.post("/admin/alerts/channels", json={
            "name": "nope",
            "webhook_url": "http://example.com",
            "payload_template": "{}",
        }, headers=auth_header(viewer_token))
        assert resp.status_code == 403
