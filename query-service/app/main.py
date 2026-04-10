from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings, validate_settings
from app.database import get_db, init_db
from app.models import DBConnection
from app.routers import admin, api_keys, gateway, query, roles, users
from app.middleware.rate_limiter import RateLimitMiddleware, rate_limiter
from app.services.connection_manager import connection_manager
from app.services.settings_manager import settings_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


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
    try:
        from app.services import apisix_client

        # Ensure upstream for query-service exists
        await apisix_client.put_resource("upstreams", "query-service", {
            "name": "query-service",
            "type": "roundrobin",
            "nodes": {"query-service:8000": 1},
        })

        # Ensure /api/query/* route exists with key-auth
        await apisix_client.put_resource("routes", "query-api", {
            "name": "query-api",
            "uri": "/api/query/*",
            "methods": ["POST", "GET"],
            "upstream_id": "query-service",
            "plugins": {
                "key-auth": {},
                "proxy-rewrite": {
                    "regex_uri": ["^/api/query(.*)", "/query$1"],
                },
            },
            "status": 1,
        })
        logger.info("APISIX query route provisioned successfully")
    except Exception as exc:
        logger.warning("Failed to provision APISIX query route (will retry on first request): %s", exc)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    logger.info("Disposing all database engines...")
    await connection_manager.dispose_all()

    # Close Keycloak admin client if initialized
    from app.routers.users import _kc_admin
    if _kc_admin is not None:
        await _kc_admin.close()

    logger.info("Shutdown complete.")


app = FastAPI(
    title="API Hub - Query Service",
    description="Arbitrary SQL execution against registered databases with role-based access control and audit logging.",
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
    logger.warning("CORS_ALLOWED_ORIGINS is empty — no cross-origin requests will be allowed")

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
app.include_router(api_keys.router)
app.include_router(gateway.router)
app.include_router(roles.router)
app.include_router(users.router)


# ── Dev/Testing token endpoint ───────────────────────────────────────────────

if settings.ENABLE_DEV_TOKEN_ENDPOINT:
    logger.warning("DEV TOKEN ENDPOINT IS ENABLED — disable in production (ENABLE_DEV_TOKEN_ENDPOINT=false)")

    from fastapi import Depends, HTTPException, status
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.auth import create_token
    from app.schemas import TokenRequest, TokenResponse

    @app.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
    async def issue_token(body: TokenRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
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
