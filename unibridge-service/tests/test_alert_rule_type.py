"""``rule_type`` attribution on alert history.

``alert_history.alert_type`` has always stored the *transition*
("triggered"/"resolved"); ``rule_type`` records which monitoring rule produced
the row. Both are asserted here so the split cannot silently regress.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models import AlertChannel, AlertHistory, AlertSettings
from app.services import alert_checker
from app.services.alert_owner_dispatcher import dispatch_alert
from app.services.alert_state import AlertStateManager
from app.services.server_monitor import HostSignal, ServiceSignal
from tests.conftest import auth_header

PAYLOAD_TEMPLATE = '{"recipients":{{recipients_json}},"body":"{{message}}"}'
RECIPIENT_TEMPLATE = '{"emailAddress":"{{email}}","recipientType":"TO"}'


def _probe_patches(**overrides):
    """Patch every checker probe to return nothing unless overridden."""
    names = {
        "_check_db_health": [],
        "_check_s3_health": [],
        "_check_nas_health": [],
        "_check_upstream_health": [],
        "_check_route_error_rate": [],
    }
    names.update(overrides)
    return [
        patch(f"app.services.alert_checker.{name}", new_callable=AsyncMock, return_value=value)
        for name, value in names.items()
    ]


async def _run_cycle(state, *, trigger_after_failures=1, **overrides):
    patches = _probe_patches(**overrides)
    with patch("app.services.alert_checker._persist_state_safely", new_callable=AsyncMock), \
         patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as dispatch:
        for p in patches:
            p.start()
        try:
            await alert_checker.run_single_check(
                state, trigger_after_failures=trigger_after_failures,
            )
        finally:
            for p in patches:
                p.stop()
    return dispatch


# ── The checker tags every dispatch with its rule ────────────────────────────


@pytest.mark.parametrize(
    "probe,results,expected_rule,expected_resource_type",
    [
        ("_check_db_health", [("mydb", False, None)], "db_health", "db"),
        ("_check_s3_health", [("archive", False)], "s3_health", "s3"),
        ("_check_nas_health", [("reports", False, None)], "nas_health", "nas"),
        (
            "_check_upstream_health",
            [("up-1", False, "unreachable")],
            "upstream_health",
            "upstream",
        ),
    ],
)
@pytest.mark.asyncio
async def test_connection_rules_tag_their_dispatch(
    probe, results, expected_rule, expected_resource_type
):
    state = AlertStateManager()
    dispatch = await _run_cycle(state, **{probe: results})

    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["rule_type"] == expected_rule
    assert kwargs["resource_type"] == expected_resource_type
    assert kwargs["alert_type"] == "triggered"


@pytest.mark.asyncio
async def test_route_error_rate_tags_its_dispatch():
    state = AlertStateManager()
    settings = AsyncMock(return_value=(10.0, 0))
    with patch("app.services.alert_checker._load_route_error_settings", settings):
        dispatch = await _run_cycle(
            state, _check_route_error_rate=[("route-1", 50.0, 100.0)]
        )

    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["rule_type"] == "route_error_rate"
    assert kwargs["resource_type"] == "route"


@pytest.mark.asyncio
async def test_server_signal_tags_its_own_alert_type_as_the_rule(monkeypatch):
    signal = HostSignal(
        alert_type="server_disk",
        target="host-a",
        display="Host A",
        is_healthy=False,
        severity="critical",
        value=95.0,
        threshold=90.0,
        message="Disk is full",
        monitor_label="디스크 사용률",
    )
    monkeypatch.setattr(
        alert_checker, "_load_server_monitoring",
        AsyncMock(return_value=([type("H", (), {"enabled": True})()], alert_checker.ServerThresholds(), 0)),
    )
    monkeypatch.setattr(
        alert_checker.server_monitor, "evaluate_hosts", AsyncMock(return_value=[signal])
    )
    state = AlertStateManager()
    dispatch = await _run_cycle(state)

    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["rule_type"] == "server_disk"
    assert kwargs["resource_type"] == "server"


@pytest.mark.asyncio
async def test_external_service_signal_tags_its_rule(monkeypatch):
    signal = ServiceSignal(
        alert_type="external_service_down",
        target="orders",
        display="orders",
        is_healthy=False,
        severity="critical",
        message="orders is unreachable",
        monitor_label="외부 서비스",
    )
    monkeypatch.setattr(
        alert_checker, "_load_service_monitoring",
        AsyncMock(return_value=([type("S", (), {"enabled": True})()], 0)),
    )
    monkeypatch.setattr(
        alert_checker.server_monitor, "evaluate_services", AsyncMock(return_value=[signal])
    )
    state = AlertStateManager()
    dispatch = await _run_cycle(state)

    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["rule_type"] == "external_service_down"
    # The rule namespace and the mute/recipient namespace differ on purpose.
    assert kwargs["resource_type"] == "service"


# ── The history row keeps both ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_row_records_rule_and_transition_separately(engine):
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
        await db.commit()

    with patch("app.services.alert_owner_dispatcher.async_session", session_factory), \
         patch("app.services.alert_owner_dispatcher.send_webhook",
               AsyncMock(return_value=(True, None))):
        await dispatch_alert(
            resource_type="server", resource_id="host-a",
            alert_type="resolved", rule_type="server_disk",
            target="host-a", message="Disk recovered",
        )

    async with session_factory() as db:
        row = (await db.execute(select(AlertHistory))).scalars().one()
    assert row.alert_type == "resolved"
    assert row.rule_type == "server_disk"


@pytest.mark.asyncio
async def test_rule_type_is_null_when_not_supplied(engine):
    """A caller that omits it still writes a row — the column is nullable."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertSettings(id=1, mail_channel_id=None, admin_emails="[]"))
        await db.commit()

    with patch("app.services.alert_owner_dispatcher.async_session", session_factory):
        await dispatch_alert(
            resource_type="db", resource_id="x", alert_type="triggered",
            target="x", message="down",
        )

    async with session_factory() as db:
        row = (await db.execute(select(AlertHistory))).scalars().one()
    assert row.rule_type is None


# ── History endpoint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_endpoint_returns_and_filters_by_rule_type(
    client, admin_token, seeded_db
):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        db.add(AlertHistory(
            alert_type="triggered", rule_type="db_health", target="mydb",
            message="db down", sent_at=now,
        ))
        db.add(AlertHistory(
            alert_type="triggered", rule_type="s3_health", target="archive",
            message="s3 down", sent_at=now - timedelta(minutes=1),
        ))
        db.add(AlertHistory(
            alert_type="resolved", rule_type="s3_health", target="archive",
            message="s3 up", sent_at=now - timedelta(minutes=2),
        ))
        await db.commit()

    resp = await client.get("/admin/alerts/history", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    assert {row["rule_type"] for row in resp.json()} == {"db_health", "s3_health"}

    resp = await client.get(
        "/admin/alerts/history",
        params={"rule_type": "s3_health"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 2
    assert all(row["rule_type"] == "s3_health" for row in rows)

    # rule_type and alert_type are independent filters.
    resp = await client.get(
        "/admin/alerts/history",
        params={"rule_type": "s3_health", "alert_type": "resolved"},
        headers=auth_header(admin_token),
    )
    assert [row["message"] for row in resp.json()] == ["s3 up"]


@pytest.mark.asyncio
async def test_history_endpoint_tolerates_rows_without_a_rule_type(
    client, admin_token, seeded_db
):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertHistory(alert_type="triggered", target="legacy", message="old row"))
        await db.commit()

    resp = await client.get("/admin/alerts/history", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["rule_type"] is None
