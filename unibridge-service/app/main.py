from __future__ import annotations

import asyncio
import logging
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
from app.database import get_db, init_db
from app.models import DBConnection, MonitoredHost, MonitoredService, NASConnection
from app.routers import (
    admin,
    alerts,
    api_keys,
    external_metrics,
    gateway,
    nas,
    query,
    query_history,
    roles,
    s3,
    servers,
    usages,
    users,
)
from app.middleware.rate_limiter import RateLimitMiddleware, rate_limiter
from app.services.apisix_system_resources import QUERY_TEMPLATE_WRITE_ROUTE_ID
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


def _is_missing_route_error(exc: Exception) -> bool:
    if isinstance(exc, HTTPStatusError):
        return exc.response.status_code == 404

    message = str(exc).lower()
    return "404" in message or "not found" in message


def _internal_proxy_headers(
    extra_headers: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    headers = dict(extra_headers or {})
    secret = getattr(settings, "APISIX_INTERNAL_PROXY_SECRET", "") or getattr(
        settings, "APISIX_ADMIN_KEY", ""
    )
    if secret:
        headers[APISIX_INTERNAL_PROXY_HEADER] = secret
    return {"set": headers} if headers else {}


async def _preserve_consumer_restriction(
    route_id: str, body: dict[str, object]
) -> dict[str, object]:
    if route_id not in {
        "query-api",
        QUERY_TEMPLATE_WRITE_ROUTE_ID,
        "llm-proxy",
        "s3-api",
        "llm-messages",
        "llm-responses",
        "nas-api",
        "usages-api",
    }:
        return body

    from app.services import apisix_client

    try:
        existing_route = await apisix_client.get_resource("routes", route_id)
    except Exception as exc:
        if _is_missing_route_error(exc):
            return body
        raise

    existing_plugins = existing_route.get("plugins", {})
    consumer_restriction = existing_plugins.get("consumer-restriction")
    if not consumer_restriction:
        return body

    new_body = dict(body)
    new_plugins = dict(new_body.get("plugins", {}))
    new_plugins["consumer-restriction"] = consumer_restriction
    new_body["plugins"] = new_plugins
    return new_body


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown logic."""
    # ── Startup ──────────────────────────────────────────────────────────
    validate_settings()
    logger.info("Initializing meta database...")
    await init_db()

    logger.info("Loading saved database connections...")
    async for db in get_db():
        result = await db.execute(select(DBConnection))
        connections = result.scalars().all()
        await connection_manager.initialize(list(connections))
        logger.info("Loaded %d database connection(s)", len(connections))
        break

    logger.info("Loading saved S3 connections...")
    async for db in get_db():
        from app.models import S3Connection as S3Conn
        result = await db.execute(select(S3Conn))
        s3_connections = result.scalars().all()
        await s3_manager.initialize(list(s3_connections))
        logger.info("Loaded %d S3 connection(s)", len(s3_connections))
        break

    logger.info("Loading saved NAS connections...")
    async for db in get_db():
        from app.models import NASConnection as NASConn
        result = await db.execute(select(NASConn))
        nas_connections = result.scalars().all()
        await nas_manager.initialize(list(nas_connections))
        logger.info("Loaded %d NAS connection(s)", len(nas_connections))
        break

    logger.info("Reconciling monitored-server scrape targets...")
    async for db in get_db():
        from app.services import server_monitor
        await server_monitor.sync_targets_from_db(db)
        await server_monitor.sync_service_targets_from_db(db)
        break

    logger.info("Loading system settings...")
    async for db in get_db():
        await settings_manager.load_from_db(db)
        rate_limiter.update_limits(
            rate_limit=settings_manager.rate_limit_per_minute,
            max_concurrent=settings_manager.max_concurrent_queries,
        )
        break

    if getattr(settings, "APISIX_PROVISION_ON_START", True):
        logger.info("Provisioning APISIX query route...")
        import asyncio as _asyncio
        from app.services import apisix_client

        # APISIX's admin API returns 503 for a while after the container starts
        # (it is still syncing config from etcd), and 400/502 transients can occur
        # mid-sync. Give it a generous window — up to ~100s of backoff — so a cold
        # `compose up` does not fail startup before APISIX is actually reachable.
        _max_retries = 10
        for _attempt in range(1, _max_retries + 1):
            try:
                # Ensure prometheus global rule exists so HTTP metrics are collected
                await apisix_client.put_resource(
                    "global_rules",
                    "prometheus",
                    {
                        "plugins": {"prometheus": {}},
                    },
                )

                # Ensure upstream for unibridge-service exists
                await apisix_client.put_resource(
                    "upstreams",
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

                # Ensure /api/query/* route exists with key-auth
                await apisix_client.put_resource(
                    "routes",
                    "query-api",
                    await _preserve_consumer_restriction(
                        "query-api",
                        {
                            "name": "query-api",
                            "uri": "/api/query/*",
                            "methods": ["POST", "GET"],
                            "upstream_id": "unibridge-service",
                            # Without this, APISIX's default 60s read timeout cuts
                            # long queries before the app's own timeout fires. The
                            # app allows req.timeout up to 300s (schemas.py), so
                            # keep the gateway read above that — the app then wins
                            # the race and returns a clean 408 + audit log.
                            # Seeded from APISIX_QUERY_ROUTE_TIMEOUT (env) but
                            # runtime-overridable via the settings UI, which
                            # live-patches this route. settings_manager was loaded
                            # from the DB just above, so this reflects the latest.
                            "timeout": {
                                "connect": getattr(
                                    settings, "APISIX_QUERY_ROUTE_CONNECT_TIMEOUT", 10
                                ),
                                "send": settings_manager.query_route_timeout,
                                "read": settings_manager.query_route_timeout,
                            },
                            "plugins": {
                                "key-auth": {},
                                "proxy-rewrite": {
                                    "regex_uri": ["^/api/query(.*)", "/query$1"],
                                    "use_real_request_uri_unsafe": True,
                                    "headers": _internal_proxy_headers(),
                                },
                            },
                            "status": 1,
                        },
                    ),
                )
                logger.info("APISIX query route provisioned successfully")

                # Editing is a separate key grant from query read/execute.
                await apisix_client.put_resource(
                    "routes",
                    QUERY_TEMPLATE_WRITE_ROUTE_ID,
                    await _preserve_consumer_restriction(
                        QUERY_TEMPLATE_WRITE_ROUTE_ID,
                        {
                            "name": QUERY_TEMPLATE_WRITE_ROUTE_ID,
                            "desc": "Create, edit, and delete query templates",
                            "uri": "/api/query/templates/*",
                            "methods": ["PUT", "PATCH", "DELETE"],
                            "priority": 20,
                            "upstream_id": "unibridge-service",
                            "plugins": {
                                "key-auth": {},
                                "consumer-restriction": {
                                    "whitelist": [api_keys.DENY_ALL_CONSUMER]
                                },
                                "proxy-rewrite": {
                                    "regex_uri": ["^/api/query(.*)", "/query$1"],
                                    "use_real_request_uri_unsafe": True,
                                    "headers": _internal_proxy_headers(),
                                },
                            },
                            "status": 1,
                        },
                    ),
                )
                logger.info("APISIX query-template write route provisioned successfully")

                # Ensure /api/s3/* route exists with key-auth
                await apisix_client.put_resource(
                    "routes",
                    "s3-api",
                    await _preserve_consumer_restriction(
                        "s3-api",
                        {
                            "name": "s3-api",
                            "uri": "/api/s3/*",
                            "methods": ["GET"],
                            "upstream_id": "unibridge-service",
                            "plugins": {
                                "key-auth": {},
                                "proxy-rewrite": {
                                    "regex_uri": ["^/api/s3(.*)", "/s3$1"],
                                    "use_real_request_uri_unsafe": True,
                                    "headers": _internal_proxy_headers(),
                                },
                            },
                            "status": 1,
                        },
                    ),
                )
                logger.info("APISIX S3 route provisioned successfully")

                # Ensure /api/nas/* route exists with key-auth. Ships inline
                # deny-all (consumer-restriction whitelist = DENY_ALL_CONSUMER) so
                # the route is never callable by an arbitrary key between this PUT
                # and the consumer-restriction replay below; the replay
                # (sync_all_consumer_route_restrictions) installs the real
                # whitelist, and on later boots _preserve_consumer_restriction
                # keeps it.
                await apisix_client.put_resource(
                    "routes",
                    "nas-api",
                    await _preserve_consumer_restriction(
                        "nas-api",
                        {
                            "name": "nas-api",
                            "uri": "/api/nas/*",
                            "methods": ["GET"],
                            "upstream_id": "unibridge-service",
                            "plugins": {
                                "key-auth": {},
                                "consumer-restriction": {"whitelist": [api_keys.DENY_ALL_CONSUMER]},
                                "proxy-rewrite": {
                                    "regex_uri": ["^/api/nas(.*)", "/nas$1"],
                                    "use_real_request_uri_unsafe": True,
                                    "headers": _internal_proxy_headers(),
                                },
                            },
                            "status": 1,
                        },
                    ),
                )
                logger.info("APISIX NAS route provisioned successfully")

                # Ensure /api/usages route exists with key-auth. Self-scoped
                # usage metrics (each consumer only ever sees its own traffic),
                # but access still follows the per-key route-grant model: ships
                # inline deny-all like nas-api so it is never callable by an
                # arbitrary key between this PUT and the consumer-restriction
                # replay below.
                await apisix_client.put_resource(
                    "routes",
                    "usages-api",
                    await _preserve_consumer_restriction(
                        "usages-api",
                        {
                            "name": "usages-api",
                            "uri": "/api/usages",
                            "methods": ["GET"],
                            "upstream_id": "unibridge-service",
                            "plugins": {
                                "key-auth": {},
                                "consumer-restriction": {"whitelist": [api_keys.DENY_ALL_CONSUMER]},
                                "proxy-rewrite": {
                                    "regex_uri": ["^/api/usages(.*)", "/usages$1"],
                                    "use_real_request_uri_unsafe": True,
                                    "headers": _internal_proxy_headers(),
                                },
                            },
                            "status": 1,
                        },
                    ),
                )
                logger.info("APISIX usages route provisioned successfully")

                # ── LiteLLM upstream and routes ──
                if settings.LITELLM_MASTER_KEY:
                    await apisix_client.put_resource(
                        "upstreams",
                        "litellm",
                        {
                            "name": "litellm",
                            "type": "roundrobin",
                            "scheme": "https",
                            "nodes": {"litellm:4000": 1},
                        },
                    )

                    # /api/llm/* → LiteLLM proxy (APISIX injects LiteLLM key automatically)
                    await apisix_client.put_resource(
                        "routes",
                        "llm-proxy",
                        await _preserve_consumer_restriction(
                            "llm-proxy",
                            {
                                "name": "llm-proxy",
                                "uri": "/api/llm/*",
                                "methods": ["POST", "GET", "PUT", "DELETE", "OPTIONS"],
                                "upstream_id": "litellm",
                                # LLM responses can stay silent past APISIX's
                                # default 60s read timeout (long TTFT, reasoning,
                                # large non-stream completions); allow long reads
                                # so the gateway doesn't drop the socket.
                                "timeout": {"connect": 60, "send": 600, "read": 600},
                                "plugins": {
                                    "key-auth": {},
                                    "proxy-rewrite": {
                                        "regex_uri": ["^/api/llm(.*)", "$1"],
                                        "use_real_request_uri_unsafe": True,
                                        "headers": {
                                            "set": {
                                                "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
                                                "x-litellm-end-user-id": "$consumer_name",
                                            },
                                        },
                                    },
                                },
                                "status": 1,
                            },
                        ),
                    )

                    # /api/llm-admin/* → LiteLLM Admin UI/API (same-origin via gateway)
                    await apisix_client.put_resource(
                        "routes",
                        "llm-admin",
                        {
                            "name": "llm-admin",
                            "uri": "/api/llm-admin/*",
                            "methods": ["POST", "GET", "PUT", "DELETE", "OPTIONS"],
                            "upstream_id": "litellm",
                            "plugins": {
                                "key-auth": {},
                                "proxy-rewrite": {
                                    "regex_uri": ["^/api/llm-admin(.*)", "$1"],
                                    "use_real_request_uri_unsafe": True,
                                },
                            },
                            "status": 1,
                        },
                    )

                    logger.info("APISIX LiteLLM routes provisioned successfully")

                    # ── LLM endpoint converter ──
                    # Translates Anthropic Messages and OpenAI Responses into the
                    # OpenAI chat-completions shape that sglang/vLLM-backed models
                    # serve reliably, then forwards to LiteLLM. The converter speaks
                    # plain HTTP on the internal network (it forwards to LiteLLM over
                    # HTTPS itself).
                    await apisix_client.put_resource(
                        "upstreams",
                        "llm-converter",
                        {
                            "name": "llm-converter",
                            "type": "roundrobin",
                            "scheme": "http",
                            "nodes": {
                                getattr(
                                    settings,
                                    "APISIX_LLM_CONVERTER_NODE",
                                    "llm-converter:4001",
                                ): 1
                            },
                        },
                    )

                    # Specific converter routes. Higher priority than the llm-proxy
                    # /api/llm/* catch-all so these exact paths win; the same key-auth
                    # / master-key injection as llm-proxy applies. Each ships deny-all
                    # by default so that between this PUT and the consumer-restriction
                    # replay below the route is never callable by an arbitrary key;
                    # the replay (sync_all_consumer_route_restrictions) installs the
                    # real whitelist, and on later boots _preserve_consumer_restriction
                    # keeps it.
                    for _conv_route_id, _conv_uri in (
                        ("llm-messages", "/api/llm/v1/messages"),
                        ("llm-responses", "/api/llm/v1/responses"),
                    ):
                        await apisix_client.put_resource(
                            "routes",
                            _conv_route_id,
                            await _preserve_consumer_restriction(
                                _conv_route_id,
                                {
                                    "name": _conv_route_id,
                                    "uri": _conv_uri,
                                    "methods": ["POST", "OPTIONS"],
                                    "priority": 10,
                                    "upstream_id": "llm-converter",
                                    # Match llm-proxy: don't let APISIX's default
                                    # 60s read timeout cut long/idle LLM streams.
                                    "timeout": {"connect": 60, "send": 600, "read": 600},
                                    "plugins": {
                                        "key-auth": {},
                                        "consumer-restriction": {
                                            "whitelist": [api_keys.DENY_ALL_CONSUMER]
                                        },
                                        "proxy-rewrite": {
                                            "regex_uri": ["^/api/llm(.*)", "$1"],
                                            "use_real_request_uri_unsafe": True,
                                            "headers": {
                                                "set": {
                                                    "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
                                                    "x-litellm-end-user-id": "$consumer_name",
                                                },
                                            },
                                        },
                                    },
                                    "status": 1,
                                },
                            ),
                        )

                    logger.info("APISIX LLM converter routes provisioned successfully")
                else:
                    logger.info(
                        "LITELLM_MASTER_KEY not set — skipping LiteLLM route provisioning"
                    )

                break
            except Exception as exc:
                if _attempt < _max_retries:
                    _delay = min(2**_attempt, 15)  # 2,4,8,15,15,… capped
                    logger.warning(
                        "APISIX provisioning attempt %d/%d failed: %s — retrying in %ds",
                        _attempt,
                        _max_retries,
                        exc,
                        _delay,
                    )
                    await _asyncio.sleep(_delay)
                else:
                    logger.error(
                        "APISIX provisioning failed after %d attempts: %s — "
                        "failing startup until APISIX is reachable",
                        _max_retries,
                        exc,
                    )
                    raise

    else:
        logger.info("Skipping APISIX route provisioning because APISIX_PROVISION_ON_START=false")

    # Replay stored API-key consumer-restrictions on EVERY boot, regardless of
    # APISIX_PROVISION_ON_START. The database is the source of truth for
    # per-consumer route access; this reconciles APISIX with it. It only updates
    # the consumer-restriction whitelist on routes that already exist (never
    # upstreams or proxy targets), so it is safe to run on an inactive blue/green
    # color before promotion. Running it unconditionally is what lets an inactive
    # color — or any color booting after an etcd reset — restore the real access
    # rules instead of leaving routes stuck at deny-all (or stale) until the next
    # first-boot-style provision.
    import asyncio as _asyncio_replay

    _replay_max_retries = 10
    for _replay_attempt in range(1, _replay_max_retries + 1):
        try:
            async for db in get_db():
                await api_keys.sync_all_consumer_route_restrictions(db)
                logger.info(
                    "Replayed stored API key route restrictions "
                    "(APISIX_PROVISION_ON_START=%s)",
                    getattr(settings, "APISIX_PROVISION_ON_START", True),
                )
                break
            break
        except Exception as exc:
            if _replay_attempt < _replay_max_retries:
                _delay = min(2**_replay_attempt, 15)  # 2,4,8,15,15,… capped
                logger.warning(
                    "API key restriction replay attempt %d/%d failed: %s — retrying in %ds",
                    _replay_attempt,
                    _replay_max_retries,
                    exc,
                    _delay,
                )
                await _asyncio_replay.sleep(_delay)
            else:
                logger.error(
                    "API key restriction replay failed after %d attempts: %s",
                    _replay_max_retries,
                    exc,
                )
                raise

    from app.services.alert_state import (
        AlertStateManager,
        load_alert_state_from_db,
        purge_stale_states,
    )
    from app.services.alert_checker import start_checker
    from app.routers.alerts import set_alert_state

    alert_state = AlertStateManager()
    async for db in get_db():
        await load_alert_state_from_db(db, alert_state)

        # Collect ground truth for stale-state purge. APISIX-derived
        # sets stay None when the API call fails, so a transient APISIX
        # outage does not wipe upstream/route alert state.
        db_aliases_result = await db.execute(select(DBConnection.alias))
        known_db_aliases = set(db_aliases_result.scalars().all())
        nas_aliases_result = await db.execute(select(NASConnection.alias))
        known_nas_aliases = set(nas_aliases_result.scalars().all())
        host_names_result = await db.execute(select(MonitoredHost.name))
        known_host_names = set(host_names_result.scalars().all())
        service_names_result = await db.execute(select(MonitoredService.name))
        known_service_names = set(service_names_result.scalars().all())

        known_upstream_ids: set[str] | None
        known_route_ids: set[str] | None
        try:
            from app.services import apisix_client as _apisix
            upstream_data = await _apisix.list_resources("upstreams")
            known_upstream_ids = {
                str(item.get("id"))
                for item in upstream_data.get("items", [])
                if item.get("id") is not None
            }
        except Exception as exc:
            logger.warning("Stale-state purge: could not list upstreams (%s) — skipping", exc)
            known_upstream_ids = None
        try:
            route_data = await _apisix.list_resources("routes")
            known_route_ids = {
                str(item.get("id"))
                for item in route_data.get("items", [])
                if item.get("id") is not None
            }
        except Exception as exc:
            logger.warning("Stale-state purge: could not list routes (%s) — skipping", exc)
            known_route_ids = None

        await purge_stale_states(
            db,
            alert_state,
            known_db_aliases=known_db_aliases,
            known_nas_aliases=known_nas_aliases,
            known_upstream_ids=known_upstream_ids,
            known_route_ids=known_route_ids,
            known_host_names=known_host_names,
            known_service_names=known_service_names,
        )
        break
    set_alert_state(alert_state)
    app.state.alert_task = await start_checker(alert_state)
    logger.info("Alert checker started")
    app.state.meta_db_metrics_task = asyncio.create_task(metrics.monitor_meta_db_health())
    logger.info("Metadata database metrics monitor started")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    if hasattr(app.state, "meta_db_metrics_task"):
        app.state.meta_db_metrics_task.cancel()
        try:
            await app.state.meta_db_metrics_task
        except asyncio.CancelledError:
            pass
        logger.info("Metadata database metrics monitor stopped")

    if hasattr(app.state, "alert_task"):
        app.state.alert_task.cancel()
        try:
            await app.state.alert_task
        except asyncio.CancelledError:
            pass
        logger.info("Alert checker stopped")

    logger.info("Disposing all database engines...")
    await connection_manager.dispose_all()

    logger.info("Disposing all S3 clients...")
    await s3_manager.dispose_all()

    logger.info("Disposing all NAS resources...")
    await nas_manager.dispose_all()

    from app.services import prometheus_client
    await prometheus_client.aclose()

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
app.include_router(gateway.router)
app.include_router(external_metrics.router)
app.include_router(s3.router)
app.include_router(nas.router)
app.include_router(servers.router)
app.include_router(roles.router)
app.include_router(usages.router)
app.include_router(users.router)


# ── Dev/Testing token endpoint ───────────────────────────────────────────────

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
