from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.config import settings
from app.database import get_db, init_db
from app.models import DBConnection
from app.routers import admin, gateway, query, roles, users
from app.services.connection_manager import connection_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown logic."""
    # ── Startup ──────────────────────────────────────────────────────────
    logger.info("Initializing meta database...")
    await init_db()

    logger.info("Loading saved database connections...")
    async for db in get_db():
        result = await db.execute(select(DBConnection))
        connections = result.scalars().all()
        await connection_manager.initialize(list(connections))
        logger.info("Loaded %d database connection(s)", len(connections))
        break

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    logger.info("Disposing all database engines...")
    await connection_manager.dispose_all()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="API Hub - Query Service",
    description="Arbitrary SQL execution against registered databases with role-based access control and audit logging.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS - allow all origins for internal use
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(query.router)
app.include_router(admin.router)
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
