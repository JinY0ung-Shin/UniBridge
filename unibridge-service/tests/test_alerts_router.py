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
    AlertChannel,
    AlertHistory,
    AlertSettings,
    AlertState,
    DBConnection,
    OwnerGroup,
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


@pytest.mark.asyncio
async def test_get_and_update_alert_settings(client, admin_token, seeded_db):
    ch = await client.post(
        "/admin/alerts/channels",
        json={"name": "mail-settings", "webhook_url": WEBHOOK, "payload_template": TEMPLATE},
        headers=auth_header(admin_token),
    )
    assert ch.status_code == 201
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = OwnerGroup(
            name="fallback-owners",
            emails='["ops@company.com"]',
        )
        db.add(group)
        await db.commit()
        await db.refresh(group)
        fallback_owner_group_id = group.id

    expected = {
        "mail_channel_id": ch.json()["id"],
        "fallback_owner_group_id": fallback_owner_group_id,
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
async def test_fallback_owner_group_test_sends_to_selected_group(client, admin_token):
    ch = await client.post(
        "/admin/alerts/channels",
        json={
            "name": "mail-fallback-test",
            "webhook_url": WEBHOOK,
            "payload_template": '{"to":{{recipients_json}},"text":"{{message}}"}',
            "recipient_item_template": '{"email":"{{email}}"}',
            "headers": {"X-Test": "yes"},
        },
        headers=auth_header(admin_token),
    )
    assert ch.status_code == 201
    group = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "fallback-test-owners", "emails": ["ops@example.com"]},
        headers=auth_header(admin_token),
    )
    assert group.status_code == 201

    with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as send:
        resp = await client.post(
            "/admin/alerts/settings/fallback-owner-group/test",
            json={
                "mail_channel_id": ch.json()["id"],
                "fallback_owner_group_id": group.json()["id"],
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


@pytest.mark.asyncio
async def test_fallback_owner_group_test_requires_recipient_item_template(client, admin_token):
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
    group = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "fallback-no-template-owners", "emails": ["ops@example.com"]},
        headers=auth_header(admin_token),
    )
    assert group.status_code == 201

    with patch("app.routers.alerts.send_webhook", AsyncMock(return_value=(True, None))) as send:
        resp = await client.post(
            "/admin/alerts/settings/fallback-owner-group/test",
            json={
                "mail_channel_id": ch.json()["id"],
                "fallback_owner_group_id": group.json()["id"],
            },
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert "recipient_item_template" in resp.json()["error"]
    send.assert_not_awaited()


# ── Owner Groups ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_group_crud_deduplicates_emails(client, admin_token):
    create = await client.post(
        "/admin/alerts/owner-groups",
        json={
            "name": "database-team",
            "emails": [
                " dba@example.com ",
                "ops@example.com",
                "dba@example.com",
                "",
                " ops@example.com ",
            ],
            "enabled": True,
        },
        headers=auth_header(admin_token),
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["name"] == "database-team"
    assert body["emails"] == ["dba@example.com", "ops@example.com"]
    assert body["enabled"] is True
    group_id = body["id"]

    update = await client.put(
        f"/admin/alerts/owner-groups/{group_id}",
        json={
            "name": "primary-database-team",
            "emails": [
                "primary@example.com",
                "primary@example.com",
                " secondary@example.com ",
            ],
            "enabled": False,
        },
        headers=auth_header(admin_token),
    )
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["name"] == "primary-database-team"
    assert body["emails"] == ["primary@example.com", "secondary@example.com"]
    assert body["enabled"] is False

    list_resp = await client.get(
        "/admin/alerts/owner-groups",
        headers=auth_header(admin_token),
    )
    assert list_resp.status_code == 200
    assert any(
        group["id"] == group_id
        and group["name"] == "primary-database-team"
        and group["emails"] == ["primary@example.com", "secondary@example.com"]
        and group["enabled"] is False
        for group in list_resp.json()
    )

    delete = await client.delete(
        f"/admin/alerts/owner-groups/{group_id}",
        headers=auth_header(admin_token),
    )
    assert delete.status_code == 204


@pytest.mark.asyncio
async def test_owner_group_rejects_empty_email_list(client, admin_token):
    resp = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "empty-team", "emails": []},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422

    blank_resp = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "blank-team", "emails": [" ", ""]},
        headers=auth_header(admin_token),
    )
    assert blank_resp.status_code == 422


