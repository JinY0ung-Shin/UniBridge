"""Reconcile APISIX ``consumer-restriction`` whitelists with the API-key store.

Per-key route grants live in ``api_key_access`` and are enforced at the gateway
by the ``consumer-restriction`` plugin on every ``key-auth`` route. Expiry
(``expires_at``) used to be enforced only by the app's own header auth
(:mod:`app.auth`), so the LLM gateway routes — which go APISIX → llm-converter
→ LiteLLM and never reach the app at all — kept honouring an expired key
forever. Self-service keys carry a 30-day TTL, so that gap made a real product
rule unenforceable on exactly the routes it mattered most for.

This module closes it by reconciling every key-auth route's whitelist against
the database: once at boot (the replay driven by :mod:`app.main`) and then on a
timer on the active blue/green color. An expired key is removed from every
whitelist, so APISIX rejects it with 403 before the request leaves the gateway.

The consumer itself is deliberately never deleted. The APISIX consumer holds the
only copy of the key VALUE — ``/admin/api-keys/me/renew`` keeps that value alive
and only moves ``expires_at`` — so deleting it on expiry would silently turn
every renewal into a re-issuance.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKeyAccess
from app.services import apisix_client
from app.services.active_color import is_active_instance

logger = logging.getLogger(__name__)

# Sentinel consumer kept in a whitelist that would otherwise be empty. APISIX
# treats a missing/empty ``consumer-restriction`` as "any authenticated
# consumer", so the plugin needs a name nobody can ever own to mean "nobody".
DENY_ALL_CONSUMER = "__deny_all__"

# Wildcard stored in ``allowed_databases``/``allowed_routes`` for master keys.
MASTER_ACCESS = "*"

# Routes whose access is implied by another route's grant.
#
# ``llm-messages`` / ``llm-responses`` / ``llm-models`` are exact paths carved
# out of the ``/api/llm/*`` namespace and served by the converter. Granting
# ``llm-proxy`` implicitly grants them so existing stored keys keep working
# when one of these routes rolls out — no data migration, no UI change.
# Nothing is disclosed by doing so: a key that can already invoke every model
# through the catch-all learns nothing from their names.
# One-directional: granting only a converter route does NOT widen access to
# the raw ``/api/llm/*`` proxy — so a discovery-only key is still possible by
# granting ``llm-models`` alone.
# Deliberately NOT in this set: ``llm-metrics``. LiteLLM's raw exposition
# carries every key's usage, so implying it from ``llm-proxy`` would hand one
# tenant another tenant's traffic — that one IS a disclosure and stays
# explicit.
IMPLIED_ROUTES: dict[str, frozenset[str]] = {
    "llm-proxy": frozenset({"llm-messages", "llm-responses", "llm-models"}),
}

# Reconcile cadence. Five minutes bounds how long an expired key can keep
# hitting the gateway; thirty seconds keeps the first pass clear of the boot
# replay that has just written the same whitelists.
RECONCILE_INTERVAL_SECONDS = 300
RECONCILE_FIRST_DELAY_SECONDS = 30


def decode_json_list(value: str | None) -> list[str]:
    """Decode a stored JSON string array. Raises on anything else."""
    if not value:
        return []
    decoded = json.loads(value)
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) for item in decoded
    ):
        raise ValueError("Expected JSON string list")
    return decoded


def expand_allowed_routes(allowed_routes: Iterable[str]) -> set[str]:
    """Return the stored grants plus every route they imply."""
    expanded = set(allowed_routes)
    for granted, implied in IMPLIED_ROUTES.items():
        if granted in expanded:
            expanded.update(implied)
    return expanded


def grants_route(expanded_routes: set[str], route_id: str) -> bool:
    """True when an already-expanded grant set covers ``route_id``."""
    return MASTER_ACCESS in expanded_routes or route_id in expanded_routes


def is_expired(access: ApiKeyAccess, now: datetime | None = None) -> bool:
    """True when the key's TTL has elapsed. ``expires_at is None`` never expires.

    Naive values (legacy rows, or objects built in memory) are read as UTC, the
    same contract :class:`app.db_types.UtcDateTime` applies on load.
    """
    expires_at = access.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= (now or datetime.now(timezone.utc))


def effective_allowed_routes(
    access: ApiKeyAccess, now: datetime | None = None
) -> list[str]:
    """Route grants that should be honoured right now — empty once expired.

    Propagates ``json.JSONDecodeError``/``ValueError`` so the caller can decide
    what a malformed row means rather than silently reading it as "no access".
    """
    if is_expired(access, now):
        return []
    return decode_json_list(access.allowed_routes)


@dataclass
class ReconcileResult:
    """What one reconcile pass changed."""

    routes_changed: list[str] = field(default_factory=list)
    revoked_consumers: set[str] = field(default_factory=set)
    skipped_malformed: list[str] = field(default_factory=list)
    failed_routes: list[str] = field(default_factory=list)


class ReconcileError(RuntimeError):
    """At least one route PUT failed. Carries the partial :class:`ReconcileResult`."""

    def __init__(self, message: str, result: ReconcileResult) -> None:
        super().__init__(message)
        self.result = result


async def reconcile_consumer_route_restrictions(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    client: Any = None,
) -> ReconcileResult:
    """Make every key-auth route's whitelist match the API-key database.

    One ``list_resources`` call, then a PUT only for the routes whose whitelist
    actually changes — so a steady state costs one read and nothing else, and an
    install with no API keys costs not even that.

    Two rules keep this safe to run against a shared APISIX:

    - Consumers the database has never heard of are left alone. Route
      provisioning and hand-edits can add names this app does not own, and a
      reconcile that "cleaned" them would break them on every cycle.
    - A row whose ``allowed_routes`` will not decode is reported, not applied.
      Reading it as "no access" would revoke a working key over a storage bug,
      so its current whitelist membership is preserved untouched. Expiry still
      wins over a decode failure: revoking an expired key needs no grants.

    ``client`` overrides the APISIX client module (used by the router wrapper so
    the existing per-router test patches keep applying).

    Raises :class:`ReconcileError` (a ``RuntimeError``) if any PUT failed, after
    attempting every other route — the boot replay retries on exception and the
    periodic loop logs and waits for the next cycle.
    """
    apisix = client if client is not None else apisix_client
    reference = now or datetime.now(timezone.utc)

    rows = (
        await db.execute(
            select(ApiKeyAccess).order_by(ApiKeyAccess.consumer_name.asc())
        )
    ).scalars().all()

    result = ReconcileResult()
    if not rows:
        # No keys, nothing this app owns on any whitelist — so there is nothing
        # to grant and nothing to revoke. Returning before the route listing
        # keeps a fresh install (and its boot replay, which fails startup on an
        # exception) from depending on APISIX being reachable at all.
        return result

    # consumer name → route ids it should be whitelisted on (empty when expired)
    known: dict[str, set[str]] = {}
    expired_consumers: set[str] = set()
    for access in rows:
        try:
            allowed_routes = effective_allowed_routes(access, reference)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "Skipping malformed allowed_routes for consumer '%s' during "
                "consumer-restriction reconcile",
                access.consumer_name,
            )
            result.skipped_malformed.append(access.consumer_name)
            continue
        known[access.consumer_name] = expand_allowed_routes(allowed_routes)
        if is_expired(access, reference):
            # Tracked separately so the log can name who actually lost access,
            # not merely which routes were rewritten.
            expired_consumers.add(access.consumer_name)

    route_data = await apisix.list_resources("routes")

    for route in route_data.get("items", []):
        route_id = route.get("id")
        if not route_id:
            continue
        plugins = route.get("plugins", {})
        if not isinstance(plugins, dict) or "key-auth" not in plugins:
            continue

        restriction = plugins.get("consumer-restriction")
        raw_whitelist = (
            restriction.get("whitelist", []) if isinstance(restriction, dict) else []
        )
        stored = [name for name in raw_whitelist if isinstance(name, str)]
        current = set(stored) - {DENY_ALL_CONSUMER}

        desired = {
            name for name, routes in known.items() if grants_route(routes, route_id)
        }
        # Keep names this app does not manage; replace the ones it does.
        new_whitelist = (current - set(known)) | desired
        if not new_whitelist:
            new_whitelist = {DENY_ALL_CONSUMER}

        if sorted(new_whitelist) == sorted(stored):
            continue

        new_plugins = dict(plugins)
        new_plugins["consumer-restriction"] = {"whitelist": sorted(new_whitelist)}
        new_body = {
            k: v
            for k, v in route.items()
            if k not in ("id", "create_time", "update_time")
        }
        new_body["plugins"] = new_plugins

        try:
            await apisix.put_resource("routes", route_id, new_body)
        except Exception as exc:
            logger.error(
                "Consumer-restriction reconcile failed for route %s: %s",
                route_id,
                exc,
            )
            result.failed_routes.append(route_id)
            continue

        result.routes_changed.append(route_id)
        result.revoked_consumers.update(current & expired_consumers)

    if result.revoked_consumers:
        logger.info(
            "Revoked gateway access for %d expired API key(s) on %d route(s): "
            "consumers=%s routes=%s",
            len(result.revoked_consumers),
            len(result.routes_changed),
            ", ".join(sorted(result.revoked_consumers)),
            ", ".join(result.routes_changed),
        )
    elif result.routes_changed:
        logger.info(
            "Consumer-restriction reconcile updated %d route(s): %s",
            len(result.routes_changed),
            ", ".join(result.routes_changed),
        )

    if result.failed_routes:
        raise ReconcileError(
            "Consumer-restriction reconcile could not update "
            f"{len(result.failed_routes)} route(s): "
            f"{', '.join(result.failed_routes)}",
            result,
        )

    return result


async def run_expiry_reconcile_once() -> ReconcileResult:
    """One reconcile pass against the meta database."""
    from app.database import async_session

    async with async_session() as db:
        return await reconcile_consumer_route_restrictions(db)


async def run_expiry_reconcile_loop(
    *,
    interval_seconds: int = RECONCILE_INTERVAL_SECONDS,
    first_delay_seconds: int = RECONCILE_FIRST_DELAY_SECONDS,
) -> None:
    """Background loop: reconcile shortly after boot, then every 5 minutes.

    Only the active blue/green color reconciles. Both colors share one meta DB
    and one APISIX, so an ungated standby would write the same whitelists twice
    for no benefit. The check is per cycle, not once at startup: a promote or
    rollback rewrites the APISIX upstream without restarting containers, so
    ownership has to be able to move on the next tick.

    Every failure is swallowed so the task outlives a transient APISIX or
    database problem — a missed pass is caught by the next one.
    """
    logger.info("API key expiry reconcile started")
    await asyncio.sleep(first_delay_seconds)
    while True:
        try:
            if await is_active_instance():
                await run_expiry_reconcile_once()
        except Exception:
            logger.exception("API key expiry reconcile cycle failed")
        await asyncio.sleep(interval_seconds)
