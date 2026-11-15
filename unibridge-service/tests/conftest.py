"""Shared test fixtures for unibridge-service."""
from __future__ import annotations

import asyncio
import os

# Override settings BEFORE any app import
os.environ.update({
    "META_DB_URL": "sqlite+aiosqlite:///:memory:",
    "ENCRYPTION_KEY": "test-key-for-testing-only-32bytes!",
    "JWT_SECRET": "test-jwt-secret-for-testing-32bytes",
    "ENABLE_DEV_TOKEN_ENDPOINT": "true",
    "APISIX_ADMIN_URL": "http://localhost:19180",
    "APISIX_ADMIN_KEY": "test-apisix-key",
    "APISIX_INTERNAL_PROXY_SECRET": "test-internal-proxy-secret",
    "PROMETHEUS_URL": "http://localhost:19090",
})

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth import create_token, invalidate_permission_cache
from app.models import Base, Role, RolePermission
from tests.helpers import make_role_token, sessions


# ── Database fixtures ──────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def dispose_app_database_engine():
    yield
    from app.database import engine
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def fresh_route_listing_cache():
    """The gateway router caches the APISIX route listing (30s TTL) in a module
    global; reset it so one test's ``list_resources`` mock can't leak into the
    next test's route names, ids or filter expansion."""
    from app.routers import gateway

    gateway._invalidate_route_listing_cache()
    yield
    gateway._invalidate_route_listing_cache()


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
    async with sessions(engine)() as session:
        yield session


@pytest.fixture
async def seeded_db(engine):
    """Engine with the system roles (admin/user) as the app seeds them."""
    from app.database import _seed_role_definitions

    async with sessions(engine)() as db:
        for role_name, spec in _seed_role_definitions().items():
            role = Role(name=role_name, description=spec["description"], is_system=True)
            db.add(role)
            await db.flush()
            for perm in spec["permissions"]:
                db.add(RolePermission(role_id=role.id, permission=perm))
        await db.commit()
    await invalidate_permission_cache()
    return engine


@pytest.fixture
def db_factory(seeded_db):
    """A session factory over the seeded test database."""
    return sessions(seeded_db)


# ── App fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
async def app(seeded_db):
    """FastAPI app with test database override."""
    from app.database import get_db
    from app.main import app as _app

    session_factory = sessions(seeded_db)

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
def user_token():
    return create_token("testuser", "user")


@pytest.fixture
async def querier_token(seeded_db):
    """Token for a custom non-admin role that has query.execute but not
    query.databases.write.

    The two-system-role model (admin/user) intentionally gives no seeded
    non-admin role direct-query rights, but admins can still create arbitrary
    roles. This fixture seeds such a role so the per-database Permission
    enforcement logic (SELECT-only grants, missing-grant 403, etc.) stays under
    test. The role is created directly in the seeded test engine.
    """
    return await make_role_token(
        seeded_db, "querier", ("query.execute", "query.databases.read"), description="Test querier"
    )


@pytest.fixture
async def alerts_reader_token(seeded_db):
    """Token for a custom role with ``alerts.read`` but not ``alerts.write``.

    The seeded ``user`` role no longer carries alerts permissions, but admins
    can still create roles that do. Used to exercise reader-vs-writer behaviour
    (e.g. webhook/header masking for non-writers).
    """
    return await make_role_token(
        seeded_db, "alertsreader", ("alerts.read",), description="Test alerts reader"
    )


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def fresh_module_copy(name: str):
    """A throwaway, freshly executed copy of module ``name`` (not registered in
    ``sys.modules``): its globals hold their import-time values, which the
    shared copy may have lost to earlier tests.

    Only for modules whose import has no global side effects: re-executing one
    that registers Prometheus metrics or ORM tables (e.g. ``app.metrics``,
    ``app.models``) raises duplicate-registration errors."""
    import importlib.util

    spec = importlib.util.find_spec(name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Shared stand-ins ───────────────────────────────────────────────────────

USER_1_ID = "00000000-0000-4000-a000-000000000001"
NEW_USER_ID = "00000000-0000-4000-a000-000000000002"


@pytest.fixture
def kc_mock():
    """Patch ``app.routers.users._get_kc_admin`` to return an AsyncMock
    KeycloakAdminClient: one user (alice, USER_1_ID) with the ``user`` realm
    role, create_user → NEW_USER_ID, realm roles admin and user."""
    import app.routers.users as users_mod

    user = {
        "id": USER_1_ID,
        "username": "alice",
        "email": "alice@example.com",
        "enabled": True,
        "createdTimestamp": 1700000000000,
    }
    kc = AsyncMock()
    kc.list_users.return_value = ([dict(user)], 1)
    kc.get_user.return_value = dict(user)
    kc.get_user_realm_roles.return_value = [{"id": "role-id-user", "name": "user"}]
    kc.create_user.return_value = NEW_USER_ID
    kc.get_realm_roles.return_value = [
        {"id": "role-id-admin", "name": "admin"},
        {"id": "role-id-user", "name": "user"},
    ]
    # Reset the singleton so tests are isolated.
    users_mod._kc_admin = None
    with patch("app.routers.users._get_kc_admin", return_value=kc):
        yield kc
    users_mod._kc_admin = None


@pytest.fixture
def no_gateway(monkeypatch):
    """Network-map usage discovery reads APISIX upstreams: an empty listing
    instead of dialing a gateway (set its return_value/side_effect to feed it)."""
    from app.services import network_map

    upstreams = AsyncMock(return_value={"items": []})
    monkeypatch.setattr(network_map.apisix_client, "list_resources", upstreams)
    network_map.clear_gateway_cache()
    yield upstreams
    network_map.clear_gateway_cache()


@pytest.fixture
async def gateway(app):
    """APISIX for the tool calls MCP sends back out (tests.mcp_helpers.FakeGateway)."""
    from tests.mcp_helpers import FakeGateway

    fake = FakeGateway(app)
    async with httpx.AsyncClient(transport=fake, base_url="http://apisix:9080") as client:
        with patch("app.services.mcp_tools._gateway_client", return_value=client):
            yield fake


@pytest.fixture
def http_mock():
    """The app's outbound HTTP answered from registered responses (tests.http_mock):
    a request nothing answers, or a response nothing used, fails the test."""
    from tests.http_mock import mock_http

    with mock_http() as mock:
        yield mock