@pytest.mark.asyncio
async def test_owner_group_duplicate_name_returns_409(client, admin_token):
    first = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "duplicate-team", "emails": ["one@example.com"]},
        headers=auth_header(admin_token),
    )
    assert first.status_code == 201

    duplicate_create = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "duplicate-team", "emails": ["two@example.com"]},
        headers=auth_header(admin_token),
    )
    assert duplicate_create.status_code == 409

    second = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "rename-target-team", "emails": ["two@example.com"]},
        headers=auth_header(admin_token),
    )
    assert second.status_code == 201

    duplicate_update = await client.put(
        f"/admin/alerts/owner-groups/{second.json()['id']}",
        json={"name": "duplicate-team"},
        headers=auth_header(admin_token),
    )
    assert duplicate_update.status_code == 409


@pytest.mark.asyncio
async def test_owner_group_permissions_use_alert_read_and_write(client, admin_token, viewer_token):
    create = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "permission-team", "emails": ["owner@example.com"]},
        headers=auth_header(admin_token),
    )
    assert create.status_code == 201
    group_id = create.json()["id"]

    list_resp = await client.get(
        "/admin/alerts/owner-groups",
        headers=auth_header(viewer_token),
    )
    assert list_resp.status_code == 200

    create_denied = await client.post(
        "/admin/alerts/owner-groups",
        json={"name": "viewer-team", "emails": ["viewer@example.com"]},
        headers=auth_header(viewer_token),
    )
    assert create_denied.status_code == 403

    update_denied = await client.put(
        f"/admin/alerts/owner-groups/{group_id}",
        json={"enabled": False},
        headers=auth_header(viewer_token),
    )
    assert update_denied.status_code == 403

    delete_denied = await client.delete(
        f"/admin/alerts/owner-groups/{group_id}",
        headers=auth_header(viewer_token),
    )
    assert delete_denied.status_code == 403


