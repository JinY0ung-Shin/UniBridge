from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from sqlalchemy import select

from app import metrics
from app.config import settings
from app.database import async_session
from app.models import AlertSettings, MonitoredHost, MonitoredService
from app.services import server_monitor
from app.services.active_color import is_active_instance
from app.services.alert_mutes import MuteIndex, load_mute_index
from app.services.alert_owner_dispatcher import dispatch_alert
from app.services.alert_state import AlertStateManager, save_alert_state_to_db
from app.services.apisix_client import upstream_node_addresses
from app.services.gpu_report import maybe_send_gpu_util_report
from app.services.server_monitor import ServerThresholds

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # seconds
_monotonic = time.monotonic

# Route label cache: maps route_id → friendly label (name or uri), plus the
# reverse (unambiguous route name → route_id) because with APISIX prefer_name
# the Prometheus ``route`` label carries the name while alert state and
# per-resource recipients stay keyed by route id.
# Refreshed lazily with a TTL to avoid hammering APISIX on every check.
_ROUTE_LABEL_CACHE: dict[str, str] = {}
_ROUTE_ID_BY_NAME: dict[str, str] = {}
_ROUTE_LABEL_CACHE_TS: float = 0.0
_ROUTE_LABEL_TTL = 300.0  # 5 minutes
_UPSTREAM_NAME_BY_ID: dict[str, str] = {}

# Upstream probe budget. The same 5s ceiling the gateway's route test uses, and
# a cap on how much work one cycle may spend: at most 3 nodes per upstream and
# 8 in-flight requests overall, so a fleet of dead upstreams still finishes well
# inside the (>=30s) check interval instead of stalling the whole cycle.
_UPSTREAM_PROBE_TIMEOUT = 5.0
_MAX_PROBED_NODES_PER_UPSTREAM = 3
_MAX_CONCURRENT_NODE_PROBES = 8

# Per-probe timeout for the connection health checks (DB, NAS) comes from
# settings.DB_HEALTHCHECK_TIMEOUT_SECONDS. Without it a single unresponsive
# backend blocks the whole alert cycle indefinitely, so no other alert fires
# while one DB hangs. The one setting bounds both check kinds; the probes also
# run concurrently under this cap so N dead backends finish in ~one timeout, not
# N. asyncio.wait_for hands control back on timeout even when the underlying
# blocking call keeps running in a worker thread, so the cycle proceeds
# regardless.
_MAX_CONCURRENT_HEALTH_PROBES = 8

# Failure cause a health probe can report, so the outbound mail can say a target
# "timed out" rather than merely "failed". None means healthy or a plain failure.
_REASON_TIMEOUT = "timeout"


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
    global _ROUTE_LABEL_CACHE, _ROUTE_ID_BY_NAME, _ROUTE_LABEL_CACHE_TS
    from app.services import apisix_client
    try:
        data = await apisix_client.list_resources("routes")
        items = data.get("items", [])
        new_cache: dict[str, str] = {}
        for item in items:
            rid = str(item.get("id") or "")
            if not rid:
                continue
            name = item.get("name")
            uri = item.get("uri")
            if not uri:
                uris = item.get("uris") or []
                uri = uris[0] if uris else None
            new_cache[rid] = name or uri or rid
        # Reverse map: only names that identify exactly one route, and that
        # don't collide with a real route id (an id-shaped value must keep
        # resolving to itself).
        id_by_name: dict[str, str] = {}
        ambiguous: set[str] = set()
        for item in items:
            rid = str(item.get("id") or "")
            name = str(item.get("name") or "")
            if not rid or not name or name == rid or name in new_cache:
                continue
            if name in id_by_name and id_by_name[name] != rid:
                ambiguous.add(name)
                continue
            id_by_name[name] = rid
        for name in ambiguous:
            id_by_name.pop(name, None)
        _ROUTE_LABEL_CACHE = new_cache
        _ROUTE_ID_BY_NAME = id_by_name
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


async def _resolve_route_id(label_value: str) -> str:
    """Map a Prometheus ``route`` label value back to a route id.

    Under APISIX ``prefer_name`` the label carries the route name when one is
    set. Alert state keys and per-resource recipient lookups stay keyed by
    route id, so translate when the value is a known unambiguous route name;
    ids and unknown values pass through unchanged.
    """
    if _monotonic() - _ROUTE_LABEL_CACHE_TS > _ROUTE_LABEL_TTL:
        await _refresh_route_labels()
    if label_value in _ROUTE_LABEL_CACHE:
        return label_value
    return _ROUTE_ID_BY_NAME.get(label_value, label_value)


