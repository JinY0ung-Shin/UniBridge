"""Integration tests for alert rules, history, and status API."""
from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AlertHistory
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


class TestAlertRuleTestAPI:
    async def _create_channel(self, client, admin_token, name: str,
                              url: str = "http://hook.example.com/send",
                              enabled: bool = True):
        resp = await client.post("/admin/alerts/channels", json={
            "name": name,
            "webhook_url": url,
            "payload_template": '{"rule":"{{rule_name}}","to":"{{recipients}}","msg":"{{message}}","type":"{{alert_type}}"}',
            "enabled": enabled,
        }, headers=auth_header(admin_token))
        return resp.json()["id"]

    async def _create_rule(self, client, admin_token, *, name: str, channels: list[dict],
                           enabled: bool = True):
        resp = await client.post("/admin/alerts/rules", json={
            "name": name,
            "type": "db_health",
            "target": "test-db",
            "enabled": enabled,
            "channels": channels,
        }, headers=auth_header(admin_token))
        return resp.json()["id"]

    @pytest.mark.asyncio
    async def test_rule_test_sends_to_all_channels(self, client, admin_token, httpx_mock: HTTPXMock):
        ch1 = await self._create_channel(client, admin_token, "rt-ch1", "http://hook1.example.com/a")
        ch2 = await self._create_channel(client, admin_token, "rt-ch2", "http://hook2.example.com/b")
        rule_id = await self._create_rule(client, admin_token, name="multi-ch-rule", channels=[
            {"channel_id": ch1, "recipients": ["alice@x.com"]},
            {"channel_id": ch2, "recipients": ["bob@x.com", "carol@x.com"]},
        ])
        httpx_mock.add_response(url="http://hook1.example.com/a", status_code=200)
        httpx_mock.add_response(url="http://hook2.example.com/b", status_code=200)

        resp = await client.post(f"/admin/alerts/rules/{rule_id}/test",
                                 headers=auth_header(admin_token))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 2
        for result in body["results"]:
            assert result["success"] is True
            assert result["skipped"] is False
            assert result["error"] is None

    @pytest.mark.asyncio
    async def test_rule_test_injects_actual_recipients(self, client, admin_token, httpx_mock: HTTPXMock):
        ch = await self._create_channel(client, admin_token, "rt-recip-ch", "http://hook.example.com/recip")
        rule_id = await self._create_rule(client, admin_token, name="recip-rule", channels=[
            {"channel_id": ch, "recipients": ["alice@x.com", "bob@x.com"]},
        ])
        httpx_mock.add_response(url="http://hook.example.com/recip", status_code=200)

        resp = await client.post(f"/admin/alerts/rules/{rule_id}/test",
                                 headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["results"][0]["recipients"] == ["alice@x.com", "bob@x.com"]

        req = httpx_mock.get_request()
        assert b"alice@x.com, bob@x.com" in req.content
        assert b"recip-rule" in req.content

    @pytest.mark.asyncio
    async def test_rule_test_skips_disabled_channel(self, client, admin_token, httpx_mock: HTTPXMock):
        ch = await self._create_channel(client, admin_token, "rt-disabled-ch",
                                        "http://hook.example.com/dis", enabled=False)
        rule_id = await self._create_rule(client, admin_token, name="dis-rule", channels=[
            {"channel_id": ch, "recipients": ["a@x.com"]},
        ])

        resp = await client.post(f"/admin/alerts/rules/{rule_id}/test",
                                 headers=auth_header(admin_token))
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["skipped"] is True
        assert results[0]["success"] is None
        assert results[0]["error"] == "channel disabled"
        assert len(httpx_mock.get_requests()) == 0

    @pytest.mark.asyncio
    async def test_rule_test_works_on_disabled_rule(self, client, admin_token, httpx_mock: HTTPXMock):
        ch = await self._create_channel(client, admin_token, "rt-dr-ch", "http://hook.example.com/dr")
        rule_id = await self._create_rule(client, admin_token, name="dr-rule",
                                          channels=[{"channel_id": ch, "recipients": ["a@x.com"]}],
                                          enabled=False)
        httpx_mock.add_response(url="http://hook.example.com/dr", status_code=200)

        resp = await client.post(f"/admin/alerts/rules/{rule_id}/test",
                                 headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["results"][0]["success"] is True

    @pytest.mark.asyncio
    async def test_rule_test_not_found(self, client, admin_token):
        resp = await client.post("/admin/alerts/rules/999999/test",
                                 headers=auth_header(admin_token))
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rule_test_requires_write_permission(self, client, viewer_token):
        resp = await client.post("/admin/alerts/rules/1/test",
                                 headers=auth_header(viewer_token))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_rule_test_does_not_write_history(self, client, admin_token,
                                                    seeded_db, httpx_mock: HTTPXMock):
        ch = await self._create_channel(client, admin_token, "rt-hist-ch",
                                        "http://hook.example.com/hist")
        rule_id = await self._create_rule(client, admin_token, name="hist-rule",
                                          channels=[{"channel_id": ch, "recipients": ["a@x.com"]}])
        httpx_mock.add_response(url="http://hook.example.com/hist", status_code=200)

        await client.post(f"/admin/alerts/rules/{rule_id}/test",
                          headers=auth_header(admin_token))

        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as s:
            rows = (await s.execute(select(AlertHistory))).scalars().all()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_rule_test_reports_webhook_failure(self, client, admin_token, httpx_mock: HTTPXMock):
        ch_ok = await self._create_channel(client, admin_token, "rt-ok-ch",
                                           "http://hook.example.com/ok")
        ch_fail = await self._create_channel(client, admin_token, "rt-fail-ch",
                                             "http://hook.example.com/fail")
        rule_id = await self._create_rule(client, admin_token, name="mixed-rule", channels=[
            {"channel_id": ch_ok, "recipients": ["a@x.com"]},
            {"channel_id": ch_fail, "recipients": ["b@x.com"]},
        ])
        httpx_mock.add_response(url="http://hook.example.com/ok", status_code=200)
        httpx_mock.add_response(url="http://hook.example.com/fail", status_code=500)

        resp = await client.post(f"/admin/alerts/rules/{rule_id}/test",
                                 headers=auth_header(admin_token))
        assert resp.status_code == 200
        results = {r["channel_id"]: r for r in resp.json()["results"]}
        assert results[ch_ok]["success"] is True
        assert results[ch_fail]["success"] is False
        assert results[ch_fail]["error"] is not None


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
