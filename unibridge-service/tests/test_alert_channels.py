"""Integration tests for alert channels API."""
from __future__ import annotations

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
