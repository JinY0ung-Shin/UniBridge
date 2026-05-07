from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_types import utcnow
from app.models import AlertState

logger = logging.getLogger(__name__)


class AlertStateManager:
    """In-memory alert state tracker.

    Tracks (type, target) → status transitions.
    Returns transition type on state change, None if no change.
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
            "alert_notified": entry.get("alert_notified", True),
        }

    def set_entry(
        self,
        alert_type: str,
        target: str,
        *,
        status: str,
        since: str,
        display_target: str | None = None,
        alert_notified: bool = True,
    ) -> None:
        self._states[(alert_type, target)] = {
            "status": status,
            "since": since,
            "display_target": display_target or target,
            "alert_notified": alert_notified,
        }

    def update(
        self, alert_type: str, target: str, *, is_healthy: bool, display_target: str | None = None,
    ) -> str | None:
        """Update state and return transition type if changed.

        Args:
            alert_type: Check type (db_health, upstream_health, error_rate).
            target: Internal state key (may include rule ID for scoping).
            is_healthy: Whether the check passed.
            display_target: Human-readable target name for API display.
                            Defaults to target if not provided.

        Returns:
            "triggered" — transitioned ok → alert
            "resolved"  — transitioned alert → ok
            None        — no change
        """
        key = (alert_type, target)
        current = self._states.get(key)
        new_status = "ok" if is_healthy else "alert"
        now = datetime.now(timezone.utc).isoformat()

        if current is None:
            self._states[key] = {
                "status": new_status,
                "since": now,
                "display_target": display_target or target,
                "alert_notified": is_healthy,
            }
            logger.info("Alert state %s/%s initialized as %s", alert_type, target, new_status)
            return None

        current_status = current["status"]
        if current_status == new_status:
            if new_status == "alert" and not current.get("alert_notified", True):
                current["alert_notified"] = True
                logger.info("Alert state %s/%s: initial alert confirmed", alert_type, target)
                return "triggered"
            return None

        alert_was_notified = current.get("alert_notified", True)
        self._states[key] = {
            "status": new_status,
            "since": now,
            "display_target": display_target or target,
            "alert_notified": True,
        }
        if is_healthy and not alert_was_notified:
            transition = None
        else:
            transition = "resolved" if is_healthy else "triggered"
        logger.info("Alert state %s/%s: %s → %s", alert_type, target, current_status, new_status)
        return transition

    def get_all_alerts(self) -> list[dict]:
        """Return all entries currently in 'alert' status."""
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
        """Return all known entries, including both ok and alert states."""
        return [
            {
                "type": k[0],
                "target": v.get("display_target", k[1]),
                "status": v["status"],
                "since": v["since"] if v["status"] == "alert" else None,
            }
            for k, v in self._states.items()
        ]

    def get_entries(
        self,
        *,
        alert_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return internal state entries for checker coordination."""
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
                "alert_notified": entry.get("alert_notified", True),
            })
        return rows

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
    row.alert_notified = bool(entry["alert_notified"])
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
            alert_notified=row.alert_notified,
        )
