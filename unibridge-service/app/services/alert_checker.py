from __future__ import annotations

import asyncio
import logging
import time
from sqlalchemy import select

from app.database import async_session
from app.models import AlertSettings
from app.services.alert_owner_dispatcher import dispatch_alert
from app.services.alert_state import AlertStateManager, save_alert_state_to_db

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds
_monotonic = time.monotonic

# Route label cache: maps route_id → friendly label (name or uri).
# Refreshed lazily with a TTL to avoid hammering APISIX on every check.
_ROUTE_LABEL_CACHE: dict[str, str] = {}
_ROUTE_LABEL_CACHE_TS: float = 0.0
_ROUTE_LABEL_TTL = 300.0  # 5 minutes
_UPSTREAM_NAME_BY_ID: dict[str, str] = {}


async def _get_check_interval_seconds() -> int:
    try:
        async with async_session() as db:
            result = await db.execute(
                select(AlertSettings.check_interval_seconds).where(AlertSettings.id == 1)
            )
            interval = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning("Failed to load alert check interval: %s", exc)
        return CHECK_INTERVAL
    if interval is None:
        return CHECK_INTERVAL
    return min(3600, max(30, int(interval)))


async def _get_trigger_after_failures() -> int:
    try:
        async with async_session() as db:
            result = await db.execute(
                select(AlertSettings.trigger_after_failures).where(AlertSettings.id == 1)
            )
            value = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning("Failed to load alert trigger_after_failures: %s", exc)
        return 2
    if value is None:
        return 2
    return min(10, max(1, int(value)))


def _normalize_route_error_threshold_pct(value: float | int | None) -> float:
    if value is None:
        return 10.0
    return min(100.0, max(0.0, float(value)))


async def _load_route_error_threshold(db) -> float:
    result = await db.execute(
        select(AlertSettings.route_error_threshold_pct).where(AlertSettings.id == 1)
    )
    return _normalize_route_error_threshold_pct(result.scalar_one_or_none())


async def _refresh_route_labels() -> None:
    """Refresh route_id → label cache from APISIX.

    Updates `_ROUTE_LABEL_CACHE_TS` on both success AND failure so that the
    TTL governs retry cadence; otherwise an APISIX outage would cause every
    `_get_route_label` call to re-enter this function.
    """
    global _ROUTE_LABEL_CACHE, _ROUTE_LABEL_CACHE_TS
    from app.services import apisix_client
    try:
        data = await apisix_client.list_resources("routes")
        new_cache: dict[str, str] = {}
        for item in data.get("items", []):
            rid = str(item.get("id") or "")
            if not rid:
                continue
            name = item.get("name")
            uri = item.get("uri")
            if not uri:
                uris = item.get("uris") or []
                uri = uris[0] if uris else None
            new_cache[rid] = name or uri or rid
        _ROUTE_LABEL_CACHE = new_cache
    except Exception as exc:
        logger.warning("Failed to refresh route labels: %s", exc)
    finally:
        _ROUTE_LABEL_CACHE_TS = _monotonic()


async def _get_route_label(route_id: str) -> str:
    """Return friendly label for route_id, falling back to the id itself.

    Assumes single-caller per cycle (via `run_single_check`). If the checker
    ever becomes reentrant, wrap the refresh in an asyncio.Lock.
    """
    if _monotonic() - _ROUTE_LABEL_CACHE_TS > _ROUTE_LABEL_TTL:
        await _refresh_route_labels()
    return _ROUTE_LABEL_CACHE.get(route_id, route_id)


async def _check_db_health() -> list[tuple[str, bool]]:
    """Check all registered DB connections. Returns [(alias, is_healthy)]."""
    from app.services.connection_manager import connection_manager
    results = []
    for alias in connection_manager.list_aliases():
        try:
            ok, _ = await connection_manager.test_connection(alias)
            results.append((alias, ok))
        except Exception as exc:
            logger.warning("DB health check failed for '%s': %s", alias, exc)
            results.append((alias, False))
    return results


async def _check_upstream_health() -> list[tuple[str, bool]]:
    """Check APISIX upstream health. Returns [(upstream_id, is_healthy)]."""
    global _UPSTREAM_NAME_BY_ID
    from app.services import apisix_client
    results = []
    try:
        data = await apisix_client.list_resources("upstreams")
        names: dict[str, str] = {}
        for item in data.get("items", []):
            uid = item.get("id", "unknown")
            uid_str = str(uid)
            name = item.get("name")
            if name:
                names[uid_str] = str(name)
            nodes = item.get("nodes", {})
            is_healthy = bool(nodes) and any(
                w > 0 for w in (nodes.values() if isinstance(nodes, dict) else [])
            )
            results.append((uid_str, is_healthy))
        _UPSTREAM_NAME_BY_ID = names
    except Exception as exc:
        logger.warning("Upstream health check failed: %s", exc)
    return results


