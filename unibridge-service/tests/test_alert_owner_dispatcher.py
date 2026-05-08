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


class _TrackingSessionContext:
    def __init__(self, context, tracker):
        self._context = context
        self._tracker = tracker

    async def __aenter__(self):
        self._tracker.active_sessions += 1
        return await self._context.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._context.__aexit__(exc_type, exc, tb)
        finally:
            self._tracker.active_sessions -= 1


class _TrackingSessionFactory:
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.active_sessions = 0

    def __call__(self):
        return _TrackingSessionContext(self._session_factory(), self)


class _FailingHistoryDb:
    def add(self, _entry):
        return None

    async def commit(self):
        raise RuntimeError("history insert failed")


class _FailingHistoryContext:
    async def __aenter__(self):
        return _FailingHistoryDb()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FailingHistorySessionFactory:
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == 1:
            return self._session_factory()
        return _FailingHistoryContext()


async def _seed_mail_channel(
    db: AsyncSession,
    *,
    enabled: bool = True,
    payload_template: str = PAYLOAD_TEMPLATE,
    recipient_item_template: str | None = RECIPIENT_TEMPLATE,
    headers: str | None = None,
) -> AlertChannel:
    channel = AlertChannel(
        name="mail",
        webhook_url="https://hooks.example.com/mail",
        payload_template=payload_template,
        recipient_item_template=recipient_item_template,
        headers=headers,
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
async def test_dispatch_owner_alert_records_dispatch_metric(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        group = await _seed_owner_group(db, emails=["owner@example.com"])
        db.add(AlertSettings(id=1, mail_channel_id=channel.id))
        db.add(ResourceOwner(resource_type="route", resource_id="checkout", owner_group_id=group.id))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send), \
         patch("app.metrics.record_alert_dispatch") as record_metric:
        await dispatch_owner_alert(
            resource_type="route",
            resource_id="checkout",
            alert_type="triggered",
            target="checkout",
            message="Route error rate exceeded",
            rule_id=42,
        )

    record_metric.assert_called_once_with(
        rule_id=42,
        channel_type="webhook",
        status="success",
    )


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


@pytest.mark.asyncio
async def test_dispatch_owner_alert_records_failure_for_disabled_mail_channel(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db, enabled=False)
        group = await _seed_owner_group(db, emails=["owner@example.com"])
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, fallback_owner_group_id=group.id))
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
        )

    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].channel_id == channel.id
    assert histories[0].success is False
    assert "disabled" in histories[0].error_detail


@pytest.mark.asyncio
async def test_dispatch_owner_alert_sends_headers_and_alert_placeholders(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    payload_template = (
        '{"recipients":{{recipients_json}},"to":"{{recipients}}",'
        '"status":"{{status}}","target":"{{target_name}}",'
        '"rate":"{{rate}}","threshold":"{{threshold}}","rule":"{{rule_name}}"}'
    )
    async with session_factory() as db:
        channel = await _seed_mail_channel(
            db,
            payload_template=payload_template,
            headers='{"X-Token":"abc","Retry":2}',
        )
        group = await _seed_owner_group(db, emails=["owner@example.com"])
        db.add(AlertSettings(id=1, mail_channel_id=channel.id))
        db.add(ResourceOwner(resource_type="route", resource_id="checkout", owner_group_id=group.id))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="route",
            resource_id="checkout",
            alert_type="resolved",
            target="checkout",
            message="Route recovered",
            rule_id=42,
            display_target="Checkout API",
            rate=12.34,
            threshold=10.0,
            rule_name="checkout 5xx",
        )

    sent = send.await_args.kwargs
    assert sent["headers"] == {"X-Token": "abc", "Retry": "2"}
    payload = json.loads(sent["payload"])
    assert payload["recipients"] == [{"emailAddress": "owner@example.com", "recipientType": "TO"}]
    assert payload["to"] == "owner@example.com"
    assert payload["status"] == "정상 복구"
    assert payload["target"] == "Checkout API"
    assert payload["rate"] == "12.3"
    assert payload["threshold"] == "10.0"
    assert payload["rule"] == "checkout 5xx"
    histories = await _history_rows(session_factory)
    assert histories[0].rule_id == 42
    assert histories[0].success is True


@pytest.mark.asyncio
async def test_dispatch_owner_alert_requires_recipient_item_template(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db, recipient_item_template=None)
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
        )

    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert histories[0].success is False
    assert "recipient_item_template" in histories[0].error_detail


@pytest.mark.asyncio
async def test_dispatch_owner_alert_records_invalid_owner_email_json(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        group = await _seed_owner_group(db, emails='{"bad":"shape"}')
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
        )

    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert histories[0].success is False
    assert histories[0].recipients is None
    assert "JSON array of strings" in histories[0].error_detail


@pytest.mark.asyncio
async def test_dispatch_owner_alert_records_webhook_failure_and_exception(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        group = await _seed_owner_group(db, emails=["owner@example.com"])
        db.add(AlertSettings(id=1, mail_channel_id=channel.id))
        db.add(ResourceOwner(resource_type="db", resource_id="payment-db", owner_group_id=group.id))
        await db.commit()

    send = AsyncMock(side_effect=[(False, "timeout"), RuntimeError("network down")])
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
        )
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="resolved",
            target="payment-db",
            message="Database recovered",
        )

    histories = await _history_rows(session_factory)
    assert [history.success for history in histories] == [False, False]
    assert histories[0].error_detail == "timeout"
    assert histories[1].error_detail == "network down"


@pytest.mark.asyncio
async def test_dispatch_owner_alert_releases_read_session_before_webhook(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        group = await _seed_owner_group(db, emails=["owner@example.com"])
        db.add(AlertSettings(id=1, mail_channel_id=channel.id))
        db.add(ResourceOwner(resource_type="db", resource_id="payment-db", owner_group_id=group.id))
        await db.commit()

    tracking_session_factory = _TrackingSessionFactory(session_factory)

    async def send_with_session_assertion(**_kwargs):
        assert tracking_session_factory.active_sessions == 0
        return True, None

    with patch("app.services.alert_owner_dispatcher.async_session", tracking_session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send_with_session_assertion):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
        )

    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].success is True


@pytest.mark.asyncio
async def test_dispatch_owner_alert_does_not_raise_when_history_record_fails(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        group = await _seed_owner_group(db, emails=["owner@example.com"])
        db.add(AlertSettings(id=1, mail_channel_id=channel.id))
        db.add(ResourceOwner(resource_type="db", resource_id="payment-db", owner_group_id=group.id))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    failing_session_factory = _FailingHistorySessionFactory(session_factory)
    with patch("app.services.alert_owner_dispatcher.async_session", failing_session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_owner_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
        )

    send.assert_awaited_once()
    assert failing_session_factory.calls == 2
