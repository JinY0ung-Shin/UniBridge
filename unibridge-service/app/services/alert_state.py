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

    def update(self, alert_type: str, target: str, *, is_healthy: bool) -> str | None:
        """Update state and return transition type if changed.

        Returns:
            "triggered" — transitioned ok → alert
            "resolved"  — transitioned alert → ok
            None        — no change
        """
        key = (alert_type, target)
        current = self._states.get(key)
        current_status = current["status"] if current else "ok"
        new_status = "ok" if is_healthy else "alert"

        if current_status == new_status:
            return None

        now = datetime.now(timezone.utc).isoformat()
        self._states[key] = {"status": new_status, "since": now}
        transition = "resolved" if is_healthy else "triggered"
        logger.info("Alert state %s/%s: %s → %s", alert_type, target, current_status, new_status)
        return transition

    def get_all_alerts(self) -> list[dict]:
        """Return all entries currently in 'alert' status."""
        return [
            {"type": k[0], "target": k[1], "status": "alert", "since": v["since"]}
            for k, v in self._states.items()
            if v["status"] == "alert"
        ]

    def reset(self) -> None:
        self._states.clear()