async def _probe_health_bounded(
    label: str,
    aliases: list[str],
    probe: Callable[[str], Awaitable[tuple[bool, str]]],
) -> list[tuple[str, bool, str | None]]:
    """Run one manager's connectivity probes concurrently, each under a timeout.

    Returns ``[(alias, is_healthy, reason)]`` in ``aliases`` order, where
    ``reason`` is :data:`_REASON_TIMEOUT` for a probe that hung past the timeout
    and ``None`` for a healthy result or a plain failure. Bounding each probe is
    the fix for a hung backend wedging the whole cycle; the concurrency cap keeps
    N unreachable backends finishing in ~one timeout rather than serialising N.
    """
    if not aliases:
        return []
    timeout = settings.DB_HEALTHCHECK_TIMEOUT_SECONDS
    limiter = asyncio.Semaphore(_MAX_CONCURRENT_HEALTH_PROBES)

    async def _one(alias: str) -> tuple[str, bool, str | None]:
        async with limiter:
            try:
                ok, _ = await asyncio.wait_for(probe(alias), timeout=timeout)
                return alias, ok, None
            except asyncio.TimeoutError:
                logger.warning(
                    "%s health check for '%s' timed out after %ss",
                    label, alias, timeout,
                )
                return alias, False, _REASON_TIMEOUT
            except Exception as exc:
                logger.warning("%s health check failed for '%s': %s", label, alias, exc)
                return alias, False, None

    return list(await asyncio.gather(*(_one(alias) for alias in aliases)))


async def _check_db_health() -> list[tuple[str, bool, str | None]]:
    """Check all registered DB connections. Returns [(alias, is_healthy, reason)]."""
    from app.services.connection_manager import connection_manager
    return await _probe_health_bounded(
        "DB", connection_manager.list_aliases(), connection_manager.test_connection,
    )


async def _check_s3_health() -> list[tuple[str, bool]]:
    """Check registered S3 connections. Returns [(alias, is_healthy)]."""
    from app.services.s3_manager import s3_manager
    results = []
    for alias in s3_manager.list_aliases():
        try:
            ok, _ = await s3_manager.test_connection(alias)
            results.append((alias, ok))
        except Exception as exc:
            logger.warning("S3 health check failed for '%s': %s", alias, exc)
            results.append((alias, False))
    return results


async def _check_nas_health() -> list[tuple[str, bool, str | None]]:
    """Check registered NAS connections. Returns [(alias, is_healthy, reason)].

    ``nas_manager.test_connection`` already bounds its own filesystem syscall via
    ``_run_blocking``; the outer timeout here is defence in depth (a saturated
    executor could still delay reaching that bound) and gives the same uniform
    "timed out" wording the DB check now uses.
    """
    from app.services.nas_manager import nas_manager
    return await _probe_health_bounded(
        "NAS", nas_manager.list_aliases(), nas_manager.test_connection,
    )


# ── Upstream reachability probe ──────────────────────────────────────────────
# Four of the helpers below (_upstream_scheme / _upstream_health_path /
# _node_host / _upstream_host_header) mirror `app/routers/gateway.py`'s
# route-test probe (_http_scheme_for_upstream / _health_path_for_route /
# _node_host / _host_header_for_upstream). They are replicated rather than
# imported because nothing under `app/services/` imports a router today: pulling
# `app.routers.gateway` in here would invert the layering and drag its whole
# import chain (api_keys → auth → database → …) into a module that `app.main`
# deliberately imports late, from its lifespan. Four tiny pure functions are the
# cheaper half of that trade. Keep them in sync with gateway.py if that probe
# changes. Node normalization is not among them: both sides call
# `apisix_client.upstream_node_addresses`.

# Schemes this checker can probe. APISIX upstreams may also declare grpc, grpcs,
# tcp, tls, udp or kafka; a GET /health against those proves nothing and usually
# fails at the transport, i.e. reports a healthy backend as down. Those fall back
# to the config-only judgment in `_probe_upstream` — a deliberate scope limit
# (speaking those protocols is a different job), not an oversight.
_HTTP_SCHEMES = {"http", "https"}

