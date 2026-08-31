"""Tests for the assignee/admin alert dispatch service."""
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
    ResourceOwner,
)
from app.services.alert_owner_dispatcher import dispatch_alert


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


async def _seed_resource_owner(
    db: AsyncSession,
    *,
    resource_type: str = "db",
    resource_id: str = "payment-db",
    emails: list[str] | str = '["owner@example.com"]',
) -> ResourceOwner:
    owner = ResourceOwner(
        resource_type=resource_type,
        resource_id=resource_id,
        emails=json.dumps(emails) if isinstance(emails, list) else emails,
    )
    db.add(owner)
    await db.flush()
    return owner


async def _history_rows(session_factory) -> list[AlertHistory]:
    async with session_factory() as db:
        result = await db.execute(select(AlertHistory))
        return result.scalars().all()


@pytest.mark.asyncio
async def test_dispatch_alert_sends_to_resource_assignees(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(db, emails=["owner@example.com"])
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
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
    assert history.channel_id == channel.id
    assert json.loads(history.recipients) == ["owner@example.com"]
    assert history.success is True


@pytest.mark.asyncio
async def test_dispatch_alert_unions_assignees_with_admins_and_dedupes(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        # admins overlap with one assignee (case-insensitive) and add a new one.
        db.add(AlertSettings(
            id=1,
            mail_channel_id=channel.id,
            admin_emails=json.dumps(["OWNER@example.com", "admin@example.com"]),
        ))
        await _seed_resource_owner(db, emails=["owner@example.com", "dba@example.com"])
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
        )

    send.assert_awaited_once()
    histories = await _history_rows(session_factory)
    # Assignees first, then admins, case-insensitive dedupe (first occurrence wins).
    assert json.loads(histories[0].recipients) == [
        "owner@example.com",
        "dba@example.com",
        "admin@example.com",
    ]
    assert histories[0].success is True


@pytest.mark.asyncio
async def test_dispatch_alert_sends_to_admins_when_no_assignees(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(
            id=1,
            mail_channel_id=channel.id,
            admin_emails=json.dumps(["admin@example.com"]),
        ))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            display_target="payment-db",
        )

    send.assert_awaited_once()
    sent_payload = json.loads(send.await_args.kwargs["payload"])
    assert sent_payload["recipients"] == [
        {"emailAddress": "admin@example.com", "recipientType": "TO"}
    ]
    histories = await _history_rows(session_factory)
    assert json.loads(histories[0].recipients) == ["admin@example.com"]
    assert histories[0].success is True


@pytest.mark.asyncio
async def test_dispatch_alert_skips_resource_when_alerts_disabled(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(
            id=1,
            mail_channel_id=channel.id,
            admin_emails=json.dumps(["admin@example.com"]),
        ))
        await _seed_resource_owner(db, emails=["owner@example.com"])
        owner = (await db.execute(select(ResourceOwner))).scalar_one()
        owner.alerts_enabled = False
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
        )

    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].success is None
    assert histories[0].recipients is None
    assert histories[0].error_detail == "Alerts disabled for resource"


@pytest.mark.asyncio
async def test_dispatch_alert_ignores_upstream_assignees_and_sends_to_admins(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(
            id=1,
            mail_channel_id=channel.id,
            admin_emails=json.dumps(["admin@example.com"]),
        ))
        await _seed_resource_owner(
            db,
            resource_type="upstream",
            resource_id="orders-upstream",
            emails=["owner@example.com"],
        )
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="upstream",
            resource_id="orders-upstream",
            alert_type="triggered",
            target="orders-upstream",
            message="Upstream failed",
        )

    send.assert_awaited_once()
    histories = await _history_rows(session_factory)
    assert json.loads(histories[0].recipients) == ["admin@example.com"]
    assert histories[0].success is True


@pytest.mark.asyncio
async def test_dispatch_alert_adds_referenced_assignees_in_ref_order(engine):
    """An upstream alert reaches the assignees of the routes that reference it.

    Upstreams have no assignees of their own, so the checker passes the
    referencing routes as ``assignee_refs``; those addresses sit between the
    primary resource's assignees and the admins, deduped case-insensitively.
    """
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(
            id=1,
            mail_channel_id=channel.id,
            admin_emails=json.dumps(["admin@x.com", "SHARED@x.com"]),
        ))
        await _seed_resource_owner(
            db, resource_type="route", resource_id="r1",
            emails=["a@x.com", "shared@x.com"],
        )
        await _seed_resource_owner(
            db, resource_type="route", resource_id="r2", emails=["b@x.com"],
        )
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        result = await dispatch_alert(
            resource_type="upstream",
            resource_id="orders-upstream",
            alert_type="triggered",
            target="orders-upstream",
            message="Upstream failed",
            assignee_refs=[("route", "r1"), ("route", "r2")],
        )

    assert result is True
    expected = ["a@x.com", "shared@x.com", "b@x.com", "admin@x.com"]
    sent_payload = json.loads(send.await_args.kwargs["payload"])
    assert sent_payload["recipients"] == [
        {"emailAddress": email, "recipientType": "TO"} for email in expected
    ]
    histories = await _history_rows(session_factory)
    assert json.loads(histories[0].recipients) == expected


