"""Periodic pruning of query/admin audit logs and alert history.

Each table has its own retention setting (days) in
:class:`~app.services.settings_manager.SettingsManager`, where 0 means "keep
forever" — the default, so upgrading never silently discards history. Deletes
run in bounded batches with a commit per batch so a large first sweep neither
holds a long transaction nor locks out writers on SQLite.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAuditLog, AlertHistory, AuditLog
from app.services.active_color import is_active_instance
from app.services.settings_manager import settings_manager

logger = logging.getLogger(__name__)

# Rows removed per DELETE statement. Large enough that a backlog drains in a
# few cycles, small enough that no single statement blocks other writers.
BATCH_SIZE = 2000

# Safety valve: stop after this many batches in one table/run and pick up the
# rest next hour, so a huge backlog can never monopolise the event loop.
MAX_BATCHES_PER_RUN = 200

CLEANUP_INTERVAL_SECONDS = 3600
FIRST_RUN_DELAY_SECONDS = 60

# (label, model, timestamp column, settings attribute holding the day count)
_RETENTION_TARGETS: tuple[tuple[str, type, Column, str], ...] = (
    ("audit_logs", AuditLog, AuditLog.timestamp, "audit_log_retention_days"),
    (
        "admin_audit_logs",
        AdminAuditLog,
        AdminAuditLog.timestamp,
        "admin_audit_log_retention_days",
    ),
    ("alert_history", AlertHistory, AlertHistory.sent_at, "alert_history_retention_days"),
)


async def _delete_older_than(
    db: AsyncSession,
    model: type,
    timestamp_column: Column,
    cutoff: datetime,
) -> int:
    """Delete rows older than ``cutoff`` in batches. Returns rows removed.

    Restricting the DELETE to a bounded set of ids keeps each statement small
    and portable — ``DELETE ... LIMIT`` is a MySQL extension that neither
    SQLite (without the optional compile flag) nor PostgreSQL accepts.
    """
    total = 0
    for _ in range(MAX_BATCHES_PER_RUN):
        doomed = (
            select(model.id)
            .where(timestamp_column < cutoff)
            .order_by(model.id)
            .limit(BATCH_SIZE)
            .scalar_subquery()
        )
        result = await db.execute(sa_delete(model).where(model.id.in_(doomed)))
        await db.commit()
        removed = result.rowcount or 0
        total += removed
        if removed < BATCH_SIZE:
            break
    else:
        logger.warning(
            "Retention cleanup hit the %d-batch ceiling for %s; "
            "remaining rows will be removed on the next run",
            MAX_BATCHES_PER_RUN,
            model.__tablename__,
        )
    return total


async def run_retention_cleanup(*, now: datetime | None = None) -> dict[str, int]:
    """Apply every enabled retention policy once. Returns rows deleted per table."""
    from app.database import async_session

    reference = now or datetime.now(timezone.utc)
    deleted: dict[str, int] = {}

    for label, model, timestamp_column, setting_name in _RETENTION_TARGETS:
        days = int(getattr(settings_manager, setting_name, 0) or 0)
        if days <= 0:
            continue
        cutoff = reference - timedelta(days=days)
        try:
            async with async_session() as db:
                removed = await _delete_older_than(db, model, timestamp_column, cutoff)
        except Exception:
            logger.exception("Retention cleanup failed for %s", label)
            continue
        if removed:
            deleted[label] = removed

    if deleted:
        logger.info(
            "Retention cleanup removed %d row(s): %s",
            sum(deleted.values()),
            ", ".join(f"{label}={count}" for label, count in deleted.items()),
        )
    return deleted


async def run_retention_loop(
    *,
    interval_seconds: int = CLEANUP_INTERVAL_SECONDS,
    first_delay_seconds: int = FIRST_RUN_DELAY_SECONDS,
) -> None:
    """Background loop: sweep once shortly after boot, then hourly.

    Only the active blue/green color sweeps. Both colors share one meta DB, so
    an ungated standby would race the active color through the same batched
    deletes for no benefit. The check is per cycle, not once at startup: a
    promote or rollback rewrites the APISIX upstream without restarting
    containers, so ownership of the sweep has to be able to move on the next
    hourly tick.

    Every failure is swallowed so the task outlives a transient database
    problem — a missed sweep is caught by the next one.
    """
    logger.info("Log retention cleanup started")
    await asyncio.sleep(first_delay_seconds)
    while True:
        try:
            if await is_active_instance():
                await run_retention_cleanup()
        except Exception:
            logger.exception("Log retention cleanup cycle failed")
        await asyncio.sleep(interval_seconds)