# Distinguishable failure causes, so the outbound mail says which one it is. The
# alert identity stays ("upstream_health", upstream_id) for both, leaving mute
# and resolve behaviour untouched — only the message text differs.
_REASON_NO_NODES = "no_nodes"
_REASON_UNREACHABLE = "unreachable"


def _upstream_scheme(upstream: dict[str, Any]) -> str:
    scheme = upstream.get("scheme")
    return scheme if scheme in _HTTP_SCHEMES else "http"


def _is_http_upstream(upstream: dict[str, Any]) -> bool:
    """True when a GET probe is meaningful: an HTTP scheme, or none (APISIX defaults to http)."""
    scheme = upstream.get("scheme")
    return scheme is None or scheme in _HTTP_SCHEMES


def _upstream_health_path(upstream: dict[str, Any]) -> str:
    """Health path for a bare upstream (no route context).

    LiteLLM answers on /health/liveliness, not /health — the same special case
    gateway.py's `_health_path_for_route` makes, keyed here on the upstream
    itself since there is no route to consult.
    """
    identifiers = {str(upstream.get("id") or ""), str(upstream.get("name") or "")}
    if "litellm" in identifiers:
        return "/health/liveliness"
    return "/health"


def _node_host(node_addr: str) -> str:
    host, _sep, _port = node_addr.rpartition(":")
    return host.strip("[]") if host else node_addr.strip("[]")


def _upstream_host_header(upstream: dict[str, Any], node_addr: str) -> str:
    pass_host = upstream.get("pass_host", "pass")
    if pass_host == "node":
        return _node_host(node_addr)
    if pass_host == "rewrite" and isinstance(upstream.get("upstream_host"), str):
        return upstream["upstream_host"]
    return settings.HOST_IP


async def _probe_node(
    client: httpx.AsyncClient,
    limiter: asyncio.Semaphore,
    url: str,
    headers: dict[str, str],
) -> bool:
    """True when the node answered with *any* HTTP status.

    Reachability, not correctness: a 404 (or a 401 on an authenticated
    upstream) still proves the port is open and something is speaking HTTP,
    which is exactly what "is this backend up?" asks. Only transport failures —
    connection refused, DNS failure, TLS error, timeout — mean down.
    """
    async with limiter:
        try:
            resp = await client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - any transport failure means unreachable
            logger.debug("Upstream node probe failed: %s (%s)", url, exc)
            return False
    logger.debug("Upstream node probe reached %s (HTTP %d)", url, resp.status_code)
    return True


async def _probe_upstream(
    client: httpx.AsyncClient,
    limiter: asyncio.Semaphore,
    upstream: dict[str, Any],
) -> tuple[bool, str | None]:
    """Return (is_healthy, failure_reason) for one upstream.

    Healthy means at least one weighted node answered the probe; the reason is
    None then, and otherwise names which of the two failures it was so the
    outbound alert can say so. Non-HTTP upstreams are judged on config alone,
    so "no weighted nodes" is the only failure they can report.

    Zero-weight nodes are dropped before probing: they take no traffic, so their
    reachability says nothing about whether the upstream can serve.
    """
    addresses = sorted(upstream_node_addresses(upstream.get("nodes")))
    if not addresses:
        return False, _REASON_NO_NODES
    if not _is_http_upstream(upstream):
        return True, None
    scheme = _upstream_scheme(upstream)
    path = _upstream_health_path(upstream)
    probes = [
        _probe_node(
            client,
            limiter,
            f"{scheme}://{addr}{path}",
            {"Host": _upstream_host_header(upstream, addr)},
        )
        for addr in addresses[:_MAX_PROBED_NODES_PER_UPSTREAM]
    ]
    if any(await asyncio.gather(*probes)):
        return True, None
    return False, _REASON_UNREACHABLE


