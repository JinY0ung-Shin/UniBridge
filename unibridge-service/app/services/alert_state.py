from __future__ import annotations

import logging
from datetime import datetime, timezone

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

    def reset(self) -> None:
        self._states.clear()