@pytest.mark.asyncio
async def test_dispatch_alert_tolerates_broken_referenced_assignees(engine):
    """A ref that is off, missing or corrupt drops itself, not the alert.

    Unlike the primary resource, a referenced one is a courtesy recipient: it
    can neither suppress the alert (``alerts_enabled=False``) nor fail it
    (unparseable emails), because the admins must still be told.
    """
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(
            id=1,
            mail_channel_id=channel.id,
            admin_emails=json.dumps(["admin@x.com"]),
        ))
        muted = await _seed_resource_owner(
            db, resource_type="route", resource_id="r1", emails=["muted@x.com"],
        )
        muted.alerts_enabled = False
        # r2 deliberately has no ResourceOwner row at all.
        await _seed_resource_owner(
            db, resource_type="route", resource_id="r3", emails="not-json",
        )
        await _seed_resource_owner(
            db, resource_type="route", resource_id="r4", emails=["r4@x.com"],
        )
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        result = await dispatch_alert(
            resource_type="upstream",
            resource_id="orders-upstream",
            alert_type="triggered",
            target="orders-upstream",
            message="Upstream failed",
            assignee_refs=[
                ("route", "r1"), ("route", "r2"), ("route", "r3"), ("route", "r4"),
            ],
        )

    assert result is True
    send.assert_awaited_once()
    histories = await _history_rows(session_factory)
    assert json.loads(histories[0].recipients) == ["r4@x.com", "admin@x.com"]
    assert histories[0].success is True


@pytest.mark.asyncio
async def test_dispatch_alert_records_dispatch_metric(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(
            db, resource_type="route", resource_id="checkout", emails=["owner@example.com"]
        )
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send), \
         patch("app.metrics.record_alert_dispatch") as record_metric:
        await dispatch_alert(
            resource_type="route",
            resource_id="checkout",
            alert_type="triggered",
            target="checkout",
            message="Route error rate exceeded",
        )

    record_metric.assert_called_once_with(
        rule_id="route",
        channel_type="webhook",
        status="success",
    )


@pytest.mark.asyncio
async def test_dispatch_alert_skips_when_no_assignees_or_admins(engine):
    """No recipients is a skip (success None / returns None), not a delivery
    failure — the caller must not retry it and it must not count as a failure."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        result = await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            display_target="payment-db",
        )

    assert result is None
    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].success is None
    assert "No assignees or admins" in histories[0].error_detail


@pytest.mark.asyncio
async def test_dispatch_alert_skips_when_no_mail_channel(engine):
    """No configured channel is a skip (success None / returns None): an
    unconfigured deployment must not be treated as a dispatch failure."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertSettings(id=1, admin_emails=json.dumps(["admin@example.com"])))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        result = await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            display_target="payment-db",
        )

    assert result is None
    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].channel_id is None
    assert histories[0].success is None
    assert "Mail channel not configured" in histories[0].error_detail


@pytest.mark.asyncio
async def test_dispatch_alert_records_template_error(engine):
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
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(db, emails=["owner@example.com"])
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
            display_target="payment-db",
        )

    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].success is False
    assert "recipient_item_template" in histories[0].error_detail
    assert json.loads(histories[0].recipients) == ["owner@example.com"]


