"""Temporary alert-notification suppression (mutes).

A mute never changes detection or state transitions — the checker keeps
evaluating targets and flipping ``AlertState`` exactly as before. It only
withholds outbound delivery, and records that fact on the state row
(``pending_notify``) so an alert that fires while muted still announces itself
once the mute lifts and it is *still* firing.

``resource_type``/``resource_id`` match the pair the checker dispatches with
(``db``/alias, ``route``/route_id, ``server``/host name, …), so a lookup is a
plain dict hit. The pseudo-type ``global`` with an empty ``resource_id`` mutes
everything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AlertMute

logger = logging.getLogger(__name__)

GLOBAL_MUTE_TYPE = "global"

# Resource types a mute may target: the pseudo-type ``global`` plus every
# ``resource_type`` the alert checker dispatches with. Kept in sync with the
# ``dispatch_alert(resource_type=...)`` call sites in ``alert_checker``.
MUTE_RESOURCE_TYPES = frozenset(
    {GLOBAL_MUTE_TYPE, "db", "s3", "nas", "upstream", "route", "server", "service"}
)

# Longest mute a caller may request. A mute is an operational escape hatch, not
# a way to switch monitoring off indefinitely.
MAX_MUTE_DAYS = 30

# Rule identifier (AlertState.alert_type) → the resource_type a mute uses.
# Rules not listed here are matched by prefix below.
_RULE_TO_RESOURCE_TYPE = {
    "db_health": "db",
    "s3_health": "s3",
    "nas_health": "nas",
    "upstream_health": "upstream",
    "route_error_rate": "route",
}


def resource_type_for_rule(rule_type: str) -> str | None:
    """Map a monitoring rule to the resource_type its mute is keyed by.

    Returns None for an unrecognised rule (e.g. a legacy state row), which the
    caller should treat as un-mutable rather than guess at.
    """
    mapped = _RULE_TO_RESOURCE_TYPE.get(rule_type)
    if mapped is not None:
        return mapped
    if rule_type.startswith("external_service_"):
        return "service"
    if rule_type.startswith("server_"):
        return "server"
    return None


@dataclass(frozen=True)
class MuteIndex:
    """Point-in-time snapshot of the active mutes, loaded once per check cycle."""

    global_until: datetime | None = None
    targets: dict[tuple[str, str], datetime] = field(default_factory=dict)

    def muted_until(self, resource_type: str, resource_id: str) -> datetime | None:
        """Latest expiry suppressing this target, or None when it is not muted."""
        candidates = [self.global_until, self.targets.get((resource_type, resource_id))]
        active = [ts for ts in candidates if ts is not None]
        return max(active) if active else None

    def is_muted(self, resource_type: str, resource_id: str) -> bool:
        return self.muted_until(resource_type, resource_id) is not None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_mute_window(muted_until: datetime, *, now: datetime | None = None) -> datetime:
    """Normalize ``muted_until`` to UTC and reject out-of-range windows.

    Raises ``ValueError`` for a past timestamp or one beyond ``MAX_MUTE_DAYS``.
    """
    reference = now or datetime.now(timezone.utc)
    normalized = _as_utc(muted_until)
    if normalized <= reference:
        raise ValueError("muted_until must be in the future")
    if normalized > reference + timedelta(days=MAX_MUTE_DAYS):
        raise ValueError(f"muted_until must be within {MAX_MUTE_DAYS} days")
    return normalized


async def purge_expired_mutes(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Drop mutes whose window has passed. Returns the number removed.

    Called lazily on every read, so the overwhelmingly common case — nothing
    has expired — must stay a pure read: issuing the DELETE unconditionally
    would open a write transaction, and take SQLite's writer lock, on each
    listing.
    """
    reference = now or datetime.now(timezone.utc)
    expired = (
        await db.execute(select(AlertMute.id).where(AlertMute.muted_until <= reference))
    ).scalars().all()
    if not expired:
        return 0
    await db.execute(sa_delete(AlertMute).where(AlertMute.id.in_(expired)))
    await db.commit()
    return len(expired)


async def list_active_mutes(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    purge: bool = True,
) -> list[AlertMute]:
    """Return the still-active mutes, pruning expired rows first."""
    reference = now or datetime.now(timezone.utc)
    if purge:
        await purge_expired_mutes(db, now=reference)
    result = await db.execute(
        select(AlertMute)
        .where(AlertMute.muted_until > reference)
        .order_by(AlertMute.resource_type, AlertMute.resource_id)
    )
    return list(result.scalars().all())


def build_index(mutes: list[AlertMute], *, now: datetime | None = None) -> MuteIndex:
    reference = now or datetime.now(timezone.utc)
    global_until: datetime | None = None
    targets: dict[tuple[str, str], datetime] = {}
    for mute in mutes:
        until = _as_utc(mute.muted_until)
        if until <= reference:
            continue
        if mute.resource_type == GLOBAL_MUTE_TYPE:
            if global_until is None or until > global_until:
                global_until = until
            continue
        targets[(mute.resource_type, mute.resource_id)] = until
    return MuteIndex(global_until=global_until, targets=targets)


async def load_mute_index(*, now: datetime | None = None) -> MuteIndex:
    """Load the active-mute snapshot on the checker's own session.

    A failure here must never stop a check cycle: alerting keeps working (and
    keeps notifying) when the mute table cannot be read.
    """
    from app.database import async_session

    reference = now or datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            result = await db.execute(
                select(AlertMute).where(AlertMute.muted_until > reference)
            )
            mutes = list(result.scalars().all())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load alert mutes; treating all targets as unmuted: %s", exc)
        return MuteIndex()
    return build_index(mutes, now=reference)
