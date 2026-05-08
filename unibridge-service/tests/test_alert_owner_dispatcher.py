"""Tests for owner-based alert dispatch service."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AlertChannel,
    AlertHistory,
    AlertSettings,
    OwnerGroup,
    ResourceOwner,
)
from app.services.alert_owner_dispatcher import dispatch_owner_alert


PAYLOAD_TEMPLATE = '{"recipients":{{recipients_json}},"body":"{{message}}"}'
RECIPIENT_TEMPLATE = '{"emailAddress":"{{email}}","recipientType":"TO"}'


async def _seed_mail_channel(db: AsyncSession, *, enabled: bool = True) -> AlertChannel:
    channel = AlertChannel(
        name="mail",
        webhook_url="https://hooks.example.com/mail",
        payload_template=PAYLOAD_TEMPLATE,
        recipient_item_template=RECIPIENT_TEMPLATE,
        enabled=enabled,
    )
    db.add(channel)
    await db.flush()
    return channel


async def _seed_owner_group(
    db: AsyncSession,
    *,
    name: str = "payment-owners",
    emails: list[str] | str = '["owner@example.com"]',
    enabled: bool = True,
) -> OwnerGroup:
    group = OwnerGroup(
        name=name,
        emails=json.dumps(emails) if isinstance(emails, list) else emails,
        enabled=enabled,
    )
    db.add(group)
    await db.flush()
    return group


async def _history_rows(session_factory) -> list[AlertHistory]:
    async with session_factory() as db:
        result = await db.execute(select(AlertHistory))
        return result.scalars().all()


@pytest.mark.asyncio
async def test_dispatch_owner_alert_uses_resource_owner_group(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        group = await _seed_owner_group(db, emails=["owner@example.com"])
        db.add(AlertSettings(id=1, mail_channel_id=channel.id))
        db.add(ResourceOwner(resource_type="db", resource_id="payment-db", owner_group_id=group.id))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            rule_id=None,
            display_target="payment-db",
        )

    send.assert_awaited_once()
    sent_payload = json.loads(send.await_args.kwargs["payload"])
    assert sent_payload["recipients"] == [
        {"emailAddress": "owner@example.com", "recipientType": "TO"}
    ]
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    history = histories[0]
    assert history.resource_type == "db"
    assert history.owner_group_id == group.id
    assert history.channel_id == channel.id
    assert json.loads(history.recipients) == ["owner@example.com"]
    assert history.success is True


@pytest.mark.asyncio
async def test_dispatch_owner_alert_uses_fallback_when_resource_owner_missing(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        fallback = await _seed_owner_group(
            db, name="fallback-owners", emails=["fallback@example.com"]
        )
        db.add(
            AlertSettings(
                id=1,
                mail_channel_id=channel.id,
                fallback_owner_group_id=fallback.id,
            )
        )
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            rule_id=None,
            display_target="payment-db",
        )

    sent_payload = json.loads(send.await_args.kwargs["payload"])
    assert sent_payload["recipients"] == [
        {"emailAddress": "fallback@example.com", "recipientType": "TO"}
    ]
    histories = await _history_rows(session_factory)
    assert histories[0].owner_group_id == fallback.id
    assert json.loads(histories[0].recipients) == ["fallback@example.com"]


@pytest.mark.asyncio
async def test_dispatch_owner_alert_skips_disabled_resource_group_and_uses_fallback(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        disabled = await _seed_owner_group(
            db, name="disabled-owners", emails=["disabled@example.com"], enabled=False
        )
        fallback = await _seed_owner_group(
            db, name="fallback-owners", emails=["fallback@example.com"]
        )
        db.add(
            AlertSettings(
                id=1,
                mail_channel_id=channel.id,
                fallback_owner_group_id=fallback.id,
            )
        )
        db.add(ResourceOwner(resource_type="db", resource_id="payment-db", owner_group_id=disabled.id))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            rule_id=None,
            display_target="payment-db",
        )

    sent_payload = json.loads(send.await_args.kwargs["payload"])
    assert sent_payload["recipients"] == [
        {"emailAddress": "fallback@example.com", "recipientType": "TO"}
    ]
    histories = await _history_rows(session_factory)
    assert histories[0].owner_group_id == fallback.id


@pytest.mark.asyncio
async def test_dispatch_owner_alert_records_failure_without_owner_or_fallback(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            rule_id=None,
            display_target="payment-db",
        )

    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].success is False
    assert "No owner group" in histories[0].error_detail


@pytest.mark.asyncio
async def test_dispatch_owner_alert_records_failure_without_mail_channel(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        group = await _seed_owner_group(db)
        db.add(AlertSettings(id=1, fallback_owner_group_id=group.id))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            rule_id=None,
            display_target="payment-db",
        )

    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].channel_id is None
    assert histories[0].owner_group_id is None
    assert histories[0].success is False
    assert "Mail channel not configured" in histories[0].error_detail


@pytest.mark.asyncio
async def test_dispatch_owner_alert_records_template_error(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = AlertChannel(
            name="mail",
            webhook_url="https://hooks.example.com/mail",
            payload_template=PAYLOAD_TEMPLATE,
            recipient_item_template='{"recipientType":"TO"}',
        )
        db.add(channel)
        await db.flush()
        group = await _seed_owner_group(db, emails=["owner@example.com"])
        db.add(AlertSettings(id=1, mail_channel_id=channel.id))
        db.add(ResourceOwner(resource_type="db", resource_id="payment-db", owner_group_id=group.id))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            rule_id=None,
            display_target="payment-db",
        )

    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].success is False
    assert "recipient_item_template" in histories[0].error_detail
    assert json.loads(histories[0].recipients) == ["owner@example.com"]
