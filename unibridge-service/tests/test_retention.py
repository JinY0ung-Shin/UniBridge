"""Log retention: the settings that drive it and the cleanup task that applies it."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models import AdminAuditLog, AlertHistory, AuditLog, SystemConfig
from app.services import retention
from app.services.settings_manager import SettingsManager, settings_manager
from tests.conftest import auth_header

SETTINGS_URL = "/admin/query/settings"


def _session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_rows(engine, *, old_days: int = 100, recent_days: int = 1, count: int = 3):
    """Insert ``count`` old and ``count`` recent rows in each retained table."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=old_days)
    recent = now - timedelta(days=recent_days)
    async with _session_factory(engine)() as db:
        for stamp in (old, recent):
            for i in range(count):
                db.add(AuditLog(
                    timestamp=stamp, user=f"u{i}", database_alias="db",
                    sql="SELECT 1", status="success",
                ))
                db.add(AdminAuditLog(
                    timestamp=stamp, actor=f"a{i}", action="update",
                    resource_type="route", resource_id="r", status="success",
                ))
                db.add(AlertHistory(
                    sent_at=stamp, alert_type="triggered", target="t", message="m",
                ))
        await db.commit()


async def _counts(engine) -> dict[str, int]:
    async with _session_factory(engine)() as db:
        return {
            "audit_logs": (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one(),
            "admin_audit_logs": (await db.execute(select(func.count()).select_from(AdminAuditLog))).scalar_one(),
            "alert_history": (await db.execute(select(func.count()).select_from(AlertHistory))).scalar_one(),
        }


@pytest.fixture
def retention_manager():
    """A fresh manager installed as the module singleton for one test."""
    manager = SettingsManager()
    with patch("app.services.retention.settings_manager", manager):
        yield manager


# ── Cleanup ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_removes_old_rows_and_keeps_recent(engine, retention_manager):
    await _seed_rows(engine)
    retention_manager.audit_log_retention_days = 30
    retention_manager.admin_audit_log_retention_days = 30
    retention_manager.alert_history_retention_days = 30

    with patch("app.database.async_session", _session_factory(engine)):
        deleted = await retention.run_retention_cleanup()

    assert deleted == {"audit_logs": 3, "admin_audit_logs": 3, "alert_history": 3}
    assert await _counts(engine) == {
        "audit_logs": 3, "admin_audit_logs": 3, "alert_history": 3,
    }


@pytest.mark.asyncio
async def test_cleanup_is_disabled_at_zero(engine, retention_manager):
    await _seed_rows(engine)
    before = await _counts(engine)

    with patch("app.database.async_session", _session_factory(engine)):
        assert await retention.run_retention_cleanup() == {}

    assert await _counts(engine) == before


@pytest.mark.asyncio
async def test_each_table_honours_only_its_own_setting(engine, retention_manager):
    await _seed_rows(engine)
    retention_manager.alert_history_retention_days = 30

    with patch("app.database.async_session", _session_factory(engine)):
        deleted = await retention.run_retention_cleanup()

    assert deleted == {"alert_history": 3}
    counts = await _counts(engine)
    assert counts["alert_history"] == 3
    assert counts["audit_logs"] == 6
    assert counts["admin_audit_logs"] == 6


@pytest.mark.asyncio
async def test_a_row_exactly_at_the_cutoff_is_kept(engine, retention_manager):
    now = datetime.now(timezone.utc)
    async with _session_factory(engine)() as db:
        db.add(AlertHistory(sent_at=now - timedelta(days=30), alert_type="triggered",
                            target="edge", message="m"))
        await db.commit()
    retention_manager.alert_history_retention_days = 30

    with patch("app.database.async_session", _session_factory(engine)):
        # ``now`` is the same reference used to compute the cutoff, so the row
        # sits exactly on it and the strict ``<`` comparison spares it.
        assert await retention.run_retention_cleanup(now=now) == {}

    assert (await _counts(engine))["alert_history"] == 1


@pytest.mark.asyncio
async def test_cleanup_deletes_in_batches(engine, retention_manager):
    old = datetime.now(timezone.utc) - timedelta(days=100)
    async with _session_factory(engine)() as db:
        for i in range(7):
            db.add(AlertHistory(sent_at=old, alert_type="triggered", target=f"t{i}", message="m"))
        await db.commit()
    retention_manager.alert_history_retention_days = 1

    with patch("app.database.async_session", _session_factory(engine)), \
         patch.object(retention, "BATCH_SIZE", 3):
        assert await retention.run_retention_cleanup() == {"alert_history": 7}

    assert (await _counts(engine))["alert_history"] == 0


@pytest.mark.asyncio
async def test_batch_ceiling_defers_the_rest_to_the_next_run(engine, retention_manager, caplog):
    old = datetime.now(timezone.utc) - timedelta(days=100)
    async with _session_factory(engine)() as db:
        for i in range(6):
            db.add(AlertHistory(sent_at=old, alert_type="triggered", target=f"t{i}", message="m"))
        await db.commit()
    retention_manager.alert_history_retention_days = 1

    with patch("app.database.async_session", _session_factory(engine)), \
         patch.object(retention, "BATCH_SIZE", 2), \
         patch.object(retention, "MAX_BATCHES_PER_RUN", 2):
        assert await retention.run_retention_cleanup() == {"alert_history": 4}

    assert "batch ceiling" in caplog.text
    assert (await _counts(engine))["alert_history"] == 2


@pytest.mark.asyncio
async def test_one_failing_table_does_not_stop_the_others(engine, retention_manager, caplog):
    await _seed_rows(engine)
    retention_manager.audit_log_retention_days = 30
    retention_manager.alert_history_retention_days = 30

    real_delete = retention._delete_older_than

    async def _fail_on_audit_logs(db, model, column, cutoff):
        if model is AuditLog:
            raise RuntimeError("table locked")
        return await real_delete(db, model, column, cutoff)

    with patch("app.database.async_session", _session_factory(engine)), \
         patch.object(retention, "_delete_older_than", _fail_on_audit_logs):
        deleted = await retention.run_retention_cleanup()

    assert deleted == {"alert_history": 3}
    assert "Retention cleanup failed for audit_logs" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_logs_a_single_summary_line(engine, retention_manager, caplog):
    caplog.set_level(logging.INFO, logger="app.services.retention")
    await _seed_rows(engine)
    retention_manager.audit_log_retention_days = 30

    with patch("app.database.async_session", _session_factory(engine)):
        await retention.run_retention_cleanup()

    summaries = [r for r in caplog.records if "Retention cleanup removed" in r.getMessage()]
    assert len(summaries) == 1
    assert "audit_logs=3" in summaries[0].getMessage()


@pytest.mark.asyncio
async def test_cleanup_stays_quiet_when_nothing_was_deleted(engine, retention_manager, caplog):
    caplog.set_level(logging.INFO, logger="app.services.retention")
    retention_manager.audit_log_retention_days = 30
    with patch("app.database.async_session", _session_factory(engine)):
        assert await retention.run_retention_cleanup() == {}
    assert "Retention cleanup removed" not in caplog.text


def test_batch_size_stays_within_the_intended_band():
    assert 1000 <= retention.BATCH_SIZE <= 5000


# ── Loop ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_waits_before_the_first_run_then_repeats():
    sleeps: list[float] = []
    calls = 0

    async def _fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    async def _fake_cleanup():
        nonlocal calls
        calls += 1
        return {}

    with patch("app.services.retention.asyncio.sleep", _fake_sleep), \
         patch("app.services.retention.run_retention_cleanup", _fake_cleanup):
        with pytest.raises(asyncio.CancelledError):
            await retention.run_retention_loop(interval_seconds=3600, first_delay_seconds=60)

    assert sleeps[0] == 60
    assert sleeps[1] == 3600
    assert calls == 2


@pytest.mark.asyncio
async def test_loop_survives_a_failing_cycle(caplog):
    sleeps = 0

    async def _fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    cleanup = AsyncMock(side_effect=[RuntimeError("db gone"), {}])

    with patch("app.services.retention.asyncio.sleep", _fake_sleep), \
         patch("app.services.retention.run_retention_cleanup", cleanup):
        with pytest.raises(asyncio.CancelledError):
            await retention.run_retention_loop()

    assert cleanup.await_count == 2
    assert "Log retention cleanup cycle failed" in caplog.text


@pytest.mark.asyncio
async def test_loop_skips_the_sweep_on_the_standby_color():
    """Blue/green share one meta DB — only the active color may delete."""
    sleeps = 0

    async def _fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    cleanup = AsyncMock(return_value={})

    with patch("app.services.retention.asyncio.sleep", _fake_sleep), \
         patch("app.services.retention.is_active_instance",
               new=AsyncMock(return_value=False)), \
         patch("app.services.retention.run_retention_cleanup", cleanup):
        with pytest.raises(asyncio.CancelledError):
            await retention.run_retention_loop()

    cleanup.assert_not_awaited()
    # The loop stays alive and keeps re-checking, so a promote is picked up.
    assert sleeps == 3


@pytest.mark.asyncio
async def test_loop_starts_sweeping_after_a_promotion_without_a_restart():
    sleeps = 0

    async def _fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    cleanup = AsyncMock(return_value={})
    # Standby on the first cycle, promoted before the second.
    active = AsyncMock(side_effect=[False, True])

    with patch("app.services.retention.asyncio.sleep", _fake_sleep), \
         patch("app.services.retention.is_active_instance", new=active), \
         patch("app.services.retention.run_retention_cleanup", cleanup):
        with pytest.raises(asyncio.CancelledError):
            await retention.run_retention_loop()

    assert cleanup.await_count == 1


# ── Settings ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_endpoint_exposes_retention_defaults(client, admin_token):
    resp = await client.get(SETTINGS_URL, headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audit_log_retention_days"] == 0
    assert body["admin_audit_log_retention_days"] == 0
    assert body["alert_history_retention_days"] == 0


@pytest.mark.asyncio
async def test_settings_endpoint_updates_retention(client, admin_token):
    original = (
        settings_manager.audit_log_retention_days,
        settings_manager.admin_audit_log_retention_days,
        settings_manager.alert_history_retention_days,
    )
    try:
        resp = await client.put(
            SETTINGS_URL,
            json={
                "audit_log_retention_days": 90,
                "admin_audit_log_retention_days": 365,
                "alert_history_retention_days": 30,
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["audit_log_retention_days"] == 90
        assert body["admin_audit_log_retention_days"] == 365
        assert body["alert_history_retention_days"] == 30

        resp = await client.get(SETTINGS_URL, headers=auth_header(admin_token))
        assert resp.json()["audit_log_retention_days"] == 90
    finally:
        (
            settings_manager.audit_log_retention_days,
            settings_manager.admin_audit_log_retention_days,
            settings_manager.alert_history_retention_days,
        ) = original


@pytest.mark.parametrize(
    "field",
    ["audit_log_retention_days", "admin_audit_log_retention_days", "alert_history_retention_days"],
)
@pytest.mark.asyncio
async def test_negative_retention_is_rejected(client, admin_token, field):
    resp = await client.put(
        SETTINGS_URL, json={field: -1}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_retention_update_is_audited(client, admin_token):
    original = settings_manager.alert_history_retention_days
    try:
        resp = await client.put(
            SETTINGS_URL,
            json={"alert_history_retention_days": 45},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text

        resp = await client.get(
            "/admin/audit-logs",
            params={"resource_type": "system_settings"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text
        logs = resp.json()
        assert len(logs) == 1
        assert logs[0]["action"] == "update"
        assert logs[0]["resource_id"] == "global"
        import json as _json
        assert _json.loads(logs[0]["after"])["alert_history_retention_days"] == 45
    finally:
        settings_manager.alert_history_retention_days = original


@pytest.mark.asyncio
async def test_manager_persists_and_reloads_retention(db_session):
    manager = SettingsManager()
    await manager.update(
        db_session,
        audit_log_retention_days=7,
        admin_audit_log_retention_days=14,
        alert_history_retention_days=21,
    )

    reloaded = SettingsManager()
    await reloaded.load_from_db(db_session)
    assert reloaded.audit_log_retention_days == 7
    assert reloaded.admin_audit_log_retention_days == 14
    assert reloaded.alert_history_retention_days == 21


@pytest.mark.asyncio
async def test_manager_rejects_a_negative_day_count(db_session):
    manager = SettingsManager()
    with pytest.raises(ValueError, match="must be >= 0"):
        await manager.update(db_session, audit_log_retention_days=-5)


@pytest.mark.asyncio
async def test_manager_ignores_unusable_stored_values(db_session, caplog):
    db_session.add(SystemConfig(key="audit_log_retention_days", value="not-a-number"))
    db_session.add(SystemConfig(key="alert_history_retention_days", value="-3"))
    await db_session.commit()

    manager = SettingsManager()
    await manager.load_from_db(db_session)

    assert manager.audit_log_retention_days == 0
    assert manager.alert_history_retention_days == 0
    assert "Invalid audit_log_retention_days" in caplog.text
    assert "Negative alert_history_retention_days" in caplog.text