@pytest.mark.asyncio
async def test_delete_fallback_owner_group_returns_409(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = OwnerGroup(name="fallback-team", emails='["fallback@example.com"]')
        db.add(group)
        await db.flush()
        settings = await db.get(AlertSettings, 1)
        if settings is None:
            settings = AlertSettings(
                id=1,
                route_error_threshold_pct=10.0,
                check_interval_seconds=60,
            )
            db.add(settings)
        settings.fallback_owner_group_id = group.id
        await db.commit()
        group_id = group.id

    resp = await client.delete(
        f"/admin/alerts/owner-groups/{group_id}",
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 409
    assert "fallback owner group" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_resource_owner_group_returns_409(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = OwnerGroup(name="resource-team", emails='["owner@example.com"]')
        db.add(group)
        await db.flush()
        db.add(ResourceOwner(
            resource_type="database",
            resource_id="analytics",
            owner_group_id=group.id,
        ))
        db.add(ResourceOwner(
            resource_type="database",
            resource_id="reporting",
            owner_group_id=group.id,
        ))
        await db.commit()
        group_id = group.id

    resp = await client.delete(
        f"/admin/alerts/owner-groups/{group_id}",
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 409
    assert "resource owner" in resp.json()["detail"]


# ── Resource Owners ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resource_owner_upsert_and_delete_for_db(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = OwnerGroup(name="orders-team", emails='["orders@example.com"]')
        db.add(group)
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
        await db.refresh(group)
        group_id = group.id

    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})
        put_resp = await client.put(
            "/admin/alerts/resource-owners/db/orders-db",
            json={"owner_group_id": group_id},
            headers=auth_header(admin_token),
        )

        assert put_resp.status_code == 200, put_resp.text
        body = put_resp.json()
        assert body["resource_type"] == "db"
        assert body["resource_id"] == "orders-db"
        assert body["display_name"] == "orders-db"
        assert body["owner_group_id"] == group_id
        assert body["owner_group_name"] == "orders-team"

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
            and row["owner_group_id"] == group_id
            and row["owner_group_name"] == "orders-team"
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
async def test_resource_owner_rejects_unknown_resource(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = OwnerGroup(name="missing-resource-team", emails='["missing@example.com"]')
        db.add(group)
        await db.commit()
        await db.refresh(group)
        group_id = group.id

    resp = await client.put(
        "/admin/alerts/resource-owners/db/missing-db",
        json={"owner_group_id": group_id},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_resource_owner_upsert_handles_concurrent_insert_conflict(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = OwnerGroup(name="race-team", emails='["race@example.com"]')
        db.add(group)
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
        await db.refresh(group)
        group_id = group.id

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
                    owner_group_id=group_id,
                ))
            raise IntegrityError("insert resource owner", {}, Exception("unique constraint"))
        await original_commit(session)

    with patch("app.routers.alerts.AsyncSession.commit", new=commit_with_concurrent_insert):
        resp = await client.put(
            "/admin/alerts/resource-owners/db/race-db",
            json={"owner_group_id": group_id},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["owner_group_id"] == group_id
    assert resp.json()["owner_group_name"] == "race-team"


@pytest.mark.asyncio
async def test_resource_owner_rejects_unsupported_resource_type(client, admin_token):
    resp = await client.put(
        "/admin/alerts/resource-owners/cache/redis",
        json={"owner_group_id": 1},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 422

    delete_resp = await client.delete(
        "/admin/alerts/resource-owners/cache/redis",
        headers=auth_header(admin_token),
    )

    assert delete_resp.status_code == 422


@pytest.mark.asyncio
async def test_resource_owner_rejects_missing_owner_group(client, admin_token, seeded_db):
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

    resp = await client.put(
        "/admin/alerts/resource-owners/s3/reports-bucket",
        json={"owner_group_id": 9999},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_resource_owner_validates_apisix_route_and_upstream(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = OwnerGroup(name="gateway-team", emails='["gateway@example.com"]')
        db.add(group)
        await db.commit()
        await db.refresh(group)
        group_id = group.id

    async def list_resources(resource: str) -> dict[str, object]:
        if resource == "routes":
            return {"items": [{"id": "orders-route", "name": "Orders Route"}]}
        if resource == "upstreams":
            return {"items": [{"id": "orders-upstream", "name": "Orders Upstream"}]}
        return {"items": []}

    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)

        route_resp = await client.put(
            "/admin/alerts/resource-owners/route/orders-route",
            json={"owner_group_id": group_id},
            headers=auth_header(admin_token),
        )
        upstream_resp = await client.put(
            "/admin/alerts/resource-owners/upstream/orders-upstream",
            json={"owner_group_id": group_id},
            headers=auth_header(admin_token),
        )
        missing_resp = await client.put(
            "/admin/alerts/resource-owners/route/missing-route",
            json={"owner_group_id": group_id},
            headers=auth_header(admin_token),
        )

    assert route_resp.status_code == 200, route_resp.text
    assert route_resp.json()["display_name"] == "Orders Route"
    assert route_resp.json()["owner_group_name"] == "gateway-team"
    assert upstream_resp.status_code == 200, upstream_resp.text
    assert upstream_resp.json()["display_name"] == "Orders Upstream"
    assert upstream_resp.json()["owner_group_name"] == "gateway-team"
    assert missing_resp.status_code == 422


@pytest.mark.asyncio
async def test_resource_owner_lists_apisix_resources_with_owner_mapping(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = OwnerGroup(name="route-team", emails='["route@example.com"]')
        db.add(group)
        await db.flush()
        db.add(ResourceOwner(
            resource_type="route",
            resource_id="orders-route",
            owner_group_id=group.id,
        ))
        await db.commit()
        group_id = group.id

    async def list_resources(resource: str) -> dict[str, object]:
        if resource == "routes":
            return {"items": [{"id": "orders-route", "name": "Orders Route"}]}
        if resource == "upstreams":
            return {"items": [{"id": "orders-upstream", "uri": "/orders"}]}
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
        and row["owner_group_id"] == group_id
        and row["owner_group_name"] == "route-team"
        for row in rows
    )
    assert any(
        row["resource_type"] == "upstream"
        and row["resource_id"] == "orders-upstream"
        and row["display_name"] == "/orders"
        and row["owner_group_id"] is None
        for row in rows
    )


@pytest.mark.asyncio
async def test_resource_owner_list_returns_503_when_apisix_fails(client, admin_token):
    with patch("app.routers.alerts.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=RuntimeError("apisix down"))
        resp = await client.get(
            "/admin/alerts/resource-owners",
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 503


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
async def test_update_rule_retarget_clears_old_rule_scoped_alert_state(
    client, admin_token, seeded_db,
):
    cid = await _create_channel(client, admin_token, "state-upd-ch")
    create = await client.post(
        "/admin/alerts/rules",
        json={
            "name": "route-state",
            "type": "route_error_rate",
            "target": "route-a",
            "threshold": 5.0,
            "channels": [{"channel_id": cid, "recipients": []}],
        },
        headers=auth_header(admin_token),
    )
    rid = create.json()["id"]
    state_target = f"route-a:rule_{rid}"
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertState(
            alert_type="route_error_rate",
            target=state_target,
            status="alert",
            display_target="checkout (route-a)",
            alert_notified=True,
        ))
        await db.commit()

    resp = await client.put(
        f"/admin/alerts/rules/{rid}",
        json={"target": "route-b"},
        headers=auth_header(admin_token),
    )

    assert resp.status_code == 200, resp.text
    async with session_factory() as db:
        rows = (await db.execute(
            select(AlertState).where(AlertState.target == state_target)
        )).scalars().all()
    assert rows == []


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
async def test_delete_rule_clears_rule_scoped_alert_state(client, admin_token, seeded_db):
    cid = await _create_channel(client, admin_token, "state-del-ch")
    create = await client.post(
        "/admin/alerts/rules",
        json={
            "name": "route-state-delete",
            "type": "route_error_rate",
            "target": "route-a",
            "threshold": 5.0,
            "channels": [{"channel_id": cid, "recipients": []}],
        },
        headers=auth_header(admin_token),
    )
    rid = create.json()["id"]
    state_target = f"route-a:rule_{rid}"
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertState(
            alert_type="route_error_rate",
            target=state_target,
            status="alert",
            display_target="checkout (route-a)",
            alert_notified=True,
        ))
        await db.commit()

    state = AlertStateManager()
    state.set_entry(
        "route_error_rate",
        state_target,
        status="alert",
        since="2026-05-07T00:00:00+00:00",
        display_target="checkout (route-a)",
        alert_notified=True,
    )
    alerts_router.set_alert_state(state)

    try:
        resp = await client.delete(
            f"/admin/alerts/rules/{rid}",
            headers=auth_header(admin_token),
        )

        assert resp.status_code == 204
        assert state.get_entries(alert_type="route_error_rate") == []
        async with session_factory() as db:
            rows = (await db.execute(
                select(AlertState).where(AlertState.target == state_target)
            )).scalars().all()
        assert rows == []
    finally:
        alerts_router.set_alert_state(None)


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