async def _check_route_error_rate() -> list[tuple[str, float]] | None:
    """Check 5xx error rate per APISIX route.

    Returns [(route_id, rate_pct), ...] for every route that has traffic
    in the last 5 minutes. Routes with 0 errors are included with rate=0
    so that resolved transitions are detected correctly.
    """
    from app.services import prometheus_client
    try:
        total_results = await prometheus_client.instant_query(
            'sum by (route) (rate(apisix_http_status[5m]))'
        )
        if not total_results:
            return []
        err_results = await prometheus_client.instant_query(
            'sum by (route) (rate(apisix_http_status{code=~"5.."}[5m]))'
        )
    except Exception as exc:
        logger.warning("Route error rate check failed: %s", exc)
        return None

    err_map: dict[str, float] = {}
    for item in err_results:
        rid = item.get("metric", {}).get("route")
        if not rid:
            continue
        try:
            val = float(item.get("value", [0, 0])[1])
        except (TypeError, ValueError):
            continue
        if val != val:  # NaN
            continue
        err_map[rid] = val

    route_rates: list[tuple[str, float]] = []
    for item in total_results:
        rid = item.get("metric", {}).get("route")
        if not rid:
            continue
        try:
            total = float(item.get("value", [0, 0])[1])
        except (TypeError, ValueError):
            continue
        if total <= 0 or total != total:  # skip no-traffic / NaN
            continue
        err = err_map.get(rid, 0.0)
        pct = (err / total) * 100
        if pct != pct:
            pct = 0.0
        route_rates.append((str(rid), pct))
    return route_rates


async def _persist_state_safely(
    state: AlertStateManager,
    alert_type: str,
    target: str,
) -> None:
    try:
        async with async_session() as db:
            await save_alert_state_to_db(db, state, alert_type, target)
    except Exception as exc:
        logger.warning("Failed to persist alert state %s/%s: %s", alert_type, target, exc)


async def _evaluate_route_error_rule(
    state: AlertStateManager,
    *,
    route_id: str,
    rate: float,
    threshold: float,
    trigger_after_failures: int,
    display_target: str | None = None,
) -> None:
    if display_target is None:
        label = await _get_route_label(route_id)
        display = f"{label} ({route_id})" if label != route_id else route_id
    else:
        display = display_target

    is_healthy = rate < threshold
    transition = state.update(
        "route_error_rate",
        route_id,
        is_healthy=is_healthy,
        display_target=display,
        trigger_after_failures=trigger_after_failures,
    )
    await _persist_state_safely(state, "route_error_rate", route_id)
    if transition:
        msg = (
            f"Route '{display}' 5xx error rate is "
            f"{rate:.1f}% (threshold: {threshold}%)."
        )
        await dispatch_alert(
            resource_type="route", resource_id=route_id,
            alert_type=transition, target=route_id, message=msg,
            display_target=display,
            rate=rate, threshold=threshold,
            monitor_label="라우트 에러율",
        )


async def run_single_check(state: AlertStateManager, *, trigger_after_failures: int) -> None:
    """Execute one round of all health checks."""
    # 1. DB health
    db_results = await _check_db_health()
    for alias, is_healthy in db_results:
        transition = state.update(
            "db_health", alias,
            is_healthy=is_healthy,
            trigger_after_failures=trigger_after_failures,
        )
        await _persist_state_safely(state, "db_health", alias)
        if transition:
            msg = f"Database '{alias}' connection {'restored' if transition == 'resolved' else 'failed'}."
            await dispatch_alert(
                resource_type="db", resource_id=alias,
                alert_type=transition, target=alias, message=msg,
                display_target=alias, monitor_label="DB 헬스체크",
            )

    # 2. Upstream health
    upstream_results = await _check_upstream_health()
    for uid, is_healthy in upstream_results:
        upstream_name = _UPSTREAM_NAME_BY_ID.get(uid)
        display = f"{upstream_name} ({uid})" if upstream_name and upstream_name != uid else uid
        transition = state.update(
            "upstream_health", uid,
            is_healthy=is_healthy,
            display_target=display,
            trigger_after_failures=trigger_after_failures,
        )
        await _persist_state_safely(state, "upstream_health", uid)
        if transition:
            msg = f"Upstream '{display}' {'recovered' if transition == 'resolved' else 'is down'}."
            await dispatch_alert(
                resource_type="upstream", resource_id=uid,
                alert_type=transition, target=uid, message=msg,
                display_target=display, monitor_label="업스트림 헬스체크",
            )

    # 3. Route-level error rate (automatic for every route; global threshold)
    route_results = await _check_route_error_rate()
    if route_results is None:
        return

    active_route_alerts = state.get_entries(alert_type="route_error_rate", status="alert")
    if not route_results and not active_route_alerts:
        return

    async with async_session() as db:
        route_threshold = await _load_route_error_threshold(db)

    route_rate_by_id = dict(route_results)
    processed: set[str] = set()
    for route_id, rate in route_results:
        processed.add(route_id)
        await _evaluate_route_error_rule(
            state,
            route_id=route_id,
            rate=rate,
            threshold=route_threshold,
            trigger_after_failures=trigger_after_failures,
        )

    # Routes that were alerting but no longer report traffic → resolve at rate 0.
    for entry in active_route_alerts:
        route_id = entry["target"]
        if route_id in processed or route_id in route_rate_by_id:
            continue
        await _evaluate_route_error_rule(
            state,
            route_id=route_id,
            rate=0.0,
            threshold=route_threshold,
            trigger_after_failures=trigger_after_failures,
            display_target=entry.get("display_target"),
        )


async def start_checker(state: AlertStateManager) -> asyncio.Task:
    """Start the periodic health check loop as a background task."""
    async def _loop():
        logger.info("Alert checker started")
        while True:
            cycle_start = _monotonic()
            check_interval = await _get_check_interval_seconds()
            trigger_after_failures = await _get_trigger_after_failures()
            try:
                await run_single_check(state, trigger_after_failures=trigger_after_failures)
            except Exception:
                logger.exception("Alert checker cycle failed")
            elapsed = _monotonic() - cycle_start
            await asyncio.sleep(max(0.0, check_interval - elapsed))

    return asyncio.create_task(_loop())
