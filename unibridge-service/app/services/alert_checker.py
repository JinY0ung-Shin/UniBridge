from __future__ import annotations

import asyncio
import logging
import time
from sqlalchemy import select, text

from app.database import async_session, engine
from app.models import AlertSettings, MonitoredHost
from app.services import server_monitor
from app.services.alert_owner_dispatcher import dispatch_alert
from app.services.alert_state import AlertStateManager, save_alert_state_to_db
from app.services.server_monitor import ServerThresholds

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


async def _load_route_error_settings(db) -> tuple[float, int]:
    """Return (threshold_pct, min_requests) for route 5xx alerting."""
    result = await db.execute(
        select(
            AlertSettings.route_error_threshold_pct,
            AlertSettings.route_error_min_requests,
        ).where(AlertSettings.id == 1)
    )
    row = result.one_or_none()
    if row is None:
        return 10.0, 20
    threshold = _normalize_route_error_threshold_pct(row[0])
    min_requests = 0 if row[1] is None else max(0, int(row[1]))
    return threshold, min_requests


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


async def _check_nas_health() -> list[tuple[str, bool]]:
    """Check registered NAS connections. Returns [(alias, is_healthy)]."""
    from app.services.nas_manager import nas_manager
    results = []
    for alias in nas_manager.list_aliases():
        try:
            ok, _ = await nas_manager.test_connection(alias)
            results.append((alias, ok))
        except Exception as exc:
            logger.warning("NAS health check failed for '%s': %s", alias, exc)
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


async def _check_route_error_rate() -> list[tuple[str, float, float]] | None:
    """Check 5xx error rate per APISIX route.

    Returns [(route_id, rate_pct, sample_count), ...] for every route that has
    traffic in the last 5 minutes, where ``sample_count`` is the approximate
    number of requests over the window (used to suppress alerts on low-traffic
    routes). Routes with 0 errors are included with rate=0 so that resolved
    transitions are detected correctly.

    Uses ``increase()`` rather than ``rate()`` so the denominator is a request
    count; the error ratio is identical either way.
    """
    from app.services import prometheus_client
    try:
        total_results = await prometheus_client.instant_query(
            'sum by (route) (increase(apisix_http_status[5m]))'
        )
        if not total_results:
            return []
        err_results = await prometheus_client.instant_query(
            'sum by (route) (increase(apisix_http_status{code=~"5.."}[5m]))'
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

    route_rates: list[tuple[str, float, float]] = []
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
        route_rates.append((str(rid), pct, total))
    return route_rates


async def _load_server_monitoring() -> tuple[list[MonitoredHost], ServerThresholds, int]:
    """Load enabled monitored hosts, global server thresholds, and re-notify cadence."""
    async with async_session() as db:
        settings_row = (
            await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
        ).scalar_one_or_none()
        hosts = list((await db.execute(select(MonitoredHost))).scalars().all())

    if settings_row is None:
        return hosts, ServerThresholds(), 0
    thresholds = ServerThresholds(
        disk_warn_pct=settings_row.server_disk_warn_pct,
        disk_crit_pct=settings_row.server_disk_crit_pct,
        cpu_warn_pct=settings_row.server_cpu_warn_pct,
        mem_warn_pct=settings_row.server_mem_warn_pct,
        forecast_hours=settings_row.server_disk_forecast_hours,
    )
    repeat = int(settings_row.repeat_alert_after_cycles or 0)
    return hosts, thresholds, repeat


async def _check_server_health(
    state: AlertStateManager,
    *,
    trigger_after_failures: int,
) -> None:
    """Evaluate node_exporter host signals and dispatch transitions.

    Reuses the shared state machine + dispatch pipeline: each signal is one
    (alert_type, host) binary state, with warn/critical severity escalation and
    optional re-notification handled by AlertStateManager.

    A failure to load the registry/thresholds is isolated to this step so it
    can never abort the DB/NAS/upstream/route checks in the same cycle.
    """
    try:
        hosts, thresholds, repeat = await _load_server_monitoring()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Server health check skipped (config load failed): %s", exc)
        return
    enabled = [h for h in hosts if getattr(h, "enabled", False)]
    if not enabled:
        return
    signals = await server_monitor.evaluate_hosts(enabled, thresholds)
    for sig in signals:
        transition = state.update(
            sig.alert_type, sig.target,
            is_healthy=sig.is_healthy,
            display_target=sig.display,
            severity=sig.severity,
            trigger_after_failures=trigger_after_failures,
            repeat_after_cycles=repeat,
        )
        await _persist_state_safely(state, sig.alert_type, sig.target)
        if transition:
            await dispatch_alert(
                resource_type="server", resource_id=sig.target,
                alert_type=transition, target=sig.target, message=sig.message,
                display_target=sig.display, rate=sig.value, threshold=sig.threshold,
                monitor_label=sig.monitor_label, severity=sig.severity,
            )


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
    sample_count: float = 0.0,
    min_requests: int = 0,
    display_target: str | None = None,
) -> None:
    if display_target is None:
        label = await _get_route_label(route_id)
        display = f"{label} ({route_id})" if label != route_id else route_id
    else:
        display = display_target

    # Routes below the minimum request floor are treated as healthy: too little
    # traffic to judge, so they never trigger and any active alert resolves.
    if sample_count < min_requests:
        is_healthy = True
    else:
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

    # 2. NAS connection health
    nas_results = await _check_nas_health()
    for alias, is_healthy in nas_results:
        transition = state.update(
            "nas_health", alias,
            is_healthy=is_healthy,
            trigger_after_failures=trigger_after_failures,
        )
        await _persist_state_safely(state, "nas_health", alias)
        if transition:
            msg = f"NAS connection '{alias}' {'restored' if transition == 'resolved' else 'is unavailable'}."
            await dispatch_alert(
                resource_type="nas", resource_id=alias,
                alert_type=transition, target=alias, message=msg,
                display_target=alias, monitor_label="NAS 연결 상태",
            )

    # 3. Upstream health
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

    # 4. Server (host) health via node_exporter metrics
    await _check_server_health(state, trigger_after_failures=trigger_after_failures)

    # 5. Route-level error rate (automatic for every route; global threshold)
    route_results = await _check_route_error_rate()
    if route_results is None:
        return

    active_route_alerts = state.get_entries(alert_type="route_error_rate", status="alert")
    if not route_results and not active_route_alerts:
        return

    async with async_session() as db:
        route_threshold, route_min_requests = await _load_route_error_settings(db)

    processed: set[str] = set()
    for route_id, rate, sample_count in route_results:
        processed.add(route_id)
        await _evaluate_route_error_rule(
            state,
            route_id=route_id,
            rate=rate,
            threshold=route_threshold,
            trigger_after_failures=trigger_after_failures,
            sample_count=sample_count,
            min_requests=route_min_requests,
        )

    # Routes that were alerting but no longer report traffic → resolve at rate 0.
    for entry in active_route_alerts:
        route_id = entry["target"]
        if route_id in processed:
            continue
        await _evaluate_route_error_rule(
            state,
            route_id=route_id,
            rate=0.0,
            threshold=route_threshold,
            trigger_after_failures=trigger_after_failures,
            sample_count=0.0,
            min_requests=route_min_requests,
            display_target=entry.get("display_target"),
        )