async def _check_upstream_health(
    *, transport: Any | None = None
) -> list[tuple[str, bool, str | None]]:
    """Probe every APISIX upstream's nodes. Returns [(upstream_id, is_healthy, reason)].

    This used to read the APISIX *config* only — an upstream counted as healthy
    whenever its ``nodes`` map was non-empty with any positive weight. That is
    close to a tautology: the config describes where traffic should go, never
    whether anything is listening there, so a crashed backend reported "healthy"
    forever while the alert text promised to say when it was "down". The mirror
    image was just as bad: list-form ``nodes`` missed the ``isinstance(…, dict)``
    guard and produced a permanent false "down". Both are fixed by normalizing
    the node shapes and actually talking to the nodes.

    ``transport`` is injectable for tests, mirroring
    :func:`app.services.server_monitor.probe_metrics_endpoint`.
    """
    global _UPSTREAM_NAME_BY_ID
    from app.services import apisix_client

    try:
        data = await apisix_client.list_resources("upstreams")
        items = [item for item in data.get("items", []) if isinstance(item, dict)]
    except Exception as exc:
        # APISIX unreachable: report nothing rather than flipping every upstream
        # to "down" on our own blindness (the caller only updates the state of
        # upstreams it hears about).
        logger.warning("Upstream health check failed: %s", exc)
        return []

    _UPSTREAM_NAME_BY_ID = {
        str(item.get("id", "unknown")): str(item["name"])
        for item in items
        if item.get("name")
    }
    if not items:
        return []

    ssl_verify: str | bool = settings.SSL_CA_CERT_PATH or settings.SSL_VERIFY
    limiter = asyncio.Semaphore(_MAX_CONCURRENT_NODE_PROBES)
    async with httpx.AsyncClient(
        timeout=_UPSTREAM_PROBE_TIMEOUT, verify=ssl_verify, transport=transport
    ) as client:
        outcomes = await asyncio.gather(
            *(_probe_upstream(client, limiter, item) for item in items),
            return_exceptions=True,
        )

    results: list[tuple[str, bool, str | None]] = []
    for item, outcome in zip(items, outcomes):
        uid_str = str(item.get("id", "unknown"))
        if isinstance(outcome, BaseException):
            # A malformed upstream (or a bug in the probe path) must not abort
            # the rest of the check: drop this one for this cycle, leaving its
            # existing alert state untouched rather than guessing at it.
            logger.warning(
                "Upstream '%s' health probe raised unexpectedly: %s", uid_str, outcome
            )
            continue
        is_healthy, reason = outcome
        results.append((uid_str, is_healthy, reason))
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
            # This is the checker's one direct Prometheus call, so it doubles as
            # the liveness signal for "can the checker reach Prometheus?" — an
            # empty (no-traffic) answer is still a successful query.
            metrics.set_alert_checker_prometheus_up(True)
            return []
        err_results = await prometheus_client.instant_query(
            'sum by (route) (increase(apisix_http_status{code=~"5.."}[5m]))'
        )
        metrics.set_alert_checker_prometheus_up(True)
    except Exception as exc:
        logger.warning("Route error rate check failed: %s", exc)
        metrics.set_alert_checker_prometheus_up(False)
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
        gpu_util_warn_pct=settings_row.server_gpu_util_warn_pct,
        gpu_mem_warn_pct=settings_row.server_gpu_mem_warn_pct,
        forecast_hours=settings_row.server_disk_forecast_hours,
    )
    repeat = int(settings_row.repeat_alert_after_cycles or 0)
    return hosts, thresholds, repeat


async def _check_server_health(
    state: AlertStateManager,
    *,
    trigger_after_failures: int,
    mutes: MuteIndex | None = None,
) -> None:
    """Evaluate node_exporter host signals and dispatch transitions.

    Reuses the shared state machine + dispatch pipeline: each signal is one
    (alert_type, host) binary state, with warn/critical severity escalation and
    optional re-notification handled by AlertStateManager.

    A failure to load the registry/thresholds is isolated to this step so it
    can never abort the DB/NAS/upstream/route checks in the same cycle.
    """
    mutes = mutes if mutes is not None else MuteIndex()
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
        was_alerting = state.get_status(sig.alert_type, sig.target) == "alert"
        transition = state.update(
            sig.alert_type, sig.target,
            is_healthy=sig.is_healthy,
            display_target=sig.display,
            severity=sig.severity,
            trigger_after_failures=trigger_after_failures,
            repeat_after_cycles=repeat,
        )
        outbound = _outbound_alert_type(
            state, mutes,
            rule_type=sig.alert_type, target=sig.target, transition=transition,
            resource_type="server", resource_id=sig.target,
            was_alerting=was_alerting,
        )
        await _persist_state_safely(state, sig.alert_type, sig.target)
        if outbound:
            result = await dispatch_alert(
                resource_type="server", resource_id=sig.target,
                alert_type=outbound, rule_type=sig.alert_type,
                target=sig.target, message=sig.message,
                display_target=sig.display, rate=sig.value, threshold=sig.threshold,
                monitor_label=sig.monitor_label, severity=sig.severity,
                target_description=sig.description,
            )
            await _handle_dispatch_result(
                state, outbound=outbound, result=result,
                rule_type=sig.alert_type, target=sig.target,
            )


