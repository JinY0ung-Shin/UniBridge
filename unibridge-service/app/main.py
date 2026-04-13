from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from httpx import HTTPStatusError
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings, validate_settings
from app.database import get_db, init_db
from app.models import DBConnection
from app.routers import admin, alerts, api_keys, gateway, query, roles, users
from app.middleware.rate_limiter import RateLimitMiddleware, rate_limiter
from app.services.connection_manager import connection_manager
from app.services.settings_manager import settings_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _is_missing_route_error(exc: Exception) -> bool:
    if isinstance(exc, HTTPStatusError):
        return exc.response.status_code == 404

    message = str(exc).lower()
    return "404" in message or "not found" in message


async def _preserve_consumer_restriction(
    route_id: str, body: dict[str, object]
) -> dict[str, object]:
    if route_id not in {"query-api", "llm-proxy"}:
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

    logger.info("Loading system settings...")
    async for db in get_db():
        await settings_manager.load_from_db(db)
        rate_limiter.update_limits(
            rate_limit=settings_manager.rate_limit_per_minute,
            max_concurrent=settings_manager.max_concurrent_queries,
        )
        break

    logger.info("Provisioning APISIX query route...")
    import asyncio as _asyncio
    from app.services import apisix_client

    _max_retries = 5
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
                    "nodes": {"unibridge-service:8000": 1},
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
                        "plugins": {
                            "key-auth": {},
                            "proxy-rewrite": {
                                "regex_uri": ["^/api/query(.*)", "/query$1"],
                            },
                        },
                        "status": 1,
                    },
                ),
            )
            logger.info("APISIX query route provisioned successfully")

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

                # /api/llm/* → LiteLLM proxy (client passes own Authorization header)
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
                            "plugins": {
                                "key-auth": {},
                                "proxy-rewrite": {
                                    "regex_uri": ["^/api/llm(.*)", "$1"],
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
                            "proxy-rewrite": {
                                "regex_uri": ["^/api/llm-admin(.*)", "$1"],
                            },
                        },
                        "status": 1,
                    },
                )

                logger.info("APISIX LiteLLM routes provisioned successfully")
            else:
                logger.info(
                    "LITELLM_MASTER_KEY not set — skipping LiteLLM route provisioning"
                )

            async for db in get_db():
                await api_keys.sync_all_consumer_route_restrictions(db)
                logger.info("Replayed stored API key route restrictions")
                break

            break
        except Exception as exc:
            if _attempt < _max_retries:
                _delay = 2**_attempt  # 2s, 4s, 8s, 16s
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

    from app.services.alert_state import AlertStateManager
    from app.services.alert_checker import start_checker
    from app.routers.alerts import set_alert_state

    alert_state = AlertStateManager()
    set_alert_state(alert_state)
    app.state.alert_task = await start_checker(alert_state)
    logger.info("Alert checker started")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    if hasattr(app.state, "alert_task"):
        app.state.alert_task.cancel()
        try:
            await app.state.alert_task
        except asyncio.CancelledError:
            pass
        logger.info("Alert checker stopped")

    logger.info("Disposing all database engines...")
    await connection_manager.dispose_all()

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
app.include_router(admin.router)
app.include_router(alerts.router)
app.include_router(api_keys.router)
app.include_router(gateway.router)
app.include_router(roles.router)
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
