"""Shared test fixtures for unibridge-service."""
from __future__ import annotations

import os

# Override settings BEFORE any app import
os.environ.update({
    "META_DB_URL": "sqlite+aiosqlite:///:memory:",
    "ENCRYPTION_KEY": "test-key-for-testing-only-32bytes!",
    "JWT_SECRET": "test-jwt-secret-for-testing-32bytes",
    "ENABLE_DEV_TOKEN_ENDPOINT": "true",
    "APISIX_ADMIN_URL": "http://localhost:19180",
    "APISIX_ADMIN_KEY": "test-apisix-key",
    "PROMETHEUS_URL": "http://localhost:19090",
})

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import ALL_PERMISSIONS, create_token, invalidate_permission_cache
from app.models import Base, Role, RolePermission


# ── Database fixtures ──────────────────────────────────────────────────────

@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def engine_sqlite():
    """Plain SQLite async engine for service-level integration tests."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_session(engine):
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.fixture
async def seeded_db(engine):
    """Engine with seeded admin/developer/viewer roles."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        SEED_ROLES = {
            "admin": ALL_PERMISSIONS,
            "developer": [
                "query.databases.read", "query.permissions.read", "query.audit.read",
                "query.execute",
                "gateway.routes.read", "gateway.upstreams.read",
                "gateway.monitoring.read",
                "apikeys.read",
                "alerts.read",
            ],
            "viewer": ["gateway.monitoring.read", "query.audit.read", "alerts.read"],
        }
        for role_name, perms in SEED_ROLES.items():
            role = Role(name=role_name, description=f"Test {role_name}", is_system=True)
            db.add(role)
            await db.flush()
            for perm in perms:
                db.add(RolePermission(role_id=role.id, permission=perm))
        await db.commit()
    await invalidate_permission_cache()
    return engine


# ── App fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
async def app(seeded_db):
    """FastAPI app with test database override."""
    from app.database import get_db
    from app.main import app as _app

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    _app.dependency_overrides[get_db] = override_get_db
    await invalidate_permission_cache()
    yield _app
    _app.dependency_overrides.clear()
    await invalidate_permission_cache()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Auth helpers ───────────────────────────────────────────────────────────

@pytest.fixture
def admin_token():
    return create_token("testadmin", "admin")


@pytest.fixture
def developer_token():
    return create_token("testdev", "developer")


@pytest.fixture
def viewer_token():
    return create_token("testviewer", "viewer")


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
