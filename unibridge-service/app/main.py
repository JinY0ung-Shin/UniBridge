from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from httpx import HTTPStatusError
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app import metrics
from app.config import settings, validate_settings
from app.database import async_session, get_db, init_db
from app.models import (
    DBConnection,
    MonitoredHost,
    MonitoredService,
    NASConnection,
    S3Connection,
)
from app.routers import (
    admin,
    alerts,
    api_keys,
    bifrost_sso,
    config_transfer,
    external_metrics,
    gateway,
    internal_alerts,
    mcp,
    nas,
    network,
    query,
    query_history,
    query_metrics,
    roles,
    s3,
    servers,
    upload_relay,
    usages,
    users,
)
from app.middleware.rate_limiter import RateLimitMiddleware, rate_limiter
from app.services.active_color import is_active_instance, run_periodic
from app.services import apisix_client, llm_routes
from app.services.apisix_system_resources import (
    APP_SERVICE_ROUTES,
    INTERNAL_PROXY_ROUTE_IDS,
    LLM_BI_NOT_FOUND_ROUTE_ID,
    LLM_NOT_FOUND_ROUTE_ID,
    PROMETHEUS_API_ROUTE,
    SERVICE_ROUTE_IDS,
    ServiceRoute,
)
from app.services.connection_manager import connection_manager
from app.services.s3_manager import s3_manager
from app.services.nas_manager import nas_manager
from app.services.settings_manager import settings_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
APISIX_INTERNAL_PROXY_HEADER = "X-UniBridge-Internal-Proxy"
# INTERNAL_PROXY_ROUTE_IDS (apisix_system_resources) are the routes provisioned
# with _internal_proxy_headers() — the ones whose requests reach this app
# through APISIX and are authenticated by that header.

# The fixed routes whose replayed consumer-restriction whitelist provisioning
# keeps (_preserve_consumer_restriction): every service route and the /api/llm
# routes that take key grants. A new fixed route joins this set.
_GRANTED_ROUTE_IDS = frozenset(
    {*SERVICE_ROUTE_IDS, "llm-proxy", *llm_routes.CONVERTER_ROUTE_IDS, "llm-metrics"}
)


def _internal_proxy_secret() -> str:
    return getattr(settings, "APISIX_INTERNAL_PROXY_SECRET", "")


