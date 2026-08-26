"""Daily GPU under-utilisation report.

The inverse of the ``server_gpu_util`` alert: instead of warning that a GPU host
is working too hard, this surfaces hosts that are idle. Once per KST calendar
day — at or after ``GPU_UTIL_REPORT_HOUR_KST`` — every GPU host with a target
utilisation is compared against its trailing-24h average GPU utilisation (mean
across the host's cards), and each host below its target gets one mail through
the ordinary owner/admin dispatch pipeline.

This is a report, not an alert: it never touches :class:`AlertState`, has no
triggered/resolved transitions, and produces history rows with
``alert_type="report"`` / ``rule_type="server_gpu_underutil"``. Delivery is
gated by the same mutes the checker honours, but a muted host simply misses that
day's mail — there is no pending re-fire, because there is no incident to
announce late.

The once-per-day marker (``AlertSettings.server_gpu_report_last_sent_at``) lives
in the shared meta DB so neither a blue/green pair nor a restart can double-send.
It is written after any successful evaluation — including one that mailed nobody
— so the rest of the day costs a single settings read per cycle. A Prometheus
failure, by contrast, leaves the marker alone and the run is retried on the next
checker cycle, which is also how the report catches up after downtime.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import AlertSettings, MonitoredHost
from app.services import server_monitor
from app.services.alert_mutes import load_mute_index
from app.services.alert_owner_dispatcher import dispatch_alert

logger = logging.getLogger(__name__)

# Korea Standard Time as a fixed UTC+9 offset — Korea has no DST, so this needs
# no zoneinfo lookup and therefore no tzdata in the container image.
KST = timezone(timedelta(hours=9))

RULE_TYPE = "server_gpu_underutil"
MONITOR_LABEL = "서버 GPU 목표 사용률(일일)"


def _as_utc(value: datetime) -> datetime:
    """Normalize to UTC, treating a naive value as UTC (SQLite reads back naive)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_report_due(
    now_utc: datetime,
    last_sent: datetime | None,
    report_hour_kst: int,
) -> bool:
    """True when today's KST report has not run yet and its hour has arrived.

    Both timestamps are compared as KST calendar dates, so a run that happened
    at 08:00 KST blocks re-runs for the rest of that KST day regardless of where
    the UTC date boundary falls inside it.
    """
    now_kst = _as_utc(now_utc).astimezone(KST)
    if now_kst.hour < report_hour_kst:
        return False
    if last_sent is None:
        return True
    return _as_utc(last_sent).astimezone(KST).date() < now_kst.date()


def _eligible_hosts(
    hosts: list[MonitoredHost], global_target: float
) -> list[tuple[MonitoredHost, float]]:
    """Enabled GPU hosts whose effective target is positive, with that target."""
    eligible: list[tuple[MonitoredHost, float]] = []
    for host in hosts:
        if not getattr(host, "enabled", True):
            continue
        if not server_monitor._gpu_address(host):
            continue
        target = server_monitor._effective(
            getattr(host, "gpu_util_target_pct", None), global_target
        )
        if target > 0:
            eligible.append((host, target))
    return eligible


async def _mark_report_sent(when: datetime) -> None:
    async with async_session() as db:
        settings_row = (
            await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
        ).scalar_one_or_none()
        if settings_row is None:
            return
        settings_row.server_gpu_report_last_sent_at = when
        await db.commit()


async def maybe_send_gpu_util_report(*, now: datetime | None = None) -> int | None:
    """Run today's under-utilisation report if it is due. Returns mails sent.

    Returns ``None`` when there is nothing to do (no settings row, or the report
    already ran for this KST day), and the number of hosts mailed otherwise.
    Prometheus failures propagate so the caller leaves the marker untouched and
    retries next cycle; a per-host delivery failure does not, because
    ``dispatch_alert`` records it and swallows it.
    """
    reference = _as_utc(now) if now is not None else datetime.now(timezone.utc)

    async with async_session() as db:
        settings_row = (
            await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
        ).scalar_one_or_none()
        if settings_row is None:
            return None
        if not is_report_due(
            reference,
            settings_row.server_gpu_report_last_sent_at,
            settings.GPU_UTIL_REPORT_HOUR_KST,
        ):
            return None
        global_target = float(settings_row.server_gpu_util_target_pct or 0.0)
        hosts = list((await db.execute(select(MonitoredHost))).scalars().all())

    eligible = _eligible_hosts(hosts, global_target)
    if not eligible:
        await _mark_report_sent(reference)
        return 0

    averages = await server_monitor.gpu_util_daily_avg_map()
    mutes = await load_mute_index()

    sent = 0
    for host, target in eligible:
        name = host.name
        if mutes.is_muted("server", name):
            logger.info("GPU report for '%s' skipped: notifications muted", name)
            continue
        average = averages.get(name)
        if average is None:
            # server_gpu_down already covers an exporter that was down all day.
            logger.info("GPU report for '%s' skipped: no 24h utilisation data", name)
            continue
        if average >= target:
            continue
        await dispatch_alert(
            resource_type="server",
            resource_id=name,
            alert_type="report",
            rule_type=RULE_TYPE,
            target=name,
            display_target=name,
            message=(
                f"Server '{name}' 24h average GPU utilisation is {average:.1f}% "
                f"— below the daily target of {target:.0f}%."
            ),
            rate=average,
            threshold=target,
            monitor_label=MONITOR_LABEL,
            target_description=str(getattr(host, "description", "") or ""),
        )
        sent += 1

    await _mark_report_sent(reference)
    return sent
