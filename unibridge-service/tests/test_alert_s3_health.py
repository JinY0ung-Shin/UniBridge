"""The ``s3_health`` alert rule.

Mirrors ``db_health``/``nas_health``: the same N-failure debounce, the same
recovery notification, and recipients resolved through ``ResourceOwner`` type
``s3`` plus the global admins.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models import AlertChannel, AlertHistory, AlertSettings, AlertState, ResourceOwner
from app.services import alert_checker
from app.services.alert_owner_dispatcher import dispatch_alert
from app.services.alert_state import AlertStateManager, purge_stale_states

PAYLOAD_TEMPLATE = '{"recipients":{{recipients_json}},"body":"{{message}}"}'
RECIPIENT_TEMPLATE = '{"emailAddress":"{{email}}","recipientType":"TO"}'


def _silence_other_probes():
    return (
        patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._check_nas_health", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._persist_state_safely", new_callable=AsyncMock),
    )


async def _run_s3_cycle(state, results, *, trigger_after_failures=2):
    db_p, nas_p, up_p, route_p, persist_p = _silence_other_probes()
    with db_p, nas_p, up_p, route_p, persist_p, \
         patch("app.services.alert_checker._check_s3_health", new_callable=AsyncMock) as probe, \
         patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as dispatch:
        probe.return_value = results
        await alert_checker.run_single_check(
            state, trigger_after_failures=trigger_after_failures,
        )
    return dispatch


# ── Probe ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_s3_health_reports_every_registered_alias():
    manager = AsyncMock()
    manager.list_aliases = lambda: ["good", "bad"]
    manager.test_connection = AsyncMock(
        side_effect=[(True, "Connection successful"), (False, "Connection failed")]
    )
    with patch("app.services.s3_manager.s3_manager", manager):
        assert await alert_checker._check_s3_health() == [("good", True), ("bad", False)]


@pytest.mark.asyncio
async def test_check_s3_health_treats_a_raising_probe_as_unhealthy():
    manager = AsyncMock()
    manager.list_aliases = lambda: ["boom"]
    manager.test_connection = AsyncMock(side_effect=RuntimeError("client gone"))
    with patch("app.services.s3_manager.s3_manager", manager):
        assert await alert_checker._check_s3_health() == [("boom", False)]


@pytest.mark.asyncio
async def test_check_s3_health_with_no_connections():
    manager = AsyncMock()
    manager.list_aliases = lambda: []
    with patch("app.services.s3_manager.s3_manager", manager):
        assert await alert_checker._check_s3_health() == []


# ── Rule behaviour ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s3_health_debounces_until_the_failure_threshold():
    state = AlertStateManager()

    dispatch = await _run_s3_cycle(state, [("archive", False)])
    dispatch.assert_not_awaited()
    assert state.get_status("s3_health", "archive") == "ok"

    dispatch = await _run_s3_cycle(state, [("archive", False)])
    dispatch.assert_awaited_once()
    assert state.get_status("s3_health", "archive") == "alert"

    kwargs = dispatch.await_args.kwargs
    assert kwargs["resource_type"] == "s3"
    assert kwargs["resource_id"] == "archive"
    assert kwargs["rule_type"] == "s3_health"
    assert kwargs["alert_type"] == "triggered"
    assert kwargs["target"] == "archive"
    assert kwargs["display_target"] == "archive"
    assert kwargs["message"] == "S3 connection 'archive' is unavailable."
    assert kwargs["monitor_label"] == "S3 연결 상태"


@pytest.mark.asyncio
async def test_s3_health_recovery_notifies():
    state = AlertStateManager()
    await _run_s3_cycle(state, [("archive", False)])
    await _run_s3_cycle(state, [("archive", False)])

    dispatch = await _run_s3_cycle(state, [("archive", True)])

    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["alert_type"] == "resolved"
    assert kwargs["rule_type"] == "s3_health"
    assert kwargs["message"] == "S3 connection 'archive' restored."
    assert state.get_status("s3_health", "archive") == "ok"


@pytest.mark.asyncio
async def test_s3_health_respects_the_trigger_after_failures_setting():
    state = AlertStateManager()
    for _ in range(2):
        dispatch = await _run_s3_cycle(state, [("archive", False)], trigger_after_failures=3)
        dispatch.assert_not_awaited()
    dispatch = await _run_s3_cycle(state, [("archive", False)], trigger_after_failures=3)
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_healthy_s3_never_notifies():
    state = AlertStateManager()
    dispatch = await _run_s3_cycle(state, [("archive", True)])
    dispatch.assert_not_awaited()
    assert state.get_status("s3_health", "archive") == "ok"


# ── Recipients ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s3_alert_reaches_the_s3_assignees_and_admins(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = AlertChannel(
            name="mail",
            webhook_url="https://hooks.example.com/mail",
            payload_template=PAYLOAD_TEMPLATE,
            recipient_item_template=RECIPIENT_TEMPLATE,
        )
        db.add(channel)
        await db.flush()
        db.add(AlertSettings(
            id=1, mail_channel_id=channel.id,
            admin_emails=json.dumps(["admin@example.com"]),
        ))
        db.add(ResourceOwner(
            resource_type="s3",
            resource_id="archive",
            emails=json.dumps(["storage@example.com"]),
        ))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="s3",
            resource_id="archive",
            alert_type="triggered",
            rule_type="s3_health",
            target="archive",
            message="S3 connection 'archive' is unavailable.",
            display_target="archive",
            monitor_label="S3 연결 상태",
        )

    send.assert_awaited_once()
    payload = json.loads(send.await_args.kwargs["payload"])
    assert payload["recipients"] == [
        {"emailAddress": "storage@example.com", "recipientType": "TO"},
        {"emailAddress": "admin@example.com", "recipientType": "TO"},
    ]
    async with session_factory() as db:
        history = (await db.execute(select(AlertHistory))).scalars().one()
    assert history.resource_type == "s3"
    assert history.rule_type == "s3_health"
    assert history.alert_type == "triggered"


@pytest.mark.asyncio
async def test_s3_alerts_can_be_switched_off_per_connection(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        channel = AlertChannel(
            name="mail",
            webhook_url="https://hooks.example.com/mail",
            payload_template=PAYLOAD_TEMPLATE,
            recipient_item_template=RECIPIENT_TEMPLATE,
        )
        db.add(channel)
        await db.flush()
        db.add(AlertSettings(
            id=1, mail_channel_id=channel.id,
            admin_emails=json.dumps(["admin@example.com"]),
        ))
        db.add(ResourceOwner(
            resource_type="s3", resource_id="archive",
            emails=json.dumps(["storage@example.com"]), alerts_enabled=False,
        ))
        await db.commit()

    send = AsyncMock(return_value=(True, None))
    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook", send):
        await dispatch_alert(
            resource_type="s3", resource_id="archive", alert_type="triggered",
            rule_type="s3_health", target="archive", message="down",
        )

    send.assert_not_awaited()


# ── Stale-state purge ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_drops_s3_state_for_a_removed_alias(db_session):
    state = AlertStateManager()
    for alias in ("kept", "gone"):
        db_session.add(AlertState(alert_type="s3_health", target=alias, status="ok"))
        state.set_entry("s3_health", alias, status="ok", since="2026-01-01T00:00:00+00:00")
    await db_session.commit()

    removed = await purge_stale_states(
        db_session, state,
        known_db_aliases=set(), known_nas_aliases=set(),
        known_upstream_ids=None, known_route_ids=None,
        known_s3_aliases={"kept"},
    )

    assert removed == [("s3_health", "gone")]
    remaining = (await db_session.execute(select(AlertState.target))).scalars().all()
    assert remaining == ["kept"]


@pytest.mark.asyncio
async def test_purge_skips_s3_when_the_registry_is_unavailable(db_session):
    state = AlertStateManager()
    db_session.add(AlertState(alert_type="s3_health", target="archive", status="alert"))
    state.set_entry("s3_health", "archive", status="alert", since="2026-01-01T00:00:00+00:00")
    await db_session.commit()

    removed = await purge_stale_states(
        db_session, state,
        known_db_aliases=set(), known_nas_aliases=set(),
        known_upstream_ids=None, known_route_ids=None,
        known_s3_aliases=None,
    )

    assert removed == []
    assert state.get_status("s3_health", "archive") == "alert"