@pytest.mark.asyncio
async def test_dispatch_alert_skips_for_disabled_mail_channel(engine):
    """A disabled channel means alerts were intentionally switched off — a skip
    (success None), not a delivery failure that should retry or trip the
    meta-alert."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db, enabled=False)
        db.add(AlertSettings(
            id=1,
            mail_channel_id=channel.id,
            admin_emails=json.dumps(["admin@example.com"]),
        ))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        result = await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
        )

    assert result is None
    send.assert_not_awaited()
    histories = await _history_rows(session_factory)
    assert len(histories) == 1
    assert histories[0].channel_id == channel.id
    assert histories[0].success is None
    assert "disabled" in histories[0].error_detail


@pytest.mark.asyncio
async def test_dispatch_alert_sends_headers_and_alert_placeholders(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    payload_template = (
        '{"recipients":{{recipients_json}},"to":"{{recipients}}",'
        '"status":"{{status}}","target":"{{target_name}}",'
        '"rate":"{{rate}}","threshold":"{{threshold}}","rule":"{{rule_name}}",'
        '"description":"{{target_description}}"}'
    )
    async with session_factory() as db:
        channel = await _seed_mail_channel(
            db,
            payload_template=payload_template,
            headers='{"X-Token":"abc","Retry":2}',
        )
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(
            db, resource_type="route", resource_id="checkout", emails=["owner@example.com"]
        )
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="route",
            resource_id="checkout",
            alert_type="resolved",
            target="checkout",
            message="Route recovered",
            display_target="Checkout API",
            rate=12.34,
            threshold=10.0,
            monitor_label="라우트 에러율",
            target_description="Handles checkout traffic",
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
    assert payload["rule"] == "라우트 에러율"
    assert payload["description"] == "Handles checkout traffic"
    histories = await _history_rows(session_factory)
    assert histories[0].success is True


@pytest.mark.asyncio
async def test_dispatch_alert_labels_a_scheduled_report_as_such(engine):
    """A "report" must not borrow the recovery wording — it announces no incident."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(
            db, payload_template='{"recipients":{{recipients_json}},"status":"{{status}}"}'
        )
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails='["ops@example.com"]'))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="server",
            resource_id="gpu1",
            alert_type="report",
            rule_type="server_gpu_underutil",
            target="gpu1",
            message="Server 'gpu1' 24h average GPU utilisation is 4.0%.",
            display_target="gpu1",
        )

    assert json.loads(send.await_args.kwargs["payload"])["status"] == "정기 리포트"
    histories = await _history_rows(session_factory)
    assert (histories[0].alert_type, histories[0].rule_type) == ("report", "server_gpu_underutil")


@pytest.mark.asyncio
async def test_dispatch_alert_requires_recipient_item_template(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db, recipient_item_template=None)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(db, emails=["owner@example.com"])
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
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
async def test_dispatch_alert_records_invalid_assignee_email_json(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(db, emails='{"bad":"shape"}')
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
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
async def test_dispatch_alert_records_webhook_failure_and_exception(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(db, emails=["owner@example.com"])
        await db.commit()

    send = AsyncMock(side_effect=[(False, "timeout"), RuntimeError("network down")])
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        # A configured channel that fails to deliver returns False so the caller
        # can re-arm and retry — for both a returned error and a raised one.
        failed_send = await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
        )
        raised_send = await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="resolved",
            target="payment-db",
            message="Database recovered",
        )

    assert failed_send is False
    assert raised_send is False
    histories = await _history_rows(session_factory)
    assert [history.success for history in histories] == [False, False]
    assert histories[0].error_detail == "timeout"
    assert histories[1].error_detail == "network down"


@pytest.mark.asyncio
async def test_dispatch_alert_return_value_is_tristate(engine):
    """The return mirrors AlertHistory.success: True sent, None skipped, False failed."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(db, emails=["owner@example.com"])
        # A second resource whose alerts are switched off, to exercise the skip.
        await _seed_resource_owner(
            db, resource_type="nas", resource_id="reports",
            emails=["ops@example.com"],
        )
        owner = (
            await db.execute(
                select(ResourceOwner).where(ResourceOwner.resource_type == "nas")
            )
        ).scalar_one()
        owner.alerts_enabled = False
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        sent = await dispatch_alert(
            resource_type="db", resource_id="payment-db",
            alert_type="triggered", target="payment-db", message="down",
        )
        skipped = await dispatch_alert(
            resource_type="nas", resource_id="reports",
            alert_type="triggered", target="reports", message="down",
        )

    assert sent is True
    assert skipped is None


@pytest.mark.asyncio
async def test_dispatch_alert_releases_read_session_before_webhook(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(db, emails=["owner@example.com"])
        await db.commit()

    tracking_session_factory = _TrackingSessionFactory(session_factory)

    async def send_with_session_assertion(**_kwargs):
        assert tracking_session_factory.active_sessions == 0
        return True, None

    with patch("app.services.alert_owner_dispatcher.async_session", tracking_session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send_with_session_assertion):
        await dispatch_alert(
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
async def test_dispatch_alert_does_not_raise_when_history_record_fails(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = await _seed_mail_channel(db)
        db.add(AlertSettings(id=1, mail_channel_id=channel.id, admin_emails="[]"))
        await _seed_resource_owner(db, emails=["owner@example.com"])
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    failing_session_factory = _FailingHistorySessionFactory(session_factory)
    with patch("app.services.alert_owner_dispatcher.async_session", failing_session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="db",
            resource_id="payment-db",
            alert_type="triggered",
            target="payment-db",
            message="Database failed",
        )

    send.assert_awaited_once()
    assert failing_session_factory.calls == 2
