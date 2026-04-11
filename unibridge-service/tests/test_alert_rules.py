"""Integration tests for alert rules, history, and status API."""
from __future__ import annotations

import pytest
from tests.conftest import auth_header


class TestAlertRulesAPI:
    @pytest.mark.asyncio
    async def test_create_rule_with_channel(self, client, admin_token):
        ch = await client.post("/admin/alerts/channels", json={
            "name": "rule-test-ch",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        ch_id = ch.json()["id"]

        resp = await client.post("/admin/alerts/rules", json={
            "name": "order-db-check",
            "type": "db_health",
            "target": "order-db",
            "channels": [{"channel_id": ch_id, "recipients": ["team@co.com"]}],
        }, headers=auth_header(admin_token))
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "order-db-check"
        assert len(data["channels"]) == 1
        assert data["channels"][0]["recipients"] == ["team@co.com"]

    @pytest.mark.asyncio
    async def test_list_rules(self, client, admin_token):
        resp = await client.get("/admin/alerts/rules", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_update_rule(self, client, admin_token):
        ch = await client.post("/admin/alerts/channels", json={
            "name": "rule-upd-ch",
            "webhook_url": "http://example.com/hook",
            "payload_template": "{}",
        }, headers=auth_header(admin_token))
        ch_id = ch.json()["id"]

        create = await client.post("/admin/alerts/rules", json={
            "name": "upd-rule",
            "type": "db_health",
            "target": "db1",
            "channels": [{"channel_id": ch_id, "recipients": ["a@b.com"]}],
        }, headers=auth_header(admin_token))
        rule_id = create.json()["id"]

        resp = await client.put(f"/admin/alerts/rules/{rule_id}", json={
            "name": "upd-rule-v2",
            "enabled": False,
        }, headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["name"] == "upd-rule-v2"
        assert resp.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_delete_rule(self, client, admin_token):
        create = await client.post("/admin/alerts/rules", json={
            "name": "del-rule",
            "type": "upstream_health",
            "target": "*",
            "channels": [],
        }, headers=auth_header(admin_token))
        rule_id = create.json()["id"]
        resp = await client.delete(f"/admin/alerts/rules/{rule_id}", headers=auth_header(admin_token))
        assert resp.status_code == 204


class TestAlertHistoryAPI:
    @pytest.mark.asyncio
    async def test_list_history_empty(self, client, admin_token):
        resp = await client.get("/admin/alerts/history", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json() == []


class TestAlertStatusAPI:
    @pytest.mark.asyncio
    async def test_status_empty(self, client, admin_token):
        resp = await client.get("/admin/alerts/status", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json() == []
