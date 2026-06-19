from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_types import utcnow
from app.models import AlertState

logger = logging.getLogger(__name__)


_SEVERITY_RANK = {"warning": 1, "critical": 2}


def _severity_rank(severity: str | None) -> int:
    return _SEVERITY_RANK.get(severity or "", 0)


class AlertStateManager:
    """In-memory alert state tracker.

    Each entry: status ∈ {"ok", "alert"}, fail_count is the consecutive
    failure tally, since is the timestamp of the most recent status flip.
    ``severity`` (optional) carries the current host-signal severity while
    alerting, and ``cycles_in_alert`` drives re-notification cadence.
    """

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], dict] = {}

    def get_status(self, alert_type: str, target: str) -> str:
        entry = self._states.get((alert_type, target))
        return entry["status"] if entry else "ok"

    def get_entry(self, alert_type: str, target: str) -> dict[str, Any] | None:
        entry = self._states.get((alert_type, target))
        if entry is None:
            return None
        return {
            "type": alert_type,
            "target": target,
            "status": entry["status"],
            "since": entry["since"],
            "display_target": entry.get("display_target", target),
            "fail_count": entry.get("fail_count", 0),
            "severity": entry.get("severity"),
        }

    def set_entry(
        self,
        alert_type: str,
        target: str,
        *,
        status: str,
        since: str,
        display_target: str | None = None,
        fail_count: int = 0,
        severity: str | None = None,
    ) -> None:
        self._states[(alert_type, target)] = {
            "status": status,
            "since": since,
            "display_target": display_target or target,
            "fail_count": fail_count,
            "severity": severity,
            "cycles_in_alert": 0,
        }

    def update(
        self,
        alert_type: str,
        target: str,
        *,
        is_healthy: bool,
        trigger_after_failures: int,
        display_target: str | None = None,
        severity: str | None = None,
        repeat_after_cycles: int = 0,
    ) -> str | None:
        """Update state and return transition type if changed.

        Returns "triggered" / "resolved" / None. fail_count drives status:
        flip to "alert" only when fail_count reaches trigger_after_failures.

        ``severity`` (host signals) enables escalation: while already alerting,
        a rise in severity (e.g. warning → critical) re-fires "triggered".
        ``repeat_after_cycles`` > 0 re-fires "triggered" every N unhealthy
        cycles while the alert persists. Callers that pass neither keep the
        original binary behaviour exactly.
        """
        key = (alert_type, target)
        now = datetime.now(timezone.utc).isoformat()
        entry = self._states.get(key)

        if entry is None:
            if is_healthy:
                self.set_entry(
                    alert_type, target,
                    status="ok", since=now, display_target=display_target, fail_count=0,
                )
                logger.info("Alert state %s/%s initialized as ok", alert_type, target)
                return None
            fail_count = 1
            if fail_count >= trigger_after_failures:
                self.set_entry(
                    alert_type, target,
                    status="alert", since=now, display_target=display_target,
                    fail_count=fail_count, severity=severity,
                )
                logger.info("Alert state %s/%s initialized as alert", alert_type, target)
                return "triggered"
            self.set_entry(
                alert_type, target,
                status="ok", since=now, display_target=display_target, fail_count=fail_count,
            )
            logger.info(
                "Alert state %s/%s initialized as ok (fail_count=%d)",
                alert_type, target, fail_count,
            )
            return None

        # Update display_target on every observation so renames propagate.
        if display_target is not None:
            entry["display_target"] = display_target

        if is_healthy:
            was_alert = entry["status"] == "alert"
            entry["fail_count"] = 0
            entry["cycles_in_alert"] = 0
            if was_alert:
                entry["status"] = "ok"
                entry["since"] = now
                entry["severity"] = None
                logger.info("Alert state %s/%s: alert → ok", alert_type, target)
                return "resolved"
            return None

        # unhealthy
        entry["fail_count"] = entry.get("fail_count", 0) + 1
        if entry["status"] == "alert":
            entry["fail_count"] = min(entry["fail_count"], trigger_after_failures)
            # Severity escalation: a rise re-fires immediately; a fall is recorded silently.
            if severity is not None and _severity_rank(severity) != _severity_rank(entry.get("severity")):
                escalated = _severity_rank(severity) > _severity_rank(entry.get("severity"))
                entry["severity"] = severity
                if escalated:
                    entry["since"] = now
                    entry["cycles_in_alert"] = 0
                    logger.info("Alert state %s/%s escalated to %s", alert_type, target, severity)
                    return "triggered"
            # Re-notification cadence while still firing.
            if repeat_after_cycles and repeat_after_cycles > 0:
                entry["cycles_in_alert"] = entry.get("cycles_in_alert", 0) + 1
                if entry["cycles_in_alert"] >= repeat_after_cycles:
                    entry["cycles_in_alert"] = 0
                    logger.info("Alert state %s/%s re-notifying (every %d cycles)", alert_type, target, repeat_after_cycles)
                    return "triggered"
            return None
        if entry["fail_count"] >= trigger_after_failures:
            entry["status"] = "alert"
            entry["since"] = now
            entry["severity"] = severity
            entry["cycles_in_alert"] = 0
            logger.info(
                "Alert state %s/%s: ok → alert (fail_count=%d, threshold=%d)",
                alert_type, target, entry["fail_count"], trigger_after_failures,
            )
            return "triggered"
        return None

    def get_all_alerts(self) -> list[dict]:
        return [
            {
                "type": k[0],
                "target": v.get("display_target", k[1]),
                "status": "alert",
                "since": v["since"],
            }
            for k, v in self._states.items()
            if v["status"] == "alert"
        ]

    def get_all_statuses(self) -> list[dict]:
        return [
            {
                "type": k[0],
                "target": v.get("display_target", k[1]),
                "status": v["status"],
                "since": v["since"] if v["status"] == "alert" else None,
                "severity": v.get("severity") if v["status"] == "alert" else None,
            }
            for k, v in self._states.items()
        ]

    def get_entries(
        self,
        *,
        alert_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (entry_type, target), entry in self._states.items():
            if alert_type is not None and entry_type != alert_type:
                continue
            if status is not None and entry["status"] != status:
                continue
            rows.append({
                "type": entry_type,
                "target": target,
                "status": entry["status"],
                "since": entry["since"],
                "display_target": entry.get("display_target", target),
                "fail_count": entry.get("fail_count", 0),
                "severity": entry.get("severity"),
            })
        return rows

    def discard(self, alert_type: str, target: str) -> None:
        self._states.pop((alert_type, target), None)

    def reset(self) -> None:
        self._states.clear()


def _parse_since(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return utcnow()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def save_alert_state_to_db(
    db: AsyncSession,
    state: AlertStateManager,
    alert_type: str,
    target: str,
) -> None:
    """Persist one current alert-state entry."""
    entry = state.get_entry(alert_type, target)
    if entry is None:
        return

    result = await db.execute(
        select(AlertState).where(
            AlertState.alert_type == alert_type,
            AlertState.target == target,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = AlertState(alert_type=alert_type, target=target)
        db.add(row)

    row.status = entry["status"]
    row.since = _parse_since(entry["since"])
    row.display_target = entry["display_target"]
    row.fail_count = int(entry["fail_count"])
    row.severity = entry.get("severity")
    row.updated_at = utcnow()
    await db.commit()


async def load_alert_state_from_db(
    db: AsyncSession,
    state: AlertStateManager,
) -> None:
    """Load persisted alert-state entries into the in-memory manager."""
    result = await db.execute(select(AlertState).order_by(AlertState.id))
    rows = result.scalars().all()
    state.reset()
    for row in rows:
        since = row.since
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        else:
            since = since.astimezone(timezone.utc)
        state.set_entry(
            row.alert_type,
            row.target,
            status=row.status,
            since=since.isoformat(),
            display_target=row.display_target,
            fail_count=row.fail_count,
            severity=row.severity,
        )


async def delete_alert_state(
    db: AsyncSession,
    alert_type: str,
    target: str,
) -> None:
    """Delete a single persisted alert-state row by (alert_type, target)."""
    result = await db.execute(
        select(AlertState).where(
            AlertState.alert_type == alert_type,
            AlertState.target == target,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        await db.delete(row)


async def purge_stale_states(
    db: AsyncSession,
    state: AlertStateManager,
    *,
    known_db_aliases: set[str],
    known_nas_aliases: set[str],
    known_upstream_ids: set[str] | None,
    known_route_ids: set[str] | None,
    known_host_names: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Drop alert states whose targets no longer exist.

    Pass None for `known_upstream_ids` / `known_route_ids` to skip the
    corresponding alert types when APISIX is unreachable — better to
    leave state alone than to wipe it because of a transient outage.
    Returns the (alert_type, target) pairs that were removed.

    Route states are keyed by plain ``route_id``. Any legacy rule-scoped
    states (``error_rate`` and ``{route}:rule_{id}`` targets from the old
    rule-based model) are treated as stale and purged.
    """
    result = await db.execute(select(AlertState))
    removed: list[tuple[str, str]] = []
    for row in result.scalars().all():
        atype = row.alert_type
        target = row.target
        should_remove = False

        if atype == "db_health":
            should_remove = target not in known_db_aliases
        elif atype == "nas_health":
            should_remove = target not in known_nas_aliases
        elif atype == "upstream_health":
            if known_upstream_ids is not None:
                should_remove = target not in known_upstream_ids
        elif atype == "route_error_rate":
            if ":rule_" in target:
                # Legacy rule-scoped target; the new model keys by route_id.
                should_remove = True
            elif known_route_ids is not None:
                should_remove = target not in known_route_ids
        elif atype.startswith("server_"):
            # Host signals (server_down / server_disk / server_cpu / ...): key by
            # host name. Skip the purge when the registry could not be loaded.
            if known_host_names is not None:
                should_remove = target not in known_host_names
        elif atype == "error_rate":
            # Global error-rate monitoring was removed; drop any leftover state.
            should_remove = True

        if should_remove:
            state.discard(atype, target)
            await db.delete(row)
            removed.append((atype, target))

    if removed:
        await db.commit()
        logger.info(
            "Purged %d stale alert state entr%s at startup",
            len(removed),
            "y" if len(removed) == 1 else "ies",
        )
    return removed