# Postgres advisory-lock key used to elect a single alert-checker leader across
# blue/green colors. Both colors run this background task, but only the holder
# of this lock evaluates rules and dispatches alerts — otherwise every webhook /
# email fires twice (once per live color) for the whole window both colors run.
# Arbitrary stable constant within int64.
_ALERT_LEADER_LOCK_KEY = 0x554E49425247  # "UNIBRG"


async def _acquire_leadership() -> "object | None":
    """Try to become the sole alert-checker leader.

    Returns a held resource on success, or None if another instance is leader.

    - Postgres (blue/green): grabs a session-level ``pg_try_advisory_lock`` on a
      dedicated connection and returns that connection. The lock is held for the
      connection's lifetime and auto-released when it closes (clean shutdown, or
      the container dying — which is exactly when the surviving color should take
      over).
    - Anything else (e.g. single-stack SQLite, tests): no cross-process sharing,
      so this instance always leads; returns a sentinel.
    """
    if engine.dialect.name != "postgresql":
        return _SINGLE_STACK_LEADER

    conn = await engine.connect()
    try:
        got = (
            await conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _ALERT_LEADER_LOCK_KEY},
            )
        ).scalar()
    except Exception:
        await conn.close()
        raise
    if got:
        return conn
    await conn.close()
    return None


_SINGLE_STACK_LEADER = object()


async def start_checker(state: AlertStateManager) -> asyncio.Task:
    """Start the periodic health check loop as a background task.

    Across blue/green colors only one instance (the advisory-lock leader) runs
    the loop; the others stand by and retry acquiring leadership each interval so
    a surviving color takes over when the current leader shuts down.
    """
    async def _loop():
        leader: object | None = None
        try:
            # Elect a leader before doing any work; stand by until we win.
            while leader is None:
                try:
                    leader = await _acquire_leadership()
                except Exception as exc:
                    logger.warning("Alert checker leader election failed: %s", exc)
                if leader is None:
                    logger.info(
                        "Alert checker standing by (another color holds leadership)"
                    )
                    await asyncio.sleep(await _get_check_interval_seconds())

            logger.info("Alert checker started (leader)")
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
        finally:
            # Release the advisory lock on shutdown so the surviving color can
            # take over promptly (sentinel leader holds nothing to close).
            if leader is not None and leader is not _SINGLE_STACK_LEADER:
                try:
                    await leader.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    return asyncio.create_task(_loop())