async def _load_service_monitoring() -> tuple[list[MonitoredService], int]:
    """Load registered external services and the re-notify cadence."""
    async with async_session() as db:
        settings_row = (
            await db.execute(select(AlertSettings).where(AlertSettings.id == 1))
        ).scalar_one_or_none()
        services = list((await db.execute(select(MonitoredService))).scalars().all())
    repeat = int(settings_row.repeat_alert_after_cycles or 0) if settings_row else 0
    return services, repeat


async def _check_service_health(
    state: AlertStateManager,
    *,
    trigger_after_failures: int,
    mutes: MuteIndex | None = None,
) -> None:
    """Evaluate external-service reachability signals and dispatch transitions.

    The service analogue of :func:`_check_server_health`: one binary
    ``external_service_down`` state per enabled service, fed through the same
    AlertStateManager + dispatch pipeline, with recipients resolved from the
    service's 담당자 (ResourceOwner resource_type ``service``) plus global admins.
    A config-load failure is isolated so it can never abort the other checks.
    """
    mutes = mutes if mutes is not None else MuteIndex()
    try:
        services, repeat = await _load_service_monitoring()
    except Exception as exc:  # noqa: BLE001
        logger.warning("External-service health check skipped (config load failed): %s", exc)
        return
    enabled = [s for s in services if getattr(s, "enabled", False)]
    if not enabled:
        return
    signals = await server_monitor.evaluate_services(enabled)
    for sig in signals:
        was_alerting = state.get_status(sig.alert_type, sig.target) == "alert"
        transition = state.update(
            sig.alert_type, sig.target,
            is_healthy=sig.is_healthy,
            display_target=sig.display,
            severity=sig.severity,
            trigger_after_failures=trigger_after_failures,
            repeat_after_cycles=repeat,
        )
        outbound = _outbound_alert_type(
            state, mutes,
            rule_type=sig.alert_type, target=sig.target, transition=transition,
            resource_type="service", resource_id=sig.target,
            was_alerting=was_alerting,
        )
        await _persist_state_safely(state, sig.alert_type, sig.target)
        if outbound:
            result = await dispatch_alert(
                resource_type="service", resource_id=sig.target,
                alert_type=outbound, rule_type=sig.alert_type,
                target=sig.target, message=sig.message,
                display_target=sig.display, rate=None, threshold=None,
                monitor_label=sig.monitor_label, severity=sig.severity,
                target_description=sig.description,
            )
            await _handle_dispatch_result(
                state, outbound=outbound, result=result,
                rule_type=sig.alert_type, target=sig.target,
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


def _outbound_alert_type(
    state: AlertStateManager,
    mutes: MuteIndex,
    *,
    rule_type: str,
    target: str,
    transition: str | None,
    resource_type: str,
    resource_id: str,
    was_alerting: bool,
) -> str | None:
    """Decide what to notify for one just-evaluated target, honouring mutes.

    Detection already happened — ``transition`` is verbatim what
    ``AlertStateManager.update`` returned — so this only gates delivery. It
    mutates the entry's ``pending_notify`` flag in memory; the caller's existing
    :func:`_persist_state_safely` call writes it out. ``was_alerting`` is the
    target's status *before* that update.

    ``pending_notify`` means exactly "this incident has not been announced".
    Everything below follows from that one invariant: an announced incident
    always gets its recovery announced, and an unannounced one never does.

    Returns "triggered" / "resolved" / None:

    - A trigger opening an unannounced incident while muted is withheld and
      remembered. A withheld *re*-announcement (severity escalation or the
      repeat cadence) is only a reminder of an incident recipients already know
      about, so it must not mark that incident unannounced.
    - A remembered trigger fires on the first unmuted cycle where the target is
      still alerting, even though that cycle has no transition of its own.
    - A recovery is delivered whenever its trigger was announced — a mute does
      not apply, because leaving a paged incident open is worse than one extra
      message. A recovery whose trigger was never announced stays silent.
    """
    muted = mutes.is_muted(resource_type, resource_id)
    pending = state.get_pending_notify(rule_type, target)

    if transition == "triggered":
        if muted:
            if not was_alerting:
                state.set_pending_notify(rule_type, target, True)
            logger.info(
                "Alert %s/%s triggered while muted — notification withheld",
                rule_type, target,
            )
            return None
        state.set_pending_notify(rule_type, target, False)
        return "triggered"

    if transition == "resolved":
        state.set_pending_notify(rule_type, target, False)
        if pending:
            logger.info(
                "Alert %s/%s resolved without an announced trigger — staying silent",
                rule_type, target,
            )
            return None
        return "resolved"

    if pending and not muted and state.get_status(rule_type, target) == "alert":
        state.set_pending_notify(rule_type, target, False)
        logger.info(
            "Alert %s/%s still firing after mute expiry — notifying now",
            rule_type, target,
        )
        return "triggered"
    return None


async def _rearm_pending_after_failed_dispatch(
    state: AlertStateManager,
    rule_type: str,
    target: str,
) -> None:
    """Mark an announced-but-undelivered trigger as still pending, and persist it.

    ``_outbound_alert_type`` clears ``pending_notify`` the instant it decides to
    notify a trigger, and the caller persists that *before* dispatch. If the send
    then fails, that cleared flag would bury the incident forever: no later cycle
    has a transition of its own and ``pending_notify`` is already False. Setting
    it back to True (and re-persisting so a restart keeps it) lets the next
    cycle's ``pending and not muted and status == "alert"`` branch re-announce
    it.
    """
    state.set_pending_notify(rule_type, target, True)
    await _persist_state_safely(state, rule_type, target)


async def _handle_dispatch_result(
    state: AlertStateManager,
    *,
    outbound: str,
    result: bool | None,
    rule_type: str,
    target: str,
) -> None:
    """React to what :func:`dispatch_alert` reported for one just-sent alert.

    ``result`` is tri-state (see :func:`dispatch_alert`): True = delivered,
    None = nothing to deliver to (a skip, not a failure), False = a delivery was
    attempted and failed. Only a failed *trigger* is re-armed, so the next cycle
    re-announces it. A failed *recovery* is deliberately left alone — the target
    is healthy again, so a missed recovery is self-correcting and re-arming would
    only risk a spurious re-trigger — but it is logged.
    """
    if result is not False:
        return
    if outbound == "triggered":
        logger.warning(
            "Alert dispatch for %s/%s failed to deliver — re-arming to retry next cycle",
            rule_type, target,
        )
        await _rearm_pending_after_failed_dispatch(state, rule_type, target)
    else:
        logger.warning(
            "Recovery dispatch for %s/%s failed to deliver — not re-arming "
            "(target is healthy, so the missed recovery is self-correcting)",
            rule_type, target,
        )


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
    mutes: MuteIndex | None = None,
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
    was_alerting = state.get_status("route_error_rate", route_id) == "alert"
    transition = state.update(
        "route_error_rate",
        route_id,
        is_healthy=is_healthy,
        display_target=display,
        trigger_after_failures=trigger_after_failures,
    )
    outbound = _outbound_alert_type(
        state, mutes if mutes is not None else MuteIndex(),
        rule_type="route_error_rate", target=route_id, transition=transition,
        resource_type="route", resource_id=route_id,
        was_alerting=was_alerting,
    )
    await _persist_state_safely(state, "route_error_rate", route_id)
    if outbound:
        msg = (
            f"Route '{display}' 5xx error rate is "
            f"{rate:.1f}% (threshold: {threshold}%)."
        )
        result = await dispatch_alert(
            resource_type="route", resource_id=route_id,
            alert_type=outbound, rule_type="route_error_rate",
            target=route_id, message=msg,
            display_target=display,
            rate=rate, threshold=threshold,
            monitor_label="라우트 에러율",
        )
        await _handle_dispatch_result(
            state, outbound=outbound, result=result,
            rule_type="route_error_rate", target=route_id,
        )


async def run_single_check(
    state: AlertStateManager,
    *,
    trigger_after_failures: int,
    mutes: MuteIndex | None = None,
) -> None:
    """Execute one round of all health checks.

    ``mutes`` is the active-suppression snapshot for this cycle; it gates
    outbound notifications only, never detection. Omit it and the current
    snapshot is loaded from the database.
    """
    if mutes is None:
        mutes = await load_mute_index()

    # 1. DB health
    db_results = await _check_db_health()
    for alias, is_healthy, reason in db_results:
        was_alerting = state.get_status("db_health", alias) == "alert"
        transition = state.update(
            "db_health", alias,
            is_healthy=is_healthy,
            trigger_after_failures=trigger_after_failures,
        )
        outbound = _outbound_alert_type(
            state, mutes,
            rule_type="db_health", target=alias, transition=transition,
            resource_type="db", resource_id=alias,
            was_alerting=was_alerting,
        )
        await _persist_state_safely(state, "db_health", alias)
        if outbound:
            if outbound == "resolved":
                msg = f"Database '{alias}' connection restored."
            elif reason == _REASON_TIMEOUT:
                msg = (
                    f"Database '{alias}' health check timed out after "
                    f"{settings.DB_HEALTHCHECK_TIMEOUT_SECONDS:g}s."
                )
            else:
                msg = f"Database '{alias}' connection failed."
            result = await dispatch_alert(
                resource_type="db", resource_id=alias,
                alert_type=outbound, rule_type="db_health",
                target=alias, message=msg,
                display_target=alias, monitor_label="DB 헬스체크",
            )
            await _handle_dispatch_result(
                state, outbound=outbound, result=result,
                rule_type="db_health", target=alias,
            )

    # 2. S3 connection health
    s3_results = await _check_s3_health()
    for alias, is_healthy in s3_results:
        was_alerting = state.get_status("s3_health", alias) == "alert"
        transition = state.update(
            "s3_health", alias,
            is_healthy=is_healthy,
            trigger_after_failures=trigger_after_failures,
        )
        outbound = _outbound_alert_type(
            state, mutes,
            rule_type="s3_health", target=alias, transition=transition,
            resource_type="s3", resource_id=alias,
            was_alerting=was_alerting,
        )
        await _persist_state_safely(state, "s3_health", alias)
        if outbound:
            msg = f"S3 connection '{alias}' {'restored' if outbound == 'resolved' else 'is unavailable'}."
            result = await dispatch_alert(
                resource_type="s3", resource_id=alias,
                alert_type=outbound, rule_type="s3_health",
                target=alias, message=msg,
                display_target=alias, monitor_label="S3 연결 상태",
            )
            await _handle_dispatch_result(
                state, outbound=outbound, result=result,
                rule_type="s3_health", target=alias,
            )

    # 3. NAS connection health
    nas_results = await _check_nas_health()
    for alias, is_healthy, reason in nas_results:
        was_alerting = state.get_status("nas_health", alias) == "alert"
        transition = state.update(
            "nas_health", alias,
            is_healthy=is_healthy,
            trigger_after_failures=trigger_after_failures,
        )
        outbound = _outbound_alert_type(
            state, mutes,
            rule_type="nas_health", target=alias, transition=transition,
            resource_type="nas", resource_id=alias,
            was_alerting=was_alerting,
        )
        await _persist_state_safely(state, "nas_health", alias)
        if outbound:
            if outbound == "resolved":
                msg = f"NAS connection '{alias}' restored."
            elif reason == _REASON_TIMEOUT:
                msg = (
                    f"NAS connection '{alias}' health check timed out after "
                    f"{settings.DB_HEALTHCHECK_TIMEOUT_SECONDS:g}s."
                )
            else:
                msg = f"NAS connection '{alias}' is unavailable."
            result = await dispatch_alert(
                resource_type="nas", resource_id=alias,
                alert_type=outbound, rule_type="nas_health",
                target=alias, message=msg,
                display_target=alias, monitor_label="NAS 연결 상태",
            )
            await _handle_dispatch_result(
                state, outbound=outbound, result=result,
                rule_type="nas_health", target=alias,
            )

    # 4. Upstream health
    upstream_results = await _check_upstream_health()
    for uid, is_healthy, reason in upstream_results:
        upstream_name = _UPSTREAM_NAME_BY_ID.get(uid)
        display = f"{upstream_name} ({uid})" if upstream_name and upstream_name != uid else uid
        was_alerting = state.get_status("upstream_health", uid) == "alert"
        transition = state.update(
            "upstream_health", uid,
            is_healthy=is_healthy,
            display_target=display,
            trigger_after_failures=trigger_after_failures,
        )
        outbound = _outbound_alert_type(
            state, mutes,
            rule_type="upstream_health", target=uid, transition=transition,
            resource_type="upstream", resource_id=uid,
            was_alerting=was_alerting,
        )
        await _persist_state_safely(state, "upstream_health", uid)
        if outbound:
            if outbound == "resolved":
                msg = f"Upstream '{display}' recovered."
            elif reason == _REASON_NO_NODES:
                msg = f"Upstream '{display}' has no weighted nodes configured."
            else:
                msg = f"Upstream '{display}' is down (no reachable node)."
            result = await dispatch_alert(
                resource_type="upstream", resource_id=uid,
                alert_type=outbound, rule_type="upstream_health",
                target=uid, message=msg,
                display_target=display, monitor_label="업스트림 헬스체크",
            )
            await _handle_dispatch_result(
                state, outbound=outbound, result=result,
                rule_type="upstream_health", target=uid,
            )

    # 5. Server (host) health via node_exporter metrics
    await _check_server_health(
        state, trigger_after_failures=trigger_after_failures, mutes=mutes,
    )

    # 5b. External API-service reachability (RED-metrics registry)
    await _check_service_health(
        state, trigger_after_failures=trigger_after_failures, mutes=mutes,
    )

    # 6. Route-level error rate (automatic for every route; global threshold)
    route_results = await _check_route_error_rate()
    if route_results is None:
        return

    active_route_alerts = state.get_entries(alert_type="route_error_rate", status="alert")
    if not route_results and not active_route_alerts:
        return

    async with async_session() as db:
        route_threshold, route_min_requests = await _load_route_error_settings(db)

    # The Prometheus label may carry the route *name* (APISIX prefer_name);
    # state keys and recipient lookups need the route id. Merge rows first:
    # right after a rename both the old and the new label report for the same
    # route for one window, and evaluating them separately would double-count
    # fail cycles (or let the stale row spuriously resolve the alert).
    merged: dict[str, tuple[float, float]] = {}  # route_id → (errors, requests)
    for label_value, rate, sample_count in route_results:
        route_id = await _resolve_route_id(label_value)
        errors, requests = merged.get(route_id, (0.0, 0.0))
        merged[route_id] = (
            errors + rate * sample_count / 100.0,
            requests + sample_count,
        )

    processed: set[str] = set()
    for route_id, (errors, requests) in merged.items():
        processed.add(route_id)
        await _evaluate_route_error_rule(
            state,
            route_id=route_id,
            rate=(errors / requests * 100.0) if requests > 0 else 0.0,
            threshold=route_threshold,
            trigger_after_failures=trigger_after_failures,
            sample_count=requests,
            min_requests=route_min_requests,
            mutes=mutes,
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
            mutes=mutes,
        )


async def start_checker(state: AlertStateManager) -> asyncio.Task:
    """Start the periodic health check loop as a background task."""
    async def _loop():
        logger.info("Alert checker started")
        while True:
            cycle_start = _monotonic()
            check_interval = await _get_check_interval_seconds()
            # Blue/green runs both colors against the same meta DB and the same
            # APISIX, so an ungated cycle would send every alert mail twice and
            # let the two processes race on persisted alert state. The gate is
            # re-evaluated every cycle rather than once at startup: a promote or
            # rollback only rewrites the APISIX upstream, it does not restart
            # containers, so the newly active color must start checking (and the
            # demoted one must stop) on the next tick without any restart.
            # `is_active_instance` logs its own transitions; staying quiet here
            # keeps a standby color from writing a line every interval.
            if await is_active_instance():
                trigger_after_failures = await _get_trigger_after_failures()
                try:
                    await run_single_check(state, trigger_after_failures=trigger_after_failures)
                except Exception:
                    logger.exception("Alert checker cycle failed")
                # Separate try: the daily GPU report is a scheduled side errand,
                # not part of health checking, so neither its failure nor the
                # health checks' may take the other down.
                try:
                    await maybe_send_gpu_util_report()
                except Exception:
                    logger.exception("Daily GPU utilisation report failed")
            elapsed = _monotonic() - cycle_start
            await asyncio.sleep(max(0.0, check_interval - elapsed))

    return asyncio.create_task(_loop())
