"""Integration tests for /admin/alerts router endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models import AlertChannel, AlertHistory
from app.routers import alerts as alerts_router
from app.services.alert_state import AlertStateManager
from tests.conftest import auth_header


WEBHOOK = "https://hooks.example.com/svc"
TEMPLATE = '{"text":"{{message}}","status":"{{status}}"}'


# ── Channels ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_channels_empty(client, admin_token):
    resp = await client.get("/admin/alerts/channels", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_channel_success(client, admin_token):
    resp = await client.post(
        "/admin/alerts/channels",
        json={
            "name": "ops",
            "webhook_url": WEBHOOK,
            "payload_template": TEMPLATE,
            "headers": {"X-Token": "abc"},
            "enabled": True,
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "ops"
    assert body["headers"] == {"X-Token": "abc"}


@pytest.mark.asyncio
async def test_create_channel_duplicate_name(client, admin_token):
    payload = {
        "name": "dup-ch",
        "webhook_url": WEBHOOK,
        "payload_template": TEMPLATE,
    }
    r1 = await client.post(
        "/admin/alerts/channels",
        json=payload,
        headers=auth_header(admin_token),
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/admin/alerts/channels",
        json=payload,
        headers=auth_header(admin_token),
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_list_channels_after_create(client, admin_token):
    await client.post(
        "/admin/alerts/channels",
        json={"name": "list-ch", "webhook_url": WEBHOOK, "payload_template": TEMPLATE,
              "headers": {"H": "v"}},
        headers=auth_header(admin_token),
    )
    resp = await client.get("/admin/alerts/channels", headers=auth_header(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["headers"] == {"H": "v"}


@pytest.mark.asyncio
async def test_update_channel_success(client, admin_token):
    create = await client.post(
        "/admin/alerts/channels",
        json={"name": "upd", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    cid = create.json()["id"]

    resp = await client.put(
        f"/admin/alerts/channels/{cid}",
        json={
            "name": "upd2",
            "webhook_url": "https://hooks2.example.com/x",
            "payload_template": '{"new":true}',
            "headers": {"H2": "v2"},
            "enabled": False,
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "upd2"
    assert body["headers"] == {"H2": "v2"}
    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_update_channel_missing_fields_keeps_existing(client, admin_token):
    create = await client.post(
        "/admin/alerts/channels",
        json={"name": "keep", "webhook_url": WEBHOOK, "payload_template": TEMPLATE,
              "headers": {"K": "V"}},
        headers=auth_header(admin_token),
    )
    cid = create.json()["id"]
    resp = await client.put(
        f"/admin/alerts/channels/{cid}",
        json={},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["headers"] == {"K": "V"}


@pytest.mark.asyncio
async def test_update_channel_404(client, admin_token):
    resp = await client.put(
        "/admin/alerts/channels/9999",
        json={"name": "x"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_channel_duplicate_name(client, admin_token):
    a = await client.post(
        "/admin/alerts/channels",
        json={"name": "first", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    await client.post(
        "/admin/alerts/channels",
        json={"name": "second", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    resp = await client.put(
        f"/admin/alerts/channels/{a.json()['id']}",
        json={"name": "second"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_channel_success(client, admin_token):
    create = await client.post(
        "/admin/alerts/channels",
        json={"name": "del", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    cid = create.json()["id"]
    resp = await client.delete(
        f"/admin/alerts/channels/{cid}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_channel_404(client, admin_token):
    resp = await client.delete(
        "/admin/alerts/channels/9999",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_channel_success(client, admin_token):
    create = await client.post(
        "/admin/alerts/channels",
        json={"name": "tester", "webhook_url": WEBHOOK, "payload_template": TEMPLATE,
              "headers": {"X": "y"}},
        headers=auth_header(admin_token),
    )
    cid = create.json()["id"]

    with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as mock_send:
        resp = await client.post(
            f"/admin/alerts/channels/{cid}/test",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "error": None}
    mock_send.assert_awaited_once()
    call_kwargs = mock_send.await_args.kwargs
    assert call_kwargs["url"] == WEBHOOK
    assert call_kwargs["headers"] == {"X": "y"}


@pytest.mark.asyncio
async def test_test_channel_failure(client, admin_token):
    create = await client.post(
        "/admin/alerts/channels",
        json={"name": "tfail", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    cid = create.json()["id"]
    with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(False, "timeout"))):
        resp = await client.post(
            f"/admin/alerts/channels/{cid}/test",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json() == {"success": False, "error": "timeout"}


@pytest.mark.asyncio
async def test_test_channel_not_found(client, admin_token):
    resp = await client.post(
        "/admin/alerts/channels/9999/test",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


# ── Rules ───────────────────────────────────────────────────────────────────

async def _create_channel(client, admin_token, name="ch") -> int:
    resp = await client.post(
        "/admin/alerts/channels",
        json={"name": name, "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_create_rule_with_channels(client, admin_token):
    cid = await _create_channel(client, admin_token, "rule-ch")
    resp = await client.post(
        "/admin/alerts/rules",
        json={
            "name": "db1-down",
            "type": "db_health",
            "target": "db1",
            "channels": [{"channel_id": cid, "recipients": ["a@b.com"]}],
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["channels"][0]["channel_name"] == "rule-ch"
    assert body["channels"][0]["recipients"] == ["a@b.com"]


@pytest.mark.asyncio
async def test_list_rules(client, admin_token):
    cid = await _create_channel(client, admin_token, "lst-ch")
    await client.post(
        "/admin/alerts/rules",
        json={"name": "r1", "type": "db_health", "target": "db1",
              "channels": [{"channel_id": cid, "recipients": []}]},
        headers=auth_header(admin_token),
    )
    resp = await client.get("/admin/alerts/rules", headers=auth_header(admin_token))
    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) == 1
    assert rules[0]["name"] == "r1"


@pytest.mark.asyncio
async def test_update_rule_replaces_channels(client, admin_token):
    cid1 = await _create_channel(client, admin_token, "old-ch")
    cid2 = await _create_channel(client, admin_token, "new-ch")
    create = await client.post(
        "/admin/alerts/rules",
        json={"name": "r-upd", "type": "db_health", "target": "old-db",
              "channels": [{"channel_id": cid1, "recipients": ["x@y"]}]},
        headers=auth_header(admin_token),
    )
    rid = create.json()["id"]

    resp = await client.put(
        f"/admin/alerts/rules/{rid}",
        json={
            "name": "r-upd2",
            "type": "error_rate",
            "target": "new-db",
            "threshold": 12.5,
            "enabled": False,
            "channels": [{"channel_id": cid2, "recipients": ["z@y"]}],
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "r-upd2"
    assert body["threshold"] == 12.5
    assert body["enabled"] is False
    assert len(body["channels"]) == 1
    assert body["channels"][0]["channel_id"] == cid2


@pytest.mark.asyncio
async def test_update_rule_partial_no_channels(client, admin_token):
    cid = await _create_channel(client, admin_token, "part-ch")
    create = await client.post(
        "/admin/alerts/rules",
        json={"name": "r-part", "type": "db_health", "target": "x",
              "channels": [{"channel_id": cid, "recipients": []}]},
        headers=auth_header(admin_token),
    )
    rid = create.json()["id"]
    resp = await client.put(
        f"/admin/alerts/rules/{rid}",
        json={"enabled": False},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    # channels untouched
    assert len(resp.json()["channels"]) == 1


@pytest.mark.asyncio
async def test_update_rule_404(client, admin_token):
    resp = await client.put(
        "/admin/alerts/rules/9999",
        json={"name": "x"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_rule_success(client, admin_token):
    cid = await _create_channel(client, admin_token, "del-ch")
    create = await client.post(
        "/admin/alerts/rules",
        json={"name": "r-del", "type": "db_health", "target": "x",
              "channels": [{"channel_id": cid, "recipients": []}]},
        headers=auth_header(admin_token),
    )
    rid = create.json()["id"]
    resp = await client.delete(
        f"/admin/alerts/rules/{rid}",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_rule_404(client, admin_token):
    resp = await client.delete(
        "/admin/alerts/rules/9999",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rule_with_deleted_channel_shows_deleted(client, admin_token, app, seeded_db):
    """If a channel is deleted directly in DB, rule response shows 'deleted'."""
    cid = await _create_channel(client, admin_token, "doomed")
    create = await client.post(
        "/admin/alerts/rules",
        json={"name": "ghost", "type": "db_health", "target": "x",
              "channels": [{"channel_id": cid, "recipients": ["a@b"]}]},
        headers=auth_header(admin_token),
    )
    assert create.status_code == 201
    # Remove channel directly via DB
    SessionLocal = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        from sqlalchemy import select
        ch = (await db.execute(select(AlertChannel).where(AlertChannel.id == cid))).scalar_one()
        await db.delete(ch)
        await db.commit()

    resp = await client.get("/admin/alerts/rules", headers=auth_header(admin_token))
    body = resp.json()
    assert body[0]["channels"][0]["channel_name"] == "deleted"


@pytest.mark.asyncio
async def test_test_rule_404(client, admin_token):
    resp = await client.post(
        "/admin/alerts/rules/9999/test",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_rule_with_enabled_disabled_deleted_channels(client, admin_token, seeded_db):
    """Mixed channel scenario: enabled / disabled / deleted."""
    cid_ok = await _create_channel(client, admin_token, "ok-ch")
    cid_off = await _create_channel(client, admin_token, "off-ch")
    cid_del = await _create_channel(client, admin_token, "del-ch")

    # Disable one channel
    await client.put(
        f"/admin/alerts/channels/{cid_off}",
        json={"enabled": False},
        headers=auth_header(admin_token),
    )

    create = await client.post(
        "/admin/alerts/rules",
        json={
            "name": "mixed", "type": "error_rate", "target": "*", "threshold": 5.0,
            "channels": [
                {"channel_id": cid_ok, "recipients": ["ops@example.com"]},
                {"channel_id": cid_off, "recipients": ["off@example.com"]},
                {"channel_id": cid_del, "recipients": ["del@example.com"]},
            ],
        },
        headers=auth_header(admin_token),
    )
    rid = create.json()["id"]

    # Hard-delete the third channel
    SessionLocal = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        from sqlalchemy import select
        ch = (await db.execute(select(AlertChannel).where(AlertChannel.id == cid_del))).scalar_one()
        await db.delete(ch)
        await db.commit()

    with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as send:
        resp = await client.post(
            f"/admin/alerts/rules/{rid}/test",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    results = {r["channel_name"]: r for r in resp.json()["results"]}
    assert results["ok-ch"]["success"] is True
    assert results["ok-ch"]["skipped"] is False
    assert results["off-ch"]["skipped"] is True
    assert results["off-ch"]["error"] == "channel disabled"
    assert results["deleted"]["skipped"] is True
    assert results["deleted"]["error"] == "channel deleted"
    # only one webhook actually sent
    assert send.await_count == 1


# ── History ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_filters(client, admin_token, seeded_db):
    SessionLocal = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    base = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    async with SessionLocal() as db:
        for i in range(4):
            db.add(AlertHistory(
                alert_type="triggered" if i < 2 else "resolved",
                target="db1" if i % 2 == 0 else "db2",
                message=f"m{i}",
                recipients=json.dumps(["a@b"]) if i == 0 else None,
                sent_at=base + timedelta(minutes=i),
                success=True if i % 2 == 0 else False,
                error_detail=None,
            ))
        await db.commit()

    resp = await client.get(
        "/admin/alerts/history?alert_type=triggered",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert all(r["alert_type"] == "triggered" for r in rows)
    assert len(rows) == 2

    resp_t = await client.get(
        "/admin/alerts/history?target=db1",
        headers=auth_header(admin_token),
    )
    assert all(r["target"] == "db1" for r in resp_t.json())

    from_dt = (base+timedelta(minutes=2)).isoformat().replace("+", "%2B")
    to_dt = (base+timedelta(minutes=3)).isoformat().replace("+", "%2B")
    resp_range = await client.get(
        f"/admin/alerts/history?from_date={from_dt}&to_date={to_dt}",
        headers=auth_header(admin_token),
    )
    assert resp_range.status_code == 200
    assert len(resp_range.json()) == 2

    resp_paginated = await client.get(
        "/admin/alerts/history?limit=2&offset=1",
        headers=auth_header(admin_token),
    )
    assert len(resp_paginated.json()) == 2


# ── Status ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alert_status_no_state(client, admin_token):
    alerts_router.set_alert_state(None)
    resp = await client.get("/admin/alerts/status", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_alert_status_with_state(client, admin_token):
    state = AlertStateManager()
    state.update("db_health", "db-x", is_healthy=False)
    state.update("db_health", "db-x", is_healthy=False)  # transition to active alert
    alerts_router.set_alert_state(state)
    try:
        resp = await client.get("/admin/alerts/status", headers=auth_header(admin_token))
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["target"] == "db-x"
        assert rows[0]["status"] == "alert"
    finally:
        alerts_router.set_alert_state(None)
