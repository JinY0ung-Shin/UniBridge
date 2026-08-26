"""Tests for the daily GPU under-utilisation report."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AlertSettings, MonitoredHost
from app.services.alert_mutes import MuteIndex
from app.services.gpu_report import is_report_due, maybe_send_gpu_util_report

UTC = timezone.utc

# 2026-08-26 23:30 UTC == 2026-08-27 08:30 KST: past the 08:00 send hour, on the
# KST day *after* the UTC one. Every marker comparison below hinges on that gap.
NOW_DUE = datetime(2026, 8, 26, 23, 30, tzinfo=UTC)
# 2026-08-26 22:30 UTC == 2026-08-27 07:30 KST — half an hour too early.
NOW_EARLY = datetime(2026, 8, 26, 22, 30, tzinfo=UTC)
# The previous day's run: 2026-08-25 23:05 UTC == 2026-08-26 08:05 KST.
SENT_YESTERDAY = datetime(2026, 8, 25, 23, 5, tzinfo=UTC)
# Today's run: 2026-08-26 23:05 UTC == 2026-08-27 08:05 KST.
SENT_TODAY = datetime(2026, 8, 26, 23, 5, tzinfo=UTC)


# ── is_report_due ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "last_sent", "expected"),
    [
        (NOW_EARLY, None, False),
        (datetime(2026, 8, 26, 23, 0, tzinfo=UTC), None, True),   # exactly 08:00 KST
        (datetime(2026, 8, 27, 3, 0, tzinfo=UTC), None, True),    # 12:00 KST
        (NOW_DUE, None, True),
        (NOW_DUE, SENT_TODAY, False),
        (NOW_DUE, SENT_YESTERDAY, True),
        # SQLite hands back naive datetimes; they must read as UTC, not local.
        (NOW_DUE, SENT_TODAY.replace(tzinfo=None), False),
        (NOW_DUE, SENT_YESTERDAY.replace(tzinfo=None), True),
    ],
)
def test_is_report_due(now, last_sent, expected):
    assert is_report_due(now, last_sent, 8) is expected


def test_is_report_due_compares_kst_dates_not_utc_dates():
    """The UTC date rolls over at 09:00 KST — an hour after the report runs.

    Comparing UTC dates would therefore call the report due again at 09:00 KST
    on the very day it already ran, sending every mail twice.
    """
    just_after_utc_midnight = datetime(2026, 8, 27, 0, 30, tzinfo=UTC)  # 09:30 KST
    assert just_after_utc_midnight.date() != SENT_TODAY.date()
    assert is_report_due(just_after_utc_midnight, SENT_TODAY, 8) is False
    # Same clock reading, a marker one KST day older → genuinely due.
    assert is_report_due(just_after_utc_midnight, SENT_YESTERDAY, 8) is True


# ── maybe_send_gpu_util_report ───────────────────────────────────────────────


def _gpu_host(name: str, *, target: float | None = None, **overrides) -> MonitoredHost:
    return MonitoredHost(
        name=name,
        address="10.0.0.1:9100",
        gpu_address=overrides.pop("gpu_address", "10.0.0.1:9400"),
        gpu_util_target_pct=target,
        **overrides,
    )


async def _seed(
    engine,
    *,
    global_target: float = 0.0,
    last_sent: datetime | None = None,
    hosts: tuple[MonitoredHost, ...] = (),
    settings_row: bool = True,
):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        if settings_row:
            db.add(AlertSettings(
                id=1,
                admin_emails="[]",
                server_gpu_util_target_pct=global_target,
                server_gpu_report_last_sent_at=last_sent,
            ))
        for host in hosts:
            db.add(host)
        await db.commit()
    return factory


async def _run(
    factory,
    *,
    averages: dict[str, float] | None = None,
    now: datetime = NOW_DUE,
    mutes: MuteIndex | None = None,
    raises: Exception | None = None,
):
    """Run the report with Prometheus, mutes and dispatch stubbed out."""
    dispatch = AsyncMock()
    avg_map = AsyncMock(return_value=averages or {})
    if raises is not None:
        avg_map = AsyncMock(side_effect=raises)
    with patch("app.services.gpu_report.async_session", factory), \
         patch("app.services.gpu_report.server_monitor.gpu_util_daily_avg_map", avg_map), \
         patch("app.services.gpu_report.load_mute_index", AsyncMock(return_value=mutes or MuteIndex())), \
         patch("app.services.gpu_report.dispatch_alert", dispatch):
        sent = await maybe_send_gpu_util_report(now=now)
    return sent, dispatch, avg_map


async def _marker(factory) -> datetime | None:
    async with factory() as db:
        result = await db.execute(
            select(AlertSettings.server_gpu_report_last_sent_at).where(AlertSettings.id == 1)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_below_target_host_is_reported(engine):
    factory = await _seed(
        engine,
        global_target=50.0,
        hosts=(_gpu_host("gpu1", description="training node"),),
    )
    sent, dispatch, _ = await _run(factory, averages={"gpu1": 12.5})

    assert sent == 1
    kwargs = dispatch.await_args.kwargs
    assert kwargs["resource_type"] == "server"
    assert kwargs["resource_id"] == "gpu1"
    assert kwargs["alert_type"] == "report"
    assert kwargs["rule_type"] == "server_gpu_underutil"
    assert kwargs["target"] == "gpu1"
    assert kwargs["display_target"] == "gpu1"
    assert kwargs["rate"] == 12.5
    assert kwargs["threshold"] == 50.0
    assert kwargs["target_description"] == "training node"
    assert kwargs["message"] == (
        "Server 'gpu1' 24h average GPU utilisation is 12.5% "
        "— below the daily target of 50%."
    )
    assert await _marker(factory) == NOW_DUE


@pytest.mark.asyncio
async def test_host_at_or_above_target_is_not_reported(engine):
    factory = await _seed(engine, global_target=50.0, hosts=(_gpu_host("gpu1"),))
    # Exactly on target counts as met: the report fires strictly below.
    sent, dispatch, _ = await _run(factory, averages={"gpu1": 50.0})

    assert sent == 0
    dispatch.assert_not_awaited()
    assert await _marker(factory) == NOW_DUE


@pytest.mark.asyncio
async def test_per_host_zero_target_switches_the_report_off(engine):
    factory = await _seed(engine, global_target=50.0, hosts=(_gpu_host("spare", target=0.0),))
    sent, dispatch, avg_map = await _run(factory, averages={"spare": 1.0})

    assert sent == 0
    dispatch.assert_not_awaited()
    # No eligible host at all → Prometheus is never asked.
    avg_map.assert_not_awaited()


@pytest.mark.asyncio
async def test_null_per_host_target_inherits_the_global_default(engine):
    factory = await _seed(engine, global_target=40.0, hosts=(_gpu_host("gpu1", target=None),))
    sent, dispatch, _ = await _run(factory, averages={"gpu1": 39.9})

    assert sent == 1
    assert dispatch.await_args.kwargs["threshold"] == 40.0


@pytest.mark.asyncio
async def test_per_host_target_works_while_the_global_default_is_off(engine):
    factory = await _seed(
        engine,
        global_target=0.0,
        hosts=(_gpu_host("watched", target=30.0), _gpu_host("ignored")),
    )
    sent, dispatch, _ = await _run(factory, averages={"watched": 5.0, "ignored": 1.0})

    assert sent == 1
    assert dispatch.await_args.kwargs["resource_id"] == "watched"


@pytest.mark.asyncio
async def test_muted_host_is_skipped_for_the_day(engine):
    factory = await _seed(engine, global_target=50.0, hosts=(_gpu_host("gpu1"),))
    mutes = MuteIndex(targets={("server", "gpu1"): NOW_DUE + timedelta(hours=2)})
    sent, dispatch, _ = await _run(factory, averages={"gpu1": 5.0}, mutes=mutes)

    assert sent == 0
    dispatch.assert_not_awaited()
    # The day is still marked done — a report has nothing to re-fire later.
    assert await _marker(factory) == NOW_DUE


@pytest.mark.asyncio
async def test_host_missing_from_prometheus_is_skipped(engine):
    factory = await _seed(engine, global_target=50.0, hosts=(_gpu_host("gpu1"),))
    sent, dispatch, _ = await _run(factory, averages={})

    assert sent == 0
    dispatch.assert_not_awaited()
    assert await _marker(factory) == NOW_DUE


@pytest.mark.asyncio
async def test_disabled_and_non_gpu_hosts_are_ineligible(engine):
    factory = await _seed(
        engine,
        global_target=50.0,
        hosts=(
            _gpu_host("off", enabled=False),
            _gpu_host("cpu-only", gpu_address=None),
        ),
    )
    sent, dispatch, avg_map = await _run(factory, averages={"off": 1.0, "cpu-only": 1.0})

    assert sent == 0
    dispatch.assert_not_awaited()
    avg_map.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_written_when_no_host_is_eligible(engine):
    factory = await _seed(engine, global_target=0.0)
    sent, dispatch, avg_map = await _run(factory)

    assert sent == 0
    dispatch.assert_not_awaited()
    avg_map.assert_not_awaited()
    # Marking the empty run keeps the rest of the day at one settings read.
    assert await _marker(factory) == NOW_DUE


@pytest.mark.asyncio
async def test_prometheus_failure_propagates_and_leaves_the_marker(engine):
    factory = await _seed(
        engine,
        global_target=50.0,
        last_sent=SENT_YESTERDAY,
        hosts=(_gpu_host("gpu1"),),
    )
    with pytest.raises(RuntimeError, match="prometheus down"):
        await _run(factory, raises=RuntimeError("prometheus down"))

    # Unmarked → the next checker cycle retries today's report.
    assert await _marker(factory) == SENT_YESTERDAY


@pytest.mark.asyncio
async def test_not_due_returns_none_without_querying(engine):
    factory = await _seed(engine, global_target=50.0, hosts=(_gpu_host("gpu1"),))
    sent, dispatch, avg_map = await _run(factory, averages={"gpu1": 1.0}, now=NOW_EARLY)

    assert sent is None
    dispatch.assert_not_awaited()
    avg_map.assert_not_awaited()
    assert await _marker(factory) is None


@pytest.mark.asyncio
async def test_already_sent_today_returns_none(engine):
    factory = await _seed(
        engine, global_target=50.0, last_sent=SENT_TODAY, hosts=(_gpu_host("gpu1"),)
    )
    sent, dispatch, avg_map = await _run(factory, averages={"gpu1": 1.0})

    assert sent is None
    dispatch.assert_not_awaited()
    avg_map.assert_not_awaited()
    assert await _marker(factory) == SENT_TODAY


@pytest.mark.asyncio
async def test_missing_settings_row_returns_none(engine):
    factory = await _seed(engine, settings_row=False, hosts=(_gpu_host("gpu1"),))
    sent, dispatch, avg_map = await _run(factory, averages={"gpu1": 1.0})

    assert sent is None
    dispatch.assert_not_awaited()
    avg_map.assert_not_awaited()
