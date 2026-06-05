"""Integration tests for /admin/alerts router endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete as sa_delete, insert as sa_insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models import (
    AlertHistory,
    AlertSettings,
    DBConnection,
    NASConnection,
    ResourceOwner,
    S3Connection,
)
from app.routers import alerts as alerts_router
from app.services.alert_state import AlertStateManager
from tests.conftest import auth_header


WEBHOOK = "https://hooks.example.com/svc"
TEMPLATE = '{"text":"{{message}}","status":"{{status}}"}'


class _SettingsResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _RacingSettingsDb:
    def __init__(self):
        self.existing = AlertSettings(
            id=1,
            admin_emails="[]",
            route_error_threshold_pct=10.0,
            check_interval_seconds=60,
        )
        self.execute = AsyncMock(side_effect=[
            _SettingsResult(None),
            _SettingsResult(self.existing),
        ])
        self.rollback = AsyncMock()
        self.refresh = AsyncMock()
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        raise IntegrityError("insert alert_settings", {}, Exception("unique constraint"))


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


# ── Settings ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_and_update_alert_settings(client, admin_token):
    ch = await client.post(
        "/admin/alerts/channels",
        json={"name": "mail-settings", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    assert ch.status_code == 201

    expected = {
        "mail_channel_id": ch.json()["id"],
        "admin_emails": ["ops@company.com", "lead@company.com"],
        "route_error_threshold_pct": 12.5,
        "check_interval_seconds": 90,
    }
    resp = await client.put(
        "/admin/alerts/settings",
        json=expected,
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    for key, value in expected.items():
        assert body[key] == value

    get_resp = await client.get("/admin/alerts/settings", headers=auth_header(admin_token))
    assert get_resp.status_code == 200
    body = get_resp.json()
    for key, value in expected.items():
        assert body[key] == value


@pytest.mark.asyncio
async def test_update_alert_settings_dedupes_admin_emails(client, admin_token):
    resp = await client.put(
        "/admin/alerts/settings",
        json={"admin_emails": [" a@x.com ", "a@x.com", "b@x.com", "", " b@x.com "]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["admin_emails"] == ["a@x.com", "b@x.com"]


@pytest.mark.asyncio
async def test_update_alert_settings_accepts_empty_admin_emails(client, admin_token):
    resp = await client.put(
        "/admin/alerts/settings",
        json={"admin_emails": []},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["admin_emails"] == []


@pytest.mark.asyncio
async def test_delete_mail_channel_in_use_returns_409(client, admin_token):
    ch = await client.post(
        "/admin/alerts/channels",
        json={"name": "mail-in-use", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    assert ch.status_code == 201
    settings = await client.put(
        "/admin/alerts/settings",
        json={"mail_channel_id": ch.json()["id"]},
        headers=auth_header(admin_token),
    )
    assert settings.status_code == 200

    resp = await client.delete(
        f"/admin/alerts/channels/{ch.json()['id']}",
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 409
    assert "default mail channel" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_alert_settings_invalid_channel_does_not_create_settings_row(
    client, admin_token, seeded_db,
):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await db.execute(sa_delete(AlertSettings))
        await db.commit()

    resp = await client.put(
        "/admin/alerts/settings",
        json={"mail_channel_id": 9999},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 422
    async with session_factory() as db:
        settings = (await db.execute(select(AlertSettings))).scalar_one_or_none()
    assert settings is None


@pytest.mark.asyncio
async def test_get_or_create_alert_settings_handles_concurrent_insert_race():
    db = _RacingSettingsDb()

    settings = await alerts_router._get_or_create_alert_settings(db)

    assert settings is db.existing
    assert len(db.added) == 1
    db.rollback.assert_awaited_once()
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_update_alert_settings_rejects_explicit_numeric_nulls(client, admin_token):
    interval_resp = await client.put(
        "/admin/alerts/settings",
        json={"check_interval_seconds": None},
        headers=auth_header(admin_token),
    )
    assert interval_resp.status_code == 422

    threshold_resp = await client.put(
        "/admin/alerts/settings",
        json={"route_error_threshold_pct": None},
        headers=auth_header(admin_token),
    )
    assert threshold_resp.status_code == 422


@pytest.mark.asyncio
async def test_update_alert_settings_trigger_after_failures(client, admin_token):
    resp = await client.put(
        "/admin/alerts/settings",
        json={"trigger_after_failures": 5},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trigger_after_failures"] == 5

    resp_get = await client.get(
        "/admin/alerts/settings",
        headers=auth_header(admin_token),
    )
    assert resp_get.status_code == 200
    assert resp_get.json()["trigger_after_failures"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", [0, -1, 11, 100])
async def test_update_alert_settings_trigger_after_failures_out_of_range(
    client, admin_token, invalid_value,
):
    resp = await client.put(
        "/admin/alerts/settings",
        json={"trigger_after_failures": invalid_value},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_alert_settings_trigger_after_failures_rejects_null(
    client, admin_token,
):
    resp = await client.put(
        "/admin/alerts/settings",
        json={"trigger_after_failures": None},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


# ── Recipients test ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recipients_test_sends_to_explicit_emails(client, admin_token):
    ch = await client.post(
        "/admin/alerts/channels",
        json={
            "name": "mail-recipients-test",
            "webhook_url": WEBHOOK,
            "payload_template": '{"to":{{recipients_json}},"text":"{{message}}"}',
            "recipient_item_template": '{"email":"{{email}}"}',
            "headers": {"X-Test": "yes"},
        },
        headers=auth_header(admin_token),
    )
    assert ch.status_code == 201

    with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as send:
        resp = await client.post(
            "/admin/alerts/settings/recipients/test",
            json={
                "mail_channel_id": ch.json()["id"],
                "emails": ["ops@example.com", "lead@example.com"],
            },
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "error": None}
    send.assert_awaited_once()
    call = send.await_args.kwargs
    assert call["url"] == WEBHOOK
    assert call["headers"] == {"X-Test": "yes"}
    assert "ops@example.com" in call["payload"]
    assert "lead@example.com" in call["payload"]


@pytest.mark.asyncio
async def test_recipients_test_requires_recipient_item_template(client, admin_token):
    ch = await client.post(
        "/admin/alerts/channels",
        json={
            "name": "mail-no-recipient-template",
            "webhook_url": WEBHOOK,
            "payload_template": '{"text":"{{message}}"}',
        },
        headers=auth_header(admin_token),
    )
    assert ch.status_code == 201

    with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as send:
        resp = await client.post(
            "/admin/alerts/settings/recipients/test",
            json={
                "mail_channel_id": ch.json()["id"],
                "emails": ["ops@example.com"],
            },
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert "recipient_item_template" in resp.json()["error"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipients_test_channel_not_found(client, admin_token):
    resp = await client.post(
        "/admin/alerts/settings/recipients/test",
        json={"mail_channel_id": 9999, "emails": ["ops@example.com"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert "not found" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_recipients_test_rejects_empty_emails(client, admin_token):
    ch = await client.post(
        "/admin/alerts/channels",
        json={"name": "mail-empty-recipients", "webhook_url": WEBHOOK,
              "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    resp = await client.post(
        "/admin/alerts/settings/recipients/test",
        json={"mail_channel_id": ch.json()["id"], "emails": []},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


# ── Resource Owners (담당자) ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resource_owner_upsert_and_delete_for_db(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(DBConnection(
            alias="orders-db",
            db_type="postgres",
            host="localhost",
            port=5432,
            database="orders",
            username="orders",
            password_encrypted="encrypted",
        ))
        await db.commit()

    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})
        put_resp = await client.put(
            "/admin/alerts/resource-owners/db/orders-db",
            json={"emails": [" orders@example.com ", "orders@example.com", "ops@example.com"]},
            headers=auth_header(admin_token),
        )

        assert put_resp.status_code == 200, put_resp.text
        body = put_resp.json()
        assert body["resource_type"] == "db"
        assert body["resource_id"] == "orders-db"
        assert body["display_name"] == "orders-db"
        assert body["emails"] == ["orders@example.com", "ops@example.com"]

        list_resp = await client.get(
            "/admin/alerts/resource-owners",
            headers=auth_header(admin_token),
        )

        assert list_resp.status_code == 200, list_resp.text
        rows = list_resp.json()
        assert any(
            row["resource_type"] == "db"
            and row["resource_id"] == "orders-db"
            and row["display_name"] == "orders-db"
            and row["emails"] == ["orders@example.com", "ops@example.com"]
            for row in rows
        )

        delete_resp = await client.delete(
            "/admin/alerts/resource-owners/db/orders-db",
            headers=auth_header(admin_token),
        )

    assert delete_resp.status_code == 204

    async with session_factory() as db:
        owner = (await db.execute(select(ResourceOwner))).scalar_one_or_none()
    assert owner is None


@pytest.mark.asyncio
async def test_resource_owner_empty_emails_clears_assignees(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(DBConnection(
            alias="clear-db",
            db_type="postgres",
            host="localhost",
            port=5432,
            database="clear",
            username="clear",
            password_encrypted="encrypted",
        ))
        db.add(ResourceOwner(
            resource_type="db",
            resource_id="clear-db",
            emails='["owner@example.com"]',
        ))
        await db.commit()

    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})
        resp = await client.put(
            "/admin/alerts/resource-owners/db/clear-db",
            json={"emails": []},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["emails"] == []
    async with session_factory() as db:
        owner = (await db.execute(select(ResourceOwner))).scalar_one_or_none()
    assert owner is None


@pytest.mark.asyncio
async def test_resource_owner_rejects_unknown_resource(client, admin_token, seeded_db):
    resp = await client.put(
        "/admin/alerts/resource-owners/db/missing-db",
        json={"emails": ["missing@example.com"]},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_resource_owner_upsert_handles_concurrent_insert_conflict(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(DBConnection(
            alias="race-db",
            db_type="postgres",
            host="localhost",
            port=5432,
            database="race",
            username="race",
            password_encrypted="encrypted",
        ))
        await db.commit()

    original_commit = AsyncSession.commit
    injected = False

    async def commit_with_concurrent_insert(session: AsyncSession) -> None:
        nonlocal injected
        if not injected:
            injected = True
            async with seeded_db.begin() as conn:
                await conn.execute(sa_insert(ResourceOwner).values(
                    resource_type="db",
                    resource_id="race-db",
                    emails='["pre@example.com"]',
                ))
            raise IntegrityError("insert resource owner", {}, Exception("unique constraint"))
        await original_commit(session)

    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})
        with patch("app.routers.alerts.AsyncSession.commit", new=commit_with_concurrent_insert):
            resp = await client.put(
                "/admin/alerts/resource-owners/db/race-db",
                json={"emails": ["race@example.com"]},
                headers=auth_header(admin_token),
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["emails"] == ["race@example.com"]
    async with session_factory() as db:
        owner = (await db.execute(select(ResourceOwner))).scalar_one()
    assert json.loads(owner.emails) == ["race@example.com"]


@pytest.mark.asyncio
async def test_resource_owner_rejects_unsupported_resource_type(client, admin_token):
    resp = await client.put(
        "/admin/alerts/resource-owners/cache/redis",
        json={"emails": ["x@example.com"]},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 422

    delete_resp = await client.delete(
        "/admin/alerts/resource-owners/cache/redis",
        headers=auth_header(admin_token),
    )

    assert delete_resp.status_code == 422


@pytest.mark.asyncio
async def test_resource_owner_validates_route_and_nas_but_rejects_upstream(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(NASConnection(alias="reports-nas", base_path="/mnt/nas/reports"))
        await db.commit()

    async def list_resources(resource: str) -> dict[str, object]:
        if resource == "routes":
            return {"items": [{"id": "orders-route", "name": "Orders Route"}]}
        return {"items": []}

    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)

        route_resp = await client.put(
            "/admin/alerts/resource-owners/route/orders-route",
            json={"emails": ["gateway@example.com"]},
            headers=auth_header(admin_token),
        )
        nas_resp = await client.put(
            "/admin/alerts/resource-owners/nas/reports-nas",
            json={"emails": ["nas@example.com"]},
            headers=auth_header(admin_token),
        )
        missing_resp = await client.put(
            "/admin/alerts/resource-owners/route/missing-route",
            json={"emails": ["gateway@example.com"]},
            headers=auth_header(admin_token),
        )
        upstream_resp = await client.put(
            "/admin/alerts/resource-owners/upstream/orders-upstream",
            json={"emails": ["gateway@example.com"]},
            headers=auth_header(admin_token),
        )

    assert route_resp.status_code == 200, route_resp.text
    assert route_resp.json()["display_name"] == "Orders Route"
    assert route_resp.json()["emails"] == ["gateway@example.com"]
    assert nas_resp.status_code == 200, nas_resp.text
    assert nas_resp.json()["display_name"] == "reports-nas"
    assert nas_resp.json()["emails"] == ["nas@example.com"]
    assert missing_resp.status_code == 422
    assert upstream_resp.status_code == 422


@pytest.mark.asyncio
async def test_resource_owner_lists_apisix_resources_with_email_mapping(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(NASConnection(alias="reports-nas", base_path="/mnt/nas/reports"))
        db.add(ResourceOwner(
            resource_type="route",
            resource_id="orders-route",
            emails='["route@example.com"]',
        ))
        db.add(ResourceOwner(
            resource_type="nas",
            resource_id="reports-nas",
            emails='["nas@example.com"]',
        ))
        await db.commit()

    async def list_resources(resource: str) -> dict[str, object]:
        if resource == "routes":
            return {"items": [
                {"id": "orders-route", "name": "Orders Route"},
                {"id": "llm-proxy", "name": "LLM Proxy"},
            ]}
        return {"items": []}

    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        resp = await client.get(
            "/admin/alerts/resource-owners",
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert any(
        row["resource_type"] == "route"
        and row["resource_id"] == "orders-route"
        and row["display_name"] == "Orders Route"
        and row["emails"] == ["route@example.com"]
        for row in rows
    )
    assert any(
        row["resource_type"] == "nas"
        and row["resource_id"] == "reports-nas"
        and row["display_name"] == "reports-nas"
        and row["emails"] == ["nas@example.com"]
        for row in rows
    )
    assert not any(
        row["resource_type"] == "route" and row["resource_id"] == "llm-proxy"
        for row in rows
    )
    assert not any(row["resource_type"] == "upstream" for row in rows)


@pytest.mark.asyncio
async def test_resource_owner_list_returns_503_when_apisix_fails(client, admin_token):
    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=RuntimeError("apisix down"))
        resp = await client.get(
            "/admin/alerts/resource-owners",
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_resource_owner_permissions_use_alert_read_and_write(
    client, admin_token, alerts_reader_token, seeded_db,
):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(S3Connection(
            alias="reports-bucket",
            endpoint_url="https://s3.example.com",
            region="us-east-1",
            access_key_id_encrypted="encrypted",
            secret_access_key_encrypted="encrypted",
        ))
        await db.commit()

    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        list_resp = await client.get(
            "/admin/alerts/resource-owners",
            headers=auth_header(alerts_reader_token),
        )
        assert list_resp.status_code == 200

        write_denied = await client.put(
            "/admin/alerts/resource-owners/s3/reports-bucket",
            json={"emails": ["user@example.com"]},
            headers=auth_header(alerts_reader_token),
        )
        assert write_denied.status_code == 403

        delete_denied = await client.delete(
            "/admin/alerts/resource-owners/s3/reports-bucket",
            headers=auth_header(alerts_reader_token),
        )
        assert delete_denied.status_code == 403


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
    # History entries no longer carry a rule_id.
    assert all("rule_id" not in r for r in rows)

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
    state.update("db_health", "db-x", is_healthy=False, trigger_after_failures=2)
    state.update("db_health", "db-x", is_healthy=False, trigger_after_failures=2)  # transition to active alert
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
