"""Alert mutes: CRUD, status reporting, and notification suppression.

Covers the three things a mute must get right:
  * The endpoints (``/admin/alerts/mutes``) and their validation/permissions.
  * ``/admin/alerts/status`` reporting mute state per target and globally.
  * The checker suppressing delivery while leaving detection untouched,
    including re-arming a trigger that a mute swallowed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models import AlertMute
from app.routers import alerts as alerts_router
from app.services import alert_checker
from app.services.alert_mutes import MuteIndex, build_index, resource_type_for_rule
from app.services.alert_state import AlertStateManager
from tests.conftest import auth_header

MUTES_URL = "/admin/alerts/mutes"


def _future(**kwargs) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).isoformat()


async def _put_mute(client, token, **body):
    return await client.put(MUTES_URL, json=body, headers=auth_header(token))


async def _get_mutes(client, token):
    resp = await client.get(MUTES_URL, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── CRUD ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_mutes_empty(client, admin_token):
    assert await _get_mutes(client, admin_token) == {
        "global_muted_until": None,
        "mutes": [],
    }


@pytest.mark.asyncio
async def test_create_resource_mute_and_list(client, admin_token):
    until = _future(hours=2)
    resp = await _put_mute(
        client, admin_token, resource_type="db", resource_id="orders-db", muted_until=until
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resource_type"] == "db"
    assert resp.json()["resource_id"] == "orders-db"
    assert resp.json()["created_by"] == "testadmin"

    body = await _get_mutes(client, admin_token)
    assert body["global_muted_until"] is None
    assert [(m["resource_type"], m["resource_id"]) for m in body["mutes"]] == [
        ("db", "orders-db")
    ]


@pytest.mark.asyncio
async def test_global_mute_reported_at_top_level(client, admin_token):
    resp = await _put_mute(
        client, admin_token, resource_type="global", resource_id="", muted_until=_future(hours=1)
    )
    assert resp.status_code == 200, resp.text

    body = await _get_mutes(client, admin_token)
    assert body["global_muted_until"] is not None
    assert [m["resource_type"] for m in body["mutes"]] == ["global"]


@pytest.mark.asyncio
async def test_upsert_replaces_existing_window(client, admin_token, seeded_db):
    await _put_mute(
        client, admin_token, resource_type="nas", resource_id="reports", muted_until=_future(hours=1)
    )
    later = _future(hours=5)
    resp = await _put_mute(
        client, admin_token, resource_type="nas", resource_id="reports", muted_until=later
    )
    assert resp.status_code == 200, resp.text

    body = await _get_mutes(client, admin_token)
    assert len(body["mutes"]) == 1
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        rows = (await db.execute(select(AlertMute))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_delete_mute(client, admin_token):
    await _put_mute(
        client, admin_token, resource_type="route", resource_id="r-1", muted_until=_future(hours=1)
    )
    resp = await client.delete(
        MUTES_URL,
        params={"resource_type": "route", "resource_id": "r-1"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204
    assert (await _get_mutes(client, admin_token))["mutes"] == []


@pytest.mark.asyncio
async def test_delete_unknown_mute_is_noop(client, admin_token):
    resp = await client.delete(
        MUTES_URL,
        params={"resource_type": "db", "resource_id": "never-muted"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_listing_does_not_write_when_nothing_expired(seeded_db):
    """A read-path DELETE would take SQLite's writer lock on every listing."""
    from app.services import alert_mutes

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertMute(
            resource_type="db", resource_id="live",
            muted_until=datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="testadmin",
        ))
        await db.commit()

    async with session_factory() as db:
        commit = AsyncMock()
        with patch.object(type(db), "commit", commit):
            assert await alert_mutes.purge_expired_mutes(db) == 0
            rows = await alert_mutes.list_active_mutes(db)
        commit.assert_not_awaited()
    assert [m.resource_id for m in rows] == ["live"]