def _internal_proxy_headers(
    extra_headers: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    headers = dict(extra_headers or {})
    secret = _internal_proxy_secret()
    if secret:
        headers[APISIX_INTERNAL_PROXY_HEADER] = secret
    return {"set": headers} if headers else {}


async def _reconcile_internal_proxy_header() -> None:
    """Refresh X-UniBridge-Internal-Proxy on the system routes that already exist.

    Route provisioning is the only other writer of this header, and steady-state
    blue/green containers boot with APISIX_PROVISION_ON_START=false — so after
    APISIX_INTERNAL_PROXY_SECRET is rotated, etcd would keep injecting the old
    value while this app expects the new one and every proxied /api/query|s3|nas|
    usages request would 401. GET-mutate-PUT rewrites only the header value, so
    consumer-restriction and every other field on the route survive. The routes
    are shared by both colors, and after a rotation the colors hold different
    values, so only the active color may run this (see lifespan and
    _run_internal_proxy_reconcile_loop): a restarted standby or a rollback
    target would otherwise write its older value under the active color.
    """
    secret = _internal_proxy_secret()
    if not secret:
        # validate_settings() refuses to boot without the secret, so an empty one
        # only happens with validation patched out — nothing to reconcile toward.
        return

    for route_id in INTERNAL_PROXY_ROUTE_IDS:
        route = await apisix_client.get_resource_or_none("routes", route_id)
        if route is None:
            # First boot: provisioning creates it carrying the current secret.
            continue

        plugins = route.get("plugins") or {}
        proxy_rewrite = plugins.get("proxy-rewrite") or {}
        headers = proxy_rewrite.get("headers") or {}
        header_set = headers.get("set") or {}
        if header_set.get(APISIX_INTERNAL_PROXY_HEADER) == secret:
            continue

        # id/create_time/update_time are server-managed metadata, not config.
        new_body = {
            key: value
            for key, value in route.items()
            if key not in ("id", "create_time", "update_time")
        }
        new_body["plugins"] = {
            **plugins,
            "proxy-rewrite": {
                **proxy_rewrite,
                "headers": {
                    **headers,
                    "set": {**header_set, APISIX_INTERNAL_PROXY_HEADER: secret},
                },
            },
        }
        await apisix_client.put_resource("routes", route_id, new_body)
        logger.info(
            "Refreshed %s on APISIX route %s",
            APISIX_INTERNAL_PROXY_HEADER,
            route_id,
        )


INTERNAL_PROXY_RECONCILE_SECONDS = 15


async def _run_internal_proxy_reconcile_loop(
    interval_seconds: float = INTERNAL_PROXY_RECONCILE_SECONDS,
) -> None:
    """Keep the shared routes' internal-proxy header on the active color's value.

    A promotion or rollback only re-points the APISIX upstream, so the color
    that becomes active finds whatever value the other color wrote; checking
    here brings the routes to its own value within one interval. A standby
    never writes. Failures are logged and retried on the next interval.
    """
    await run_periodic(
        _reconcile_internal_proxy_header,
        interval_seconds=interval_seconds,
        first_delay_seconds=interval_seconds,
        on_failure=lambda exc: logger.warning(
            "Internal-proxy header reconcile failed (%s); retrying in %ss",
            exc,
            interval_seconds,
        ),
    )


async def _preserve_consumer_restriction(
    route_id: str, body: dict[str, object]
) -> dict[str, object]:
    if route_id not in _GRANTED_ROUTE_IDS:
        return body

    existing_route = await apisix_client.get_resource_or_none("routes", route_id)
    if existing_route is None:
        return body

    existing_plugins = existing_route.get("plugins", {})
    consumer_restriction = existing_plugins.get("consumer-restriction")
    if not consumer_restriction:
        return body

    new_body = dict(body)
    new_plugins = dict(new_body.get("plugins", {}))
    new_plugins["consumer-restriction"] = consumer_restriction
    new_body["plugins"] = new_plugins
    return new_body


async def _put_upstream_if_unclaimed(upstream_id: str, body: dict[str, object]) -> bool:
    """PUT a color-pinned upstream unless another color already claims it.

    The blue/green deploy script's promote step is the only intended writer of
    the upstream -> color mapping, but the node this instance would write is
    baked into its container env at creation time. A container created as (or
    forced to) provision=true that later restarts — host reboot, dockerd
    restart, OOM — would otherwise re-point live traffic at its own color and
    silently undo a promotion. So: an existing upstream whose nodes differ from
    ours means this instance is not the active color; leave it alone.

    Returns True when the PUT was issued. Only a missing upstream counts as
    unclaimed — any other admin-API failure propagates so the caller's retry
    loop sees it instead of mistaking an outage for an absent upstream.
    """
    desired = apisix_client.upstream_node_addresses(body.get("nodes"))
    existing = await apisix_client.get_resource_or_none("upstreams", upstream_id)
    if existing is not None:
        # An upstream with no usable node serves nothing, so it is a broken
        # leftover rather than a claim — repair it.
        current = apisix_client.upstream_node_addresses(existing.get("nodes"))
        if current and current != desired:
            logger.warning(
                "APISIX upstream %s already points at %s, not %s — leaving it alone. "
                "This instance is not the active color; only the deploy script's "
                "promote step may switch upstreams.",
                upstream_id,
                sorted(current),
                sorted(desired),
            )
            return False

    await apisix_client.put_resource("upstreams", upstream_id, body)
    return True


async def _put_not_found_route(route_id: str, body: dict[str, object], prefix: str) -> None:
    """Install a route that only explains a 404 (llm-not-found, llm-bi-not-found).

    APISIX refusing it (a 4xx) is only logged: it explains a 404 and must not
    keep the service down. Anything else reaches the provisioning retry loop.
    """
    try:
        await apisix_client.put_resource("routes", route_id, body)
    except HTTPStatusError as exc:
        if not 400 <= exc.response.status_code < 500:
            raise
        logger.warning(
            "APISIX refused the %s route, so %s answers its bare 404: %s",
            route_id,
            prefix,
            exc.response.text,
        )


def _service_route(route: ServiceRoute) -> dict[str, object]:
    """The body provisioning writes for one of the fixed service routes.

    Key order is part of the contract: scripts/deploy-bluegreen.sh matches the
    route JSON APISIX hands back, and tests/test_main.py pins these bodies.
    """
    body: dict[str, object] = {"name": route.id}
    if route.desc is not None:
        body["desc"] = route.desc
    if len(route.paths) == 1:
        body["uri"] = route.paths[0]
    else:
        body["uris"] = list(route.paths)
    body["methods"] = list(route.methods)
    if route.priority is not None:
        body["priority"] = route.priority
    body["upstream_id"] = route.upstream_id
    if route.query_timeout:
        body["timeout"] = {
            "connect": getattr(settings, "APISIX_QUERY_ROUTE_CONNECT_TIMEOUT", 10),
            "send": settings_manager.query_route_timeout,
            "read": settings_manager.query_route_timeout,
        }
    plugins: dict[str, object] = {
        "key-auth": {"hide_credentials": False} if route.forward_key else {},
    }
    if route.deny_all:
        plugins["consumer-restriction"] = {"whitelist": [api_keys.DENY_ALL_CONSUMER]}
    rewrite: dict[str, object] = {"regex_uri": list(route.rewrite)}
    if route.raw_request_uri:
        rewrite["use_real_request_uri_unsafe"] = True
    if route.internal_proxy:
        rewrite["headers"] = _internal_proxy_headers()
    plugins["proxy-rewrite"] = rewrite
    body["plugins"] = plugins
    body["status"] = 1
    return body


async def _put_service_route(route: ServiceRoute) -> None:
    await apisix_client.put_resource(
        "routes",
        route.id,
        await _preserve_consumer_restriction(route.id, _service_route(route)),
    )
    if route.log:
        logger.info(route.log)


async def _provision_llm_routes() -> None:
    """Point the /api/llm routes at Bifrost (app/services/llm_routes.py).

    Without a usable BIFROST_TEST_VK every route is left as it is: injecting no
    key would turn each request into a 401.
    """
    virtual_key = getattr(settings, "BIFROST_TEST_VK", "") or ""
    if not llm_routes.usable_virtual_key(virtual_key):
        logger.error(
            "BIFROST_TEST_VK is %s, so the /api/llm routes are left as they are. "
            "Set it to the value the bifrost container runs with.",
            "not set" if not virtual_key else f"not {llm_routes.VIRTUAL_KEY_RULE}",
        )
        return

    await apisix_client.put_resource("upstreams", "bifrost", llm_routes.BIFROST_UPSTREAM)

    # The converter translates Anthropic Messages and OpenAI Responses into the
    # chat-completions shape that sglang/vLLM-backed models serve reliably, then
    # forwards to Bifrost. It speaks plain HTTP on the internal network.
    await _put_upstream_if_unclaimed(
        "llm-converter",
        {
            "name": "llm-converter",
            "type": "roundrobin",
            "scheme": "http",
            "nodes": {
                getattr(settings, "APISIX_LLM_CONVERTER_NODE", "llm-converter:4001"): 1
            },
        },
    )
    for route_id in llm_routes.CONVERTER_ROUTE_IDS:
        await apisix_client.put_resource(
            "routes",
            route_id,
            await _preserve_consumer_restriction(
                route_id, llm_routes.converter_route(route_id, virtual_key)
            ),
        )

    await apisix_client.put_resource(
        "routes",
        "llm-proxy",
        await _preserve_consumer_restriction(
            "llm-proxy", llm_routes.bifrost_proxy_route(virtual_key)
        ),
    )
    await apisix_client.put_resource(
        "routes",
        "llm-metrics",
        await _preserve_consumer_restriction("llm-metrics", llm_routes.bifrost_metrics_route()),
    )
    await _put_not_found_route(LLM_NOT_FOUND_ROUTE_ID, llm_routes.not_found_route(), "/api/llm")
    logger.info("APISIX /api/llm routes provisioned on Bifrost")


async def _provision_system_routes() -> None:
    """Write the fixed upstreams and routes (APISIX_PROVISION_ON_START)."""
    await _put_upstream_if_unclaimed(
        "unibridge-service",
        {
            "name": "unibridge-service",
            "type": "roundrobin",
            "nodes": {
                getattr(
                    settings,
                    "APISIX_UNIBRIDGE_SERVICE_NODE",
                    "unibridge-service:8000",
                ): 1
            },
        },
    )
    for route in APP_SERVICE_ROUTES:
        await _put_service_route(route)

    await apisix_client.put_resource(
        "upstreams",
        "prometheus",
        {
            "name": "prometheus",
            "type": "roundrobin",
            "scheme": "http",
            "nodes": {
                getattr(
                    settings,
                    "APISIX_PROMETHEUS_NODE",
                    "prometheus:9090",
                ): 1
            },
        },
    )
    await _put_service_route(PROMETHEUS_API_ROUTE)

    # ── /api/llm on Bifrost ──
    await _provision_llm_routes()

    # ── The removed Bifrost side-by-side test (/api/llm-bi) ──
    # Its routes are gone; this 404 points callers to /api/llm.
    await _put_not_found_route(
        LLM_BI_NOT_FOUND_ROUTE_ID, llm_routes.retired_llm_bi_route(), "/api/llm-bi"
    )

    # ── External MCP servers' relay routes (mcp-ext-*) ──
    # Before the restriction replay, which fills their whitelists from the
    # stored key grants.
    from app.services import mcp_remote

    async with async_session() as db:
        await mcp_remote.provision_all(db)


BOOT_RETRY_ATTEMPTS = 10


async def _retry_at_boot(
    what: str, operation: Callable[[], Awaitable[object]], *, give_up_note: str = ""
) -> None:
    """Run a boot step until it succeeds, up to BOOT_RETRY_ATTEMPTS times.

    APISIX's admin API returns 503 for a while after the container starts (it
    is still syncing config from etcd), and 400/502 transients can occur
    mid-sync. The backoff (2, 4, 8, then 15s) gives it a window of ~100s, so a
    cold ``compose up`` does not fail startup before APISIX is reachable. The
    last failure propagates and fails startup.
    """
    for attempt in range(1, BOOT_RETRY_ATTEMPTS + 1):
        try:
            await operation()
            return
        except Exception as exc:
            if attempt == BOOT_RETRY_ATTEMPTS:
                logger.error(
                    "%s failed after %d attempts: %s%s",
                    what,
                    BOOT_RETRY_ATTEMPTS,
                    exc,
                    give_up_note,
                )
                raise
            delay = min(2**attempt, 15)  # 2,4,8,15,15,… capped
            logger.warning(
                "%s attempt %d/%d failed: %s — retrying in %ds",
                what,
                attempt,
                BOOT_RETRY_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


async def _reconcile_node_path_prefix_rule() -> None:
    """Reconcile the per-node path-prefix global rule (app/services/node_path_prefix.py).

    Best effort: an APISIX that predates the plugin answers "unknown plugin",
    and that must not keep the service down — a save that sets a prefix
    reports it to the admin instead.
    """
    from app.services import node_path_prefix

    try:
        await node_path_prefix.ensure_global_rule()
        logger.info("Node path-prefix global rule reconciled")
    except Exception as exc:
        reason = node_path_prefix.describe_apisix_error(exc)
        affected = (
            await node_path_prefix.prefixed_resource_ids()
            if node_path_prefix.is_unknown_plugin_error(exc)
            else []
        )
        if affected:
            # APISIX ignores what it cannot run, so these nodes are being sent
            # requests without their base path right now.
            logger.error(
                "APISIX does not load the %s plugin (%s), yet %s set per-node path "
                "prefixes: their prefixed nodes now get requests WITHOUT the prefix. "
                "%s.",
                node_path_prefix.PLUGIN_NAME,
                reason,
                ", ".join(affected),
                node_path_prefix.RECREATE_APISIX_ADVICE,
            )
        elif node_path_prefix.is_unknown_plugin_error(exc):
            logger.warning(
                "Could not install the %s global rule (%s); per-node path prefixes "
                "stay unavailable until APISIX loads the plugin. %s.",
                node_path_prefix.PLUGIN_NAME,
                reason,
                node_path_prefix.RECREATE_APISIX_ADVICE,
            )
        else:
            logger.warning(
                "Could not install the %s global rule (%s); it is tried again on "
                "the next boot and before any save of an upstream with a path prefix.",
                node_path_prefix.PLUGIN_NAME,
                reason,
            )


async def _replay_route_restrictions() -> None:
    async with async_session() as db:
        await api_keys.sync_all_consumer_route_restrictions(db)
        logger.info(
            "Replayed stored API key route restrictions "
            "(APISIX_PROVISION_ON_START=%s)",
            getattr(settings, "APISIX_PROVISION_ON_START", True),
        )


async def _listed_ids(resource: str) -> set[str] | None:
    """The ids APISIX lists for ``resource``, or None when the listing fails."""
    try:
        listing = await apisix_client.list_resources(resource)
    except Exception as exc:
        logger.warning("Stale-state purge: could not list %s (%s) — skipping", resource, exc)
        return None
    return {
        str(item.get("id"))
        for item in listing.get("items", [])
        if item.get("id") is not None
    }


# The resources alert state is kept for, by purge_stale_states keyword: what
# still exists comes from these meta-DB columns.
_PURGE_KNOWN_COLUMNS = (
    ("known_db_aliases", DBConnection.alias),
    ("known_nas_aliases", NASConnection.alias),
    ("known_s3_aliases", S3Connection.alias),
    ("known_host_names", MonitoredHost.name),
    ("known_service_names", MonitoredService.name),
)

# Background tasks on app.state, stopped in this order at shutdown, with what
# is logged once each has stopped. The alert checker is stopped last, apart.
_BACKGROUND_TASKS = (
    ("retention_task", "Log retention cleanup stopped"),
    ("api_key_expiry_task", "API key expiry reconcile stopped"),
    ("internal_proxy_task", None),
    ("bifrost_session_task", "Bifrost session logout loop stopped"),
    ("upload_relay_task", "Upload relay purge loop stopped"),
    ("mcp_artifact_task", "MCP artifact purge loop stopped"),
    ("meta_db_metrics_task", "Metadata database metrics monitor stopped"),
)


async def _stop_task(app: FastAPI, attr: str) -> bool:
    """Cancel the task at ``app.state.<attr>`` and wait for it; False when there is none."""
    task = getattr(app.state, attr, None)
    if task is None:
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown logic."""
    # ── Startup ──────────────────────────────────────────────────────────
    validate_settings()
    logger.info("Initializing meta database...")
    await init_db()

    for what, model, manager in (
        ("database", DBConnection, connection_manager),
        ("S3", S3Connection, s3_manager),
        ("NAS", NASConnection, nas_manager),
    ):
        logger.info("Loading saved %s connections...", what)
        async with async_session() as db:
            result = await db.execute(select(model))
            connections = result.scalars().all()
            await manager.initialize(list(connections))
            logger.info("Loaded %d %s connection(s)", len(connections), what)

    logger.info("Reconciling monitored-server scrape targets...")
    async with async_session() as db:
        from app.services import server_monitor
        await server_monitor.sync_targets_from_db(db)
        await server_monitor.sync_service_targets_from_db(db)

    logger.info("Loading system settings...")
    async with async_session() as db:
        await settings_manager.load_from_db(db)
        rate_limiter.update_limits(
            rate_limit=settings_manager.rate_limit_per_minute,
            max_concurrent=settings_manager.max_concurrent_queries,
        )

    # Reconcile the prometheus global rule on EVERY boot, regardless of
    # APISIX_PROVISION_ON_START: it is what makes APISIX collect HTTP metrics in
    # the first place, and its plugin conf carries `prefer_name` (label series
    # by route *name*, falling back to id). APISIX reads that flag only from the
    # plugin conf — a `plugin_attr.prometheus` entry in config.yaml is silently
    # ignored — so this PUT is the single place it is set. Unlike route
    # provisioning it touches no routes/upstreams, making it safe on an
    # inactive blue/green color (mirrors the restriction replay below).
    await _retry_at_boot(
        "Prometheus global-rule PUT",
        lambda: apisix_client.put_resource(
            "global_rules",
            "prometheus",
            {
                "plugins": {"prometheus": {"prefer_name": True}},
            },
        ),
        give_up_note=" — failing startup until APISIX is reachable",
    )

    # Per-node path prefixes act only through the plugin's global rule, so
    # reconcile it on every boot as well (best effort, unlike the rule above).
    await _reconcile_node_path_prefix_rule()

    if getattr(settings, "APISIX_PROVISION_ON_START", True):
        logger.info("Provisioning APISIX query route...")
        await _retry_at_boot(
            "APISIX provisioning",
            _provision_system_routes,
            give_up_note=" — failing startup until APISIX is reachable",
        )
    else:
        logger.info("Skipping APISIX route provisioning because APISIX_PROVISION_ON_START=false")

    # Reconcile the internal-proxy header at boot regardless of
    # APISIX_PROVISION_ON_START, but only on the active color: it is what carries
    # a rotated APISIX_INTERNAL_PROXY_SECRET into etcd on a color that boots with
    # provisioning off, and a standby must not write its own (possibly older)
    # value into the routes both colors share. A color promoted later takes over
    # through _run_internal_proxy_reconcile_loop. When provisioning did run just
    # above, the routes already carry the current secret and every route here
    # no-ops. Runs before the restriction replay so the replay's route snapshot
    # already holds the new value. Retries like the global-rule PUT — the GET is
    # what tells a rotation apart from a no-op, so a reconcile that never
    # reached APISIX may be leaving auth broken.
    if await is_active_instance():
        await _retry_at_boot(
            "Internal-proxy header reconcile",
            _reconcile_internal_proxy_header,
            give_up_note=(
                " — failing startup: a rotated APISIX_INTERNAL_PROXY_SECRET that "
                "never reached APISIX 401s every gateway-proxied request"
            ),
        )
    else:
        logger.info(
            "Standby color: leaving the internal-proxy header on the shared routes "
            "to the active color"
        )

    # Replay stored API-key consumer-restrictions on EVERY boot, regardless of
    # APISIX_PROVISION_ON_START. The database is the source of truth for
    # per-consumer route access; this reconciles APISIX with it. It only updates
    # the consumer-restriction whitelist on routes that already exist (never
    # upstreams or proxy targets), so it is safe to run on an inactive blue/green
    # color before promotion. Running it unconditionally is what lets an inactive
    # color — or any color booting after an etcd reset — restore the real access
    # rules instead of leaving routes stuck at deny-all (or stale) until the next
    # first-boot-style provision.
    await _retry_at_boot("API key restriction replay", _replay_route_restrictions)

    from app.services.alert_state import (
        AlertStateManager,
        load_alert_state_from_db,
        purge_stale_states,
    )
    from app.services.alert_checker import start_checker
    from app.routers.alerts import set_alert_state

    alert_state = AlertStateManager()
    async with async_session() as db:
        await load_alert_state_from_db(db, alert_state)

        # Collect ground truth for stale-state purge. APISIX-derived
        # sets stay None when the API call fails, so a transient APISIX
        # outage does not wipe upstream/route alert state.
        known: dict[str, set[str]] = {}
        for keyword, column in _PURGE_KNOWN_COLUMNS:
            rows = await db.execute(select(column))
            known[keyword] = set(rows.scalars().all())

        await purge_stale_states(
            db,
            alert_state,
            known_upstream_ids=await _listed_ids("upstreams"),
            known_route_ids=await _listed_ids("routes"),
            **known,
        )
    set_alert_state(alert_state)
    app.state.alert_task = await start_checker(alert_state)
    logger.info("Alert checker started")
    app.state.meta_db_metrics_task = asyncio.create_task(metrics.monitor_meta_db_health())
    logger.info("Metadata database metrics monitor started")

    from app.services.retention import run_retention_loop

    app.state.retention_task = asyncio.create_task(run_retention_loop())

    # Gateway-side expiry enforcement. The LLM routes never reach this app, so
    # an expired key is only stopped by dropping it from the APISIX
    # consumer-restriction whitelists — which this loop keeps in sync with the
    # database on the active color.
    from app.services.consumer_restrictions import run_expiry_reconcile_loop

    app.state.api_key_expiry_task = asyncio.create_task(run_expiry_reconcile_loop())

    app.state.internal_proxy_task = asyncio.create_task(_run_internal_proxy_reconcile_loop())

    # Bifrost keeps a session for 30 days; log out the ones the sign-in handoff
    # (app/routers/bifrost_sso.py) handed to browsers once they are due.
    from app.services.bifrost_sessions import run_revoke_loop

    app.state.bifrost_session_task = asyncio.create_task(run_revoke_loop())

    # Upload relay (app/routers/upload_relay.py): delete the files the Noah
    # server never collected once they have expired.
    from app.services.upload_relay import run_purge_loop

    app.state.upload_relay_task = asyncio.create_task(run_purge_loop())

    # MCP artifacts (app/services/mcp_artifacts.py): delete the staged files
    # nobody fetched once they have expired.
    from app.services import mcp_artifacts

    app.state.mcp_artifact_task = asyncio.create_task(mcp_artifacts.run_purge_loop())

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    for attr, stopped in _BACKGROUND_TASKS:
        if await _stop_task(app, attr) and stopped:
            logger.info(stopped)

    if await _stop_task(app, "alert_task"):
        from app.services.alert_checker import stop_template_checks
        await stop_template_checks()
        logger.info("Alert checker stopped")

    logger.info("Disposing all database engines...")
    await connection_manager.dispose_all()

    logger.info("Disposing all S3 clients...")
    await s3_manager.dispose_all()

    logger.info("Disposing all NAS resources...")
    await nas_manager.dispose_all()

    from app.services import mcp_tools, prometheus_client
    await prometheus_client.aclose()
    await mcp_tools.aclose()
    await apisix_client.aclose()

    # Close Keycloak admin client if initialized
    from app.routers.users import _kc_admin

    if _kc_admin is not None:
        await _kc_admin.close()

    logger.info("Shutdown complete.")


app = FastAPI(
    title="UniBridge Service",
    description="Unified API hub for database queries, gateway management, and access control.",
    version="1.0.0",
    lifespan=lifespan,
    # ENABLE_API_DOCS gates the interactive docs: the edge proxies /_api/docs,
    # /_api/redoc and /_api/openapi.json straight through, so by default the
    # schema of the internal admin API must not be served at all.
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)
Instrumentator().instrument(app).expose(
    app,
    endpoint="/metrics",
    include_in_schema=False,
)

# ── Security headers middleware ──────────────────────────────────────────────


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)

# ── CORS ────────────────────────────────────────────────────────────────────
_cors_origins = [
    o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()
]
if not _cors_origins:
    logger.warning(
        "CORS_ALLOWED_ORIGINS is empty — no cross-origin requests will be allowed"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Include routers
app.include_router(query.router)
app.include_router(query_history.router)
app.include_router(admin.router)
app.include_router(alerts.router)
app.include_router(api_keys.router)
app.include_router(bifrost_sso.router)
app.include_router(config_transfer.router)
app.include_router(gateway.router)
app.include_router(external_metrics.router)
app.include_router(query_metrics.router)
app.include_router(internal_alerts.router)
app.include_router(mcp.router)
app.include_router(s3.router)
app.include_router(nas.router)
app.include_router(network.router)
app.include_router(servers.router)
app.include_router(roles.router)
app.include_router(upload_relay.router)
app.include_router(usages.router)
app.include_router(users.router)


# ── Dev/Testing token endpoint ───────────────────────────────────────────────
# The same flag also stops app.auth from checking the internal-proxy secret on
# API-key requests (see Settings.ENABLE_DEV_TOKEN_ENDPOINT).

if settings.ENABLE_DEV_TOKEN_ENDPOINT:
    logger.warning(
        "DEV TOKEN ENDPOINT IS ENABLED — disable in production (ENABLE_DEV_TOKEN_ENDPOINT=false)"
    )

    from fastapi import Depends, HTTPException, status
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.auth import create_token
    from app.schemas import TokenRequest, TokenResponse

    @app.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
    async def issue_token(
        body: TokenRequest, db: AsyncSession = Depends(get_db)
    ) -> TokenResponse:
        """
        Issue a JWT token for development/testing.

        This endpoint should be disabled or protected in production.
        """
        from app.models import Role

        result = await db.execute(select(Role).where(Role.name == body.role))
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Role '{body.role}' does not exist",
            )
        token = create_token(username=body.username, role=body.role)
        return TokenResponse(access_token=token)
