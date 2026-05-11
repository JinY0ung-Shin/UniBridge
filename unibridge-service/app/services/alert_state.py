from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_types import utcnow
from app.models import AlertState

logger = logging.getLogger(__name__)

_RULE_SCOPED_ALERT_TYPES = {"error_rate", "route_error_rate"}


def _rule_state_suffix(rule_id: int) -> str:
    return f":rule_{rule_id}"


class AlertStateManager:
    """In-memory alert state tracker.

    Each entry: status ∈ {"ok", "alert"}, fail_count is the consecutive
    failure tally, since is the timestamp of the most recent status flip.
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
    ) -> None:
        self._states[(alert_type, target)] = {
            "status": status,
            "since": since,
            "display_target": display_target or target,
            "fail_count": fail_count,
        }

    def update(
        self,
        alert_type: str,
        target: str,
        *,
        is_healthy: bool,
        trigger_after_failures: int,
        display_target: str | None = None,
    ) -> str | None:
        """Update state and return transition type if changed.

        Returns "triggered" / "resolved" / None. fail_count drives status:
        flip to "alert" only when fail_count reaches trigger_after_failures.
        """
        key = (alert_type, target)
        now = datetime.now(timezone.utc).isoformat()
        entry = self._states.get(key)

        if entry is None:
            if is_healthy:
                self._states[key] = {
                    "status": "ok",
                    "since": now,
                    "display_target": display_target or target,
                    "fail_count": 0,
                }
                logger.info("Alert state %s/%s initialized as ok", alert_type, target)
                return None
            fail_count = 1
            if fail_count >= trigger_after_failures:
                self._states[key] = {
                    "status": "alert",
                    "since": now,
                    "display_target": display_target or target,
                    "fail_count": fail_count,
                }
                logger.info("Alert state %s/%s initialized as alert", alert_type, target)
                return "triggered"
            self._states[key] = {
                "status": "ok",
                "since": now,
                "display_target": display_target or target,
                "fail_count": fail_count,
            }
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
            if was_alert:
                entry["status"] = "ok"
                entry["since"] = now
                logger.info("Alert state %s/%s: alert → ok", alert_type, target)
                return "resolved"
            return None

        # unhealthy
        entry["fail_count"] = entry.get("fail_count", 0) + 1
        if entry["status"] == "alert":
            entry["fail_count"] = min(entry["fail_count"], trigger_after_failures)
            return None
        if entry["fail_count"] >= trigger_after_failures:
            entry["status"] = "alert"
            entry["since"] = now
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
            })
        return rows

    def clear_rule_states(self, rule_id: int) -> None:
        suffix = _rule_state_suffix(rule_id)
        for key in list(self._states):
            alert_type, target = key
            if alert_type in _RULE_SCOPED_ALERT_TYPES and target.endswith(suffix):
                del self._states[key]

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
        )


async def delete_alert_states_for_rule(db: AsyncSession, rule_id: int) -> None:
    """Delete persisted states whose key is scoped to an alert rule."""
    suffix = _rule_state_suffix(rule_id)
    result = await db.execute(
        select(AlertState).where(AlertState.alert_type.in_(_RULE_SCOPED_ALERT_TYPES))
    )
    for row in result.scalars().all():
        if row.target.endswith(suffix):
            await db.delete(row)