@pytest.mark.asyncio
async def test_purge_reports_how_many_it_removed(seeded_db):
    from app.services import alert_mutes

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with session_factory() as db:
        for i in range(2):
            db.add(AlertMute(
                resource_type="db", resource_id=f"stale{i}",
                muted_until=past, created_by="testadmin",
            ))
        await db.commit()

    async with session_factory() as db:
        assert await alert_mutes.purge_expired_mutes(db) == 2
        assert await alert_mutes.purge_expired_mutes(db) == 0


@pytest.mark.asyncio
async def test_expired_mutes_are_pruned_lazily(client, admin_token, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(AlertMute(
            resource_type="db",
            resource_id="stale",
            muted_until=datetime.now(timezone.utc) - timedelta(minutes=1),
            created_by="testadmin",
        ))
        await db.commit()

    assert (await _get_mutes(client, admin_token))["mutes"] == []
    async with session_factory() as db:
        assert (await db.execute(select(AlertMute))).scalars().all() == []


@pytest.mark.parametrize(
    "body,detail_fragment",
    [
        ({"resource_type": "db", "resource_id": "x", "muted_until": "PAST"}, "future"),
        ({"resource_type": "db", "resource_id": "x", "muted_until": "TOO_FAR"}, "30 days"),
        ({"resource_type": "nope", "resource_id": "x", "muted_until": "OK"}, "resource type"),
        ({"resource_type": "global", "resource_id": "x", "muted_until": "OK"}, "must be empty"),
        ({"resource_type": "db", "resource_id": "", "muted_until": "OK"}, "required"),
    ],
)
@pytest.mark.asyncio
async def test_put_mute_validation(client, admin_token, body, detail_fragment):
    replacements = {
        "PAST": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "TOO_FAR": _future(days=31),
        "OK": _future(hours=1),
    }
    body = {**body, "muted_until": replacements[body["muted_until"]]}
    resp = await client.put(MUTES_URL, json=body, headers=auth_header(admin_token))
    assert resp.status_code == 422, resp.text
    assert detail_fragment in resp.json()["detail"]


@pytest.mark.asyncio
async def test_reader_can_list_but_not_mutate(client, alerts_reader_token):
    assert (await client.get(MUTES_URL, headers=auth_header(alerts_reader_token))).status_code == 200
    resp = await client.put(
        MUTES_URL,
        json={"resource_type": "db", "resource_id": "x", "muted_until": _future(hours=1)},
        headers=auth_header(alerts_reader_token),
    )
    assert resp.status_code == 403
    resp = await client.delete(
        MUTES_URL,
        params={"resource_type": "db", "resource_id": "x"},
        headers=auth_header(alerts_reader_token),
    )
    assert resp.status_code == 403


# ── Status reporting ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_reports_target_and_global_mutes(client, admin_token):
    state = AlertStateManager()
    for _ in range(2):
        state.update("db_health", "db-x", is_healthy=False, trigger_after_failures=2)
    state.update("nas_health", "nas-y", is_healthy=True, trigger_after_failures=2)
    alerts_router.set_alert_state(state)
    try:
        await _put_mute(
            client, admin_token, resource_type="db", resource_id="db-x",
            muted_until=_future(hours=3),
        )
        resp = await client.get("/admin/alerts/status", headers=auth_header(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["global_muted_until"] is None
        rows = {r["type"]: r for r in body["items"]}
        assert rows["db_health"]["muted"] is True
        assert rows["db_health"]["muted_until"] is not None
        assert rows["nas_health"]["muted"] is False

        await _put_mute(
            client, admin_token, resource_type="global", resource_id="",
            muted_until=_future(hours=4),
        )
        body = (await client.get(
            "/admin/alerts/status", headers=auth_header(admin_token)
        )).json()
        assert body["global_muted_until"] is not None
        assert all(row["muted"] for row in body["items"])
    finally:
        alerts_router.set_alert_state(None)


@pytest.mark.asyncio
async def test_status_lists_healthy_targets_too(client, admin_token):
    """The dashboard counts "N tracked" from status rows, so a healthy target
    must still produce one — for every rule, not just the ones firing."""
    state = AlertStateManager()
    state.update("server_down", "host-a", is_healthy=True, trigger_after_failures=2)
    state.update("external_service_down", "orders", is_healthy=True, trigger_after_failures=2)
    for _ in range(2):
        state.update("server_down", "host-b", is_healthy=False, trigger_after_failures=2)
    alerts_router.set_alert_state(state)
    try:
        body = (await client.get(
            "/admin/alerts/status", headers=auth_header(admin_token)
        )).json()
    finally:
        alerts_router.set_alert_state(None)

    rows = {(r["type"], r["resource_id"]): r for r in body["items"]}
    assert set(rows) == {
        ("server_down", "host-a"),
        ("server_down", "host-b"),
        ("external_service_down", "orders"),
    }
    assert rows[("server_down", "host-a")]["status"] == "ok"
    assert rows[("server_down", "host-b")]["status"] == "alert"
    assert rows[("server_down", "host-a")]["resource_type"] == "server"
    assert rows[("external_service_down", "orders")]["resource_type"] == "service"


@pytest.mark.asyncio
async def test_status_row_for_an_unmappable_rule_has_no_mute_key(client, admin_token):
    """A legacy/unknown rule renders without a mute button rather than guessing."""
    state = AlertStateManager()
    state.set_entry("error_rate", "legacy", status="ok", since="2026-01-01T00:00:00+00:00")
    alerts_router.set_alert_state(state)
    try:
        body = (await client.get(
            "/admin/alerts/status", headers=auth_header(admin_token)
        )).json()
    finally:
        alerts_router.set_alert_state(None)

    row = body["items"][0]
    assert row["resource_type"] is None
    assert row["resource_id"] is None


@pytest.mark.asyncio
async def test_status_target_stays_the_display_label(client, admin_token):
    """``target`` is a label; ``resource_id`` is the addressable key."""
    state = AlertStateManager()
    for _ in range(2):
        state.update(
            "route_error_rate", "r-1",
            is_healthy=False, display_target="checkout (r-1)", trigger_after_failures=2,
        )
    alerts_router.set_alert_state(state)
    try:
        body = (await client.get(
            "/admin/alerts/status", headers=auth_header(admin_token)
        )).json()
    finally:
        alerts_router.set_alert_state(None)

    row = body["items"][0]
    assert row["target"] == "checkout (r-1)"
    assert row["resource_type"] == "route"
    assert row["resource_id"] == "r-1"


@pytest.mark.asyncio
async def test_global_mute_round_trips_with_an_empty_resource_id(client, admin_token):
    resp = await _put_mute(
        client, admin_token, resource_type="global", resource_id="", muted_until=_future(hours=1)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resource_id"] == ""

    resp = await client.delete(
        MUTES_URL,
        params={"resource_type": "global", "resource_id": ""},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 204
    assert (await _get_mutes(client, admin_token))["global_muted_until"] is None


@pytest.mark.asyncio
async def test_global_mute_delete_without_the_resource_id_param(client, admin_token):
    """``resource_id`` defaults to "" so the param may be omitted entirely."""
    await _put_mute(
        client, admin_token, resource_type="global", resource_id="", muted_until=_future(hours=1)
    )
    resp = await client.delete(
        MUTES_URL, params={"resource_type": "global"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 204
    assert (await _get_mutes(client, admin_token))["mutes"] == []


def test_resource_type_for_rule_covers_every_rule():
    assert resource_type_for_rule("db_health") == "db"
    assert resource_type_for_rule("s3_health") == "s3"
    assert resource_type_for_rule("nas_health") == "nas"
    assert resource_type_for_rule("upstream_health") == "upstream"
    assert resource_type_for_rule("route_error_rate") == "route"
    assert resource_type_for_rule("server_gpu_mem") == "server"
    assert resource_type_for_rule("external_service_down") == "service"
    assert resource_type_for_rule("legacy_error_rate") is None


def test_mute_index_prefers_the_later_expiry():
    now = datetime.now(timezone.utc)
    index = MuteIndex(
        global_until=now + timedelta(hours=1),
        targets={("db", "x"): now + timedelta(hours=5)},
    )
    assert index.muted_until("db", "x") == now + timedelta(hours=5)
    assert index.muted_until("db", "other") == now + timedelta(hours=1)


def test_build_index_drops_already_expired_rows():
    now = datetime.now(timezone.utc)
    index = build_index(
        [
            AlertMute(
                resource_type="db", resource_id="gone",
                muted_until=now - timedelta(seconds=1), created_by="a",
            ),
            AlertMute(
                resource_type="global", resource_id="",
                muted_until=now + timedelta(hours=1), created_by="a",
            ),
        ],
        now=now,
    )
    assert index.targets == {}
    assert index.global_until is not None


# ── Notification suppression in the checker ──────────────────────────────────


def _muted(resource_type: str, resource_id: str) -> MuteIndex:
    return MuteIndex(
        targets={(resource_type, resource_id): datetime.now(timezone.utc) + timedelta(hours=1)}
    )


def _checker_patches():
    """Silence every probe the checker runs so a test drives one rule at a time."""
    return (
        patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._check_s3_health", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._check_nas_health", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]),
        patch("app.services.alert_checker._persist_state_safely", new_callable=AsyncMock),
    )


async def _run_db_cycle(
    state,
    *,
    healthy: bool,
    mutes: MuteIndex | None = None,
    resolve_after_successes: int = 1,
):
    """One check cycle where the single DB connection reports ``healthy``."""
    patches = _checker_patches()
    with patches[1], patches[2], patches[3], patches[4], patches[5], \
         patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as db_probe, \
         patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as dispatch:
        # (alias, is_healthy, reason) — reason is None for a normal healthy/failed
        # result; only a probe timeout carries a distinct reason.
        db_probe.return_value = [("mydb", healthy, None)]
        await alert_checker.run_single_check(
            state, trigger_after_failures=1,
            resolve_after_successes=resolve_after_successes,
            mutes=mutes or MuteIndex(),
        )
    return dispatch


@pytest.mark.asyncio
async def test_trigger_while_muted_is_withheld_but_state_still_flips():
    state = AlertStateManager()
    dispatch = await _run_db_cycle(state, healthy=False, mutes=_muted("db", "mydb"))

    dispatch.assert_not_awaited()
    assert state.get_status("db_health", "mydb") == "alert"
    assert state.get_pending_notify("db_health", "mydb") is True


@pytest.mark.asyncio
async def test_global_mute_withholds_every_target():
    state = AlertStateManager()
    global_mute = MuteIndex(global_until=datetime.now(timezone.utc) + timedelta(hours=1))
    dispatch = await _run_db_cycle(state, healthy=False, mutes=global_mute)

    dispatch.assert_not_awaited()
    assert state.get_pending_notify("db_health", "mydb") is True


@pytest.mark.asyncio
async def test_still_firing_after_mute_expiry_notifies_next_cycle():
    state = AlertStateManager()
    await _run_db_cycle(state, healthy=False, mutes=_muted("db", "mydb"))

    # Mute gone; the alert is unchanged, so this cycle has no transition of its
    # own — the withheld notification must still go out.
    dispatch = await _run_db_cycle(state, healthy=False)

    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["alert_type"] == "triggered"
    assert kwargs["rule_type"] == "db_health"
    assert kwargs["resource_id"] == "mydb"
    assert state.get_pending_notify("db_health", "mydb") is False

    # …and exactly once: a third cycle stays quiet.
    dispatch = await _run_db_cycle(state, healthy=False)
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_while_muted_sends_nothing_and_clears_the_flag():
    state = AlertStateManager()
    await _run_db_cycle(state, healthy=False, mutes=_muted("db", "mydb"))
    assert state.get_pending_notify("db_health", "mydb") is True

    dispatch = await _run_db_cycle(state, healthy=True, mutes=_muted("db", "mydb"))

    dispatch.assert_not_awaited()
    assert state.get_status("db_health", "mydb") == "ok"
    assert state.get_pending_notify("db_health", "mydb") is False

    # The next unmuted cycle must not resurrect the swallowed notification.
    dispatch = await _run_db_cycle(state, healthy=True)
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_right_after_mute_expiry_stays_silent():
    """The trigger was never announced, so neither is the recovery."""
    state = AlertStateManager()
    await _run_db_cycle(state, healthy=False, mutes=_muted("db", "mydb"))

    dispatch = await _run_db_cycle(state, healthy=True)

    dispatch.assert_not_awaited()
    assert state.get_pending_notify("db_health", "mydb") is False


@pytest.mark.asyncio
async def test_recovery_of_an_announced_incident_beats_the_mute():
    """Muting after being paged must not strand the incident open."""
    state = AlertStateManager()
    dispatch = await _run_db_cycle(state, healthy=False)
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["alert_type"] == "triggered"

    dispatch = await _run_db_cycle(state, healthy=True, mutes=_muted("db", "mydb"))

    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["alert_type"] == "resolved"


@pytest.mark.asyncio
async def test_recovery_of_an_announced_incident_beats_a_global_mute():
    state = AlertStateManager()
    await _run_db_cycle(state, healthy=False)

    global_mute = MuteIndex(global_until=datetime.now(timezone.utc) + timedelta(hours=1))
    dispatch = await _run_db_cycle(state, healthy=True, mutes=global_mute)

    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["alert_type"] == "resolved"


@pytest.mark.asyncio
async def test_deferred_trigger_is_not_announced_during_a_recovery_streak():
    """Mute deferral meets recovery damping, end to end: the owed trigger waits
    for a cycle where the target is failing rather than describing it as down
    on a cycle where it came back."""
    state = AlertStateManager()
    await _run_db_cycle(
        state, healthy=False, mutes=_muted("db", "mydb"), resolve_after_successes=3,
    )
    assert state.get_pending_notify("db_health", "mydb") is True

    # Mute gone, but this cycle is healthy (1 of the 3 needed to resolve).
    dispatch = await _run_db_cycle(state, healthy=True, resolve_after_successes=3)
    dispatch.assert_not_awaited()
    assert state.get_status("db_health", "mydb") == "alert"
    assert state.get_pending_notify("db_health", "mydb") is True

    # Failing again → the debt is paid on a cycle whose message is true.
    dispatch = await _run_db_cycle(state, healthy=False, resolve_after_successes=3)
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["alert_type"] == "triggered"


# ── The suppression decision, exhaustively ───────────────────────────────────
#
# ``pending_notify`` means exactly "this incident has not been announced".
# These drive the decision directly so the invariant is pinned independently of
# any one rule's plumbing: every announced trigger eventually gets an announced
# resolve, and nothing unannounced ever gets one.


def _state_with(status: str, *, pending: bool, fail_count: int = 0) -> AlertStateManager:
    state = AlertStateManager()
    state.set_entry(
        "db_health", "mydb",
        status=status, since="2026-01-01T00:00:00+00:00", pending_notify=pending,
        fail_count=fail_count,
    )
    return state


def _decide(state, *, transition, muted, was_alerting):
    return alert_checker._outbound_alert_type(
        state,
        _muted("db", "mydb") if muted else MuteIndex(),
        rule_type="db_health", target="mydb", transition=transition,
        resource_type="db", resource_id="mydb", was_alerting=was_alerting,
    )


@pytest.mark.parametrize("muted", [False, True])
def test_announced_incident_always_gets_its_recovery(muted):
    state = _state_with("ok", pending=False)
    assert _decide(state, transition="resolved", muted=muted, was_alerting=True) == "resolved"
    assert state.get_pending_notify("db_health", "mydb") is False


@pytest.mark.parametrize("muted", [False, True])
def test_unannounced_incident_never_gets_a_recovery(muted):
    state = _state_with("ok", pending=True)
    assert _decide(state, transition="resolved", muted=muted, was_alerting=True) is None
    assert state.get_pending_notify("db_health", "mydb") is False


def test_withheld_first_announcement_marks_the_incident_unannounced():
    state = _state_with("alert", pending=False)
    assert _decide(state, transition="triggered", muted=True, was_alerting=False) is None
    assert state.get_pending_notify("db_health", "mydb") is True


def test_withheld_reannouncement_leaves_the_incident_announced():
    """An escalation/repeat reminder is not a first announcement — flagging it
    would later swallow the recovery of an incident recipients were paged for."""
    state = _state_with("alert", pending=False)
    assert _decide(state, transition="triggered", muted=True, was_alerting=True) is None
    assert state.get_pending_notify("db_health", "mydb") is False


def test_delivered_trigger_always_clears_the_flag():
    state = _state_with("alert", pending=True)
    assert _decide(state, transition="triggered", muted=False, was_alerting=False) == "triggered"
    assert state.get_pending_notify("db_health", "mydb") is False


def test_no_transition_while_still_muted_keeps_the_debt():
    state = _state_with("alert", pending=True)
    assert _decide(state, transition=None, muted=True, was_alerting=True) is None
    assert state.get_pending_notify("db_health", "mydb") is True


def test_deferred_trigger_waits_for_a_failing_cycle():
    """Recovery damping can leave an alert mid-healthy-streak. Announcing the
    withheld trigger there would page people about a target that is fine right
    now, so the debt is kept until the target is actually failing again."""
    state = _state_with("alert", pending=True, fail_count=0)
    assert _decide(state, transition=None, muted=False, was_alerting=True) is None
    assert state.get_pending_notify("db_health", "mydb") is True


def test_deferred_trigger_fires_once_the_target_is_failing_again():
    state = _state_with("alert", pending=True, fail_count=1)
    assert _decide(state, transition=None, muted=False, was_alerting=True) == "triggered"
    assert state.get_pending_notify("db_health", "mydb") is False


@pytest.mark.asyncio
async def test_repeat_cadence_reminder_muted_then_resolved_still_notifies(monkeypatch):
    """End-to-end version of the reminder case, through the host-signal path
    that actually passes ``repeat_after_cycles``."""
    from app.services.server_monitor import HostSignal

    host = type("H", (), {"enabled": True})()

    def _signal(is_healthy: bool) -> HostSignal:
        return HostSignal(
            alert_type="server_down", target="host-a", display="host-a",
            is_healthy=is_healthy, severity=None if is_healthy else "critical",
            value=None, threshold=None,
            message="up" if is_healthy else "down", monitor_label="서버 상태",
        )

    monkeypatch.setattr(
        alert_checker, "_load_server_monitoring",
        AsyncMock(return_value=([host], alert_checker.ServerThresholds(), 1)),
    )
    monkeypatch.setattr(alert_checker, "_persist_state_safely", AsyncMock())
    state = AlertStateManager()

    async def _cycle(*, healthy: bool, mutes: MuteIndex):
        monkeypatch.setattr(
            alert_checker.server_monitor, "evaluate_hosts",
            AsyncMock(return_value=[_signal(healthy)]),
        )
        with patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as d:
            await alert_checker._check_server_health(
                state, trigger_after_failures=1, mutes=mutes,
            )
        return d

    # 1. Down and unmuted → paged.
    assert (await _cycle(healthy=False, mutes=MuteIndex())).await_args.kwargs[
        "alert_type"
    ] == "triggered"

    # 2. Still down, now muted; repeat cadence re-fires and is withheld.
    dispatch = await _cycle(healthy=False, mutes=_muted("server", "host-a"))
    dispatch.assert_not_awaited()
    assert state.get_pending_notify("server_down", "host-a") is False

    # 3. Recovers while still muted → the paged incident is still closed out.
    dispatch = await _cycle(healthy=True, mutes=_muted("server", "host-a"))
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["alert_type"] == "resolved"


@pytest.mark.asyncio
async def test_unmuted_target_notifies_normally():
    state = AlertStateManager()
    dispatch = await _run_db_cycle(state, healthy=False)
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["alert_type"] == "triggered"

    dispatch = await _run_db_cycle(state, healthy=True)
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["alert_type"] == "resolved"


@pytest.mark.asyncio
async def test_mute_on_a_different_target_does_not_suppress():
    state = AlertStateManager()
    dispatch = await _run_db_cycle(state, healthy=False, mutes=_muted("db", "someone-else"))
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_notify_survives_a_restart(db_session):
    """The flag lives on the alert_state row, not only in memory."""
    from app.services.alert_state import load_alert_state_from_db, save_alert_state_to_db

    state = AlertStateManager()
    state.update("db_health", "mydb", is_healthy=False, trigger_after_failures=1)
    state.set_pending_notify("db_health", "mydb", True)
    await save_alert_state_to_db(db_session, state, "db_health", "mydb")

    reloaded = AlertStateManager()
    await load_alert_state_from_db(db_session, reloaded)
    assert reloaded.get_pending_notify("db_health", "mydb") is True


@pytest.mark.asyncio
async def test_load_mute_index_survives_a_broken_table(caplog):
    """Alerting must keep notifying when the mute table cannot be read."""
    from app.services import alert_mutes

    with patch("app.database.async_session", side_effect=RuntimeError("no table")):
        index = await alert_mutes.load_mute_index()

    assert index.global_until is None
    assert index.targets == {}
    assert "Failed to load alert mutes" in caplog.text


@pytest.mark.asyncio
async def test_load_mute_index_reads_active_rows(seeded_db):
    from app.services import alert_mutes

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        db.add(AlertMute(
            resource_type="db", resource_id="live",
            muted_until=now + timedelta(hours=1), created_by="testadmin",
        ))
        db.add(AlertMute(
            resource_type="nas", resource_id="expired",
            muted_until=now - timedelta(hours=1), created_by="testadmin",
        ))
        await db.commit()

    with patch("app.database.async_session", session_factory):
        index = await alert_mutes.load_mute_index()

    assert index.is_muted("db", "live") is True
    assert index.is_muted("nas", "expired") is False


# ── Audit ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lost_insert_race_is_audited_as_an_update(client, admin_token, seeded_db, app):
    """Losing the insert race means updating somebody else's row, and the audit
    entry must say so rather than claiming a create with an empty before."""
    from app.database import get_db

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    rival_until = datetime.now(timezone.utc) + timedelta(hours=9)

    class _RacingSession:
        """Delegates to a real session; the first commit loses the race."""

        def __init__(self, inner):
            self._inner = inner
            self._commits = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def commit(self):
            self._commits += 1
            if self._commits > 1:
                await self._inner.commit()
                return
            await self._inner.rollback()
            async with session_factory() as rival:
                rival.add(AlertMute(
                    resource_type="db", resource_id="racy",
                    muted_until=rival_until, created_by="rival",
                ))
                await rival.commit()
            raise IntegrityError("insert alert_mutes", {}, Exception("unique constraint"))

    async def _override():
        async with session_factory() as inner:
            yield _RacingSession(inner)

    previous = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = _override
    try:
        resp = await _put_mute(
            client, admin_token, resource_type="db", resource_id="racy",
            muted_until=_future(hours=2),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["created_by"] == "testadmin"
    finally:
        app.dependency_overrides[get_db] = previous

    resp = await client.get(
        "/admin/audit-logs",
        params={"resource_type": "alert_mute"},
        headers=auth_header(admin_token),
    )
    logs = resp.json()
    assert len(logs) == 1
    assert logs[0]["action"] == "update"
    before = json.loads(logs[0]["before"])
    assert before is not None
    assert before["resource_id"] == "racy"
    assert before["muted_until"].startswith(rival_until.isoformat()[:19])


@pytest.mark.asyncio
async def test_mute_mutations_are_audited(client, admin_token):
    until = _future(hours=2)
    await _put_mute(client, admin_token, resource_type="db", resource_id="d1", muted_until=until)
    await _put_mute(client, admin_token, resource_type="db", resource_id="d1", muted_until=_future(hours=6))
    await client.delete(
        MUTES_URL,
        params={"resource_type": "db", "resource_id": "d1"},
        headers=auth_header(admin_token),
    )

    resp = await client.get(
        "/admin/audit-logs",
        params={"resource_type": "alert_mute"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    logs = resp.json()
    assert [e["action"] for e in logs] == ["delete", "update", "create"]
    assert all(e["resource_id"] == "db/d1" for e in logs)
    assert logs[2]["before"] is None
    assert logs[0]["after"] is None
