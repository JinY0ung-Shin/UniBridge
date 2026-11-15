"""Plain helpers shared by test modules; the fixtures live in conftest.py.

Test modules import from here (or conftest), never from one another.
"""
from __future__ import annotations

import json
import threading
from contextlib import ExitStack, asynccontextmanager, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import create_token, invalidate_permission_cache
from app.models import ApiKeyAccess, Role, RolePermission

# ── Repository files ────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
BLUEGREEN_INFRA_COMPOSE_FILE = REPO_ROOT / "docker-compose.infra.yml"


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── Meta database ───────────────────────────────────────────────────────────


def sessions(engine) -> async_sessionmaker[AsyncSession]:
    """The session factory the app builds over ``engine``."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def make_role_token(
    engine,
    role: str,
    permissions: Iterable[str],
    *,
    username: str | None = None,
    description: str = "",
) -> str:
    """Create a custom (non-system) role holding ``permissions`` and return a
    token for a user in it (``test<role>`` unless ``username`` is given).

    Invalidates the permission cache, as any test that changes roles must."""
    async with sessions(engine)() as db:
        row = Role(name=role, description=description, is_system=False)
        db.add(row)
        await db.flush()
        for perm in permissions:
            db.add(RolePermission(role_id=row.id, permission=perm))
        await db.commit()
    await invalidate_permission_cache()
    return create_token(username or f"test{role}", role)


_JSON_KEY_COLUMNS = ("allowed_databases", "allowed_routes", "allowed_tables")


async def insert_api_key(engine, consumer_name: str, **columns: Any) -> dict[str, str]:
    """Insert an API-key row directly, bypassing ``POST /admin/api-keys`` (and
    APISIX). ``allowed_databases``/``allowed_routes``/``allowed_tables`` given
    as lists (or dicts) are stored JSON-encoded, as the app stores them.

    Returns the header APISIX hands the app for that consumer (trusted without
    the proxy secret in dev-token mode)."""
    for column in _JSON_KEY_COLUMNS:
        value = columns.get(column)
        if value is not None and not isinstance(value, str):
            columns[column] = json.dumps(list(value) if isinstance(value, tuple) else value)
    async with sessions(engine)() as db:
        db.add(ApiKeyAccess(consumer_name=consumer_name, **columns))
        await db.commit()
    return {"X-Consumer-Username": consumer_name}


# ── API keys through the admin API ──────────────────────────────────────────


@contextmanager
def mock_api_keys_apisix(**overrides: Any):
    """Patch the API-key router's APISIX client: PUT/DELETE succeed, every GET
    is a miss, and the route listing is empty (so the consumer-restriction
    sync has nothing to rewrite). ``overrides`` replace any of those mocks."""
    with patch("app.routers.api_keys.apisix_client") as mock:
        mock.put_resource = AsyncMock(return_value={})
        mock.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock.delete_resource = AsyncMock()
        mock.list_resources = AsyncMock(return_value={"items": []})
        for name, value in overrides.items():
            setattr(mock, name, value)
        yield mock


async def create_api_key_via_admin(
    client,
    token: str | None,
    name: str,
    *,
    expect: int | None = 201,
    apisix: dict[str, Any] | None = None,
    **fields: Any,
) -> httpx.Response:
    """``POST /admin/api-keys`` for ``name`` with APISIX mocked out
    (:func:`mock_api_keys_apisix`, ``apisix`` overriding its mocks).

    ``fields`` go into the body as given; a ``None`` token sends no
    Authorization header. Asserts the status is ``expect`` unless it is None."""
    from tests.conftest import auth_header

    body = {"name": name, **fields}
    with mock_api_keys_apisix(**(apisix or {})):
        resp = await client.post(
            "/admin/api-keys", json=body, headers=auth_header(token) if token else {}
        )
    if expect is not None:
        assert resp.status_code == expect, resp.text
    return resp


# ── Admin API: database connections ─────────────────────────────────────────


def cm_patch(db_type: str = "postgres"):
    """A patch of the admin router's connection_manager singleton.

    Mocked: add_connection/remove_connection (async no-ops), get_status →
    {"status": "registered"}, get_db_type → ``db_type`` (pass "neo4j" etc. to
    drive the graph-backend branches), get_engine → MagicMock,
    get_clickhouse_lock → a real lock, has_connection → True,
    test_connection (async) → success."""
    mock_cm = MagicMock()
    mock_cm.add_connection = AsyncMock()
    mock_cm.remove_connection = AsyncMock()
    mock_cm.get_status = MagicMock(return_value={"status": "registered"})
    mock_cm.get_db_type = MagicMock(return_value=db_type)
    mock_cm.get_engine = MagicMock(return_value=MagicMock())
    mock_cm.get_clickhouse_lock = MagicMock(return_value=threading.Lock())
    mock_cm.has_connection = MagicMock(return_value=True)
    mock_cm.test_connection = AsyncMock(return_value=(True, "Connection successful"))
    return patch("app.routers.admin.connection_manager", mock_cm)


async def register_database(client, token: str, alias: str = "maindb") -> None:
    """Register a Postgres connection through the admin API (no real server)."""
    from tests.conftest import auth_header

    with patch(
        "app.routers.admin.connection_manager.add_connection", new_callable=AsyncMock,
    ), patch(
        "app.routers.admin.connection_manager.get_status",
        return_value={"status": "registered"},
    ):
        resp = await client.post(
            "/admin/query/databases",
            json={
                "alias": alias,
                "db_type": "postgres",
                "host": "localhost",
                "port": 5432,
                "database": "app",
                "username": "user",
                "password": "pass",
            },
            headers=auth_header(token),
        )
    assert resp.status_code == 201, resp.text


async def audit_logs(client, token: str, **params: Any) -> Any:
    """``GET /admin/audit-logs`` with ``params``; asserts a 200."""
    from tests.conftest import auth_header

    resp = await client.get("/admin/audit-logs", params=params, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── APISIX ──────────────────────────────────────────────────────────────────


def http_status_error(
    status_code: int,
    body: str = "",
    *,
    method: str = "GET",
    url: str = "http://apisix",
    message: str = "err",
) -> httpx.HTTPStatusError:
    """The error httpx raises for a response with ``status_code`` and ``body``."""
    request = httpx.Request(method, url)
    response = httpx.Response(status_code, request=request, text=body)
    return httpx.HTTPStatusError(message, request=request, response=response)


class FakeApisix:
    """APISIX admin API stand-in.

    ``store`` holds each resource type's items by id (a GET of anything else is
    a 404 status error); ``existing`` seeds it, keyed by ``(resource, id)``.
    ``writes`` logs every PUT/DELETE in order, ``bodies`` keeps what each PUT
    sent, and ``put_errors`` makes the PUT of one ``(resource, id)`` raise
    (after it is logged).
    """

    def __init__(self, existing=None, put_errors=None) -> None:
        self.store: dict[str, dict[str, dict[str, Any]]] = {"routes": {}, "upstreams": {}}
        for (resource, resource_id), item in (existing or {}).items():
            self.store.setdefault(resource, {})[resource_id] = deepcopy(item)
        self.put_errors = dict(put_errors or {})
        self.writes: list[tuple[str, str, str]] = []
        self.bodies: dict[tuple[str, str], dict[str, Any]] = {}

    async def get_resource(self, resource: str, resource_id: str) -> dict[str, Any]:
        item = self.store.get(resource, {}).get(resource_id)
        if item is None:
            raise http_status_error(
                404, url=f"http://apisix:9180/apisix/admin/{resource}/{resource_id}"
            )
        return deepcopy(item)

    async def put_resource(self, resource: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.writes.append(("PUT", resource, resource_id))
        error = self.put_errors.get((resource, resource_id))
        if error is not None:
            raise error
        self.bodies[(resource, resource_id)] = deepcopy(body)
        self.store.setdefault(resource, {})[resource_id] = {**deepcopy(body), "id": resource_id}
        return deepcopy(self.store[resource][resource_id])

    async def delete_resource(self, resource: str, resource_id: str) -> None:
        await self.get_resource(resource, resource_id)
        self.writes.append(("DELETE", resource, resource_id))
        del self.store[resource][resource_id]

    async def list_resources(self, resource: str) -> dict[str, Any]:
        return {"items": [deepcopy(item) for item in self.store.get(resource, {}).values()]}


# A BIFROST_TEST_VK the /api/llm routes can carry (llm_routes.usable_virtual_key).
BIFROST_VK = "sk-bf-" + "v" * 32

# (value, usable) pairs for llm_routes.usable_virtual_key and the deploy
# script's bifrost_vk_usable, which must agree.
VIRTUAL_KEY_CASES = [
    (BIFROST_VK, True),
    ("sk-bf-" + "x" * 16, True),
    ("sk-bf-Az09_-" + "x" * 10, True),
    ("sk-bf-" + "x" * 15, False),
    ("v" * 40, False),
    ("SK-BF-" + "x" * 32, False),
    ("", False),
    # Bifrost would take these, but the deploy script finds the key in
    # whitespace-stripped, JSON-escaped route bodies, where they never match.
    ("sk-bf-" + "x" * 16 + " y", False),
    ("sk-bf-" + "x" * 16 + '"y', False),
    ("sk-bf-" + "x" * 16 + "\\y", False),
    ("sk-bf-" + "x" * 16 + "+y", False),
    ("sk-bf-" + "x" * 16 + "é", False),
]


# ── Running the app lifespan ────────────────────────────────────────────────


class DummyTask:
    """A background task stand-in that is already done."""

    def cancel(self) -> None:
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class _EmptyDb:
    async def execute(self, _query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    async def commit(self):
        return None


@asynccontextmanager
async def empty_session():
    """Stands in for app.main.async_session: every session is an empty database."""
    yield _EmptyDb()


def keyed_get_resource(resources: dict[tuple[str, str], object]) -> AsyncMock:
    """apisix_client.get_resource fake keyed by (resource, resource_id).

    Order-independent on purpose: with a positional ``side_effect`` list, adding
    a lookup anywhere in the lifespan silently shifts which response every later
    call receives. Unmapped keys raise the "missing resource" error the lifespan
    treats as first-boot creation, so an empty mapping means "nothing exists
    yet". Map a key to an exception to make that one lookup fail.
    """

    def _get(resource, resource_id):
        try:
            value = resources[(resource, resource_id)]
        except KeyError:
            raise RuntimeError(f"404 {resource}/{resource_id} not found") from None
        if isinstance(value, Exception):
            raise value
        return value

    return AsyncMock(side_effect=_get)


async def run_lifespan(
    settings: object,
    *,
    get_resource: Any = None,
    put_resource: Any = None,
    list_resources: Any = None,
    replay: Any = None,
    start_checker: Any = None,
    session: Any = empty_session,
    extra_patches: Iterable[Any] = (),
    app: Any = None,
) -> None:
    """Start and stop ``app.main.lifespan`` with no real APISIX, database or
    network: ``settings`` stands in for ``app.main.settings``.

    - ``get_resource``: a mock, or a ``{(resource, id): body}`` mapping for
      :func:`keyed_get_resource`; by default nothing exists yet (first boot).
    - ``put_resource``: by default a bare ``AsyncMock``.
    - ``list_resources``: by default an empty listing.
    - ``replay``: stands in for the API-key restriction replay; by default the
      real one runs (on an empty database it touches nothing).
    - ``start_checker``: by default returns an already-done task.
    - ``extra_patches``: more ``patch(...)`` context managers to enter.
    """
    from fastapi import FastAPI

    from app.main import lifespan

    if get_resource is None:
        get_resource = keyed_get_resource({})
    elif isinstance(get_resource, dict):
        get_resource = keyed_get_resource(get_resource)
    patches = [
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.async_session", session),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", settings),
        patch("app.services.apisix_client.get_resource", get_resource),
        patch("app.services.apisix_client.put_resource", put_resource or AsyncMock()),
        patch(
            "app.services.apisix_client.list_resources",
            list_resources or AsyncMock(return_value={"items": []}),
        ),
        patch(
            "app.services.alert_checker.start_checker",
            start_checker or AsyncMock(return_value=DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
        *extra_patches,
    ]
    if replay is not None:
        patches.append(patch("app.main.api_keys.sync_all_consumer_route_restrictions", replay))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        async with lifespan(app or FastAPI()):
            pass


def put_bodies(put_resource: AsyncMock, resource: str) -> dict[str, dict]:
    """What each ``put_resource(resource, id, body)`` call wrote, by id."""
    return {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == resource
    }


# ── SQLAlchemy result / session stand-ins ───────────────────────────────────

_UNSET = object()


class FakeResult:
    """A SQLAlchemy result: ``scalar_one_or_none()``/``one_or_none()`` give
    ``value`` (by default the first of ``rows``), ``scalars().all()``/``all()``
    give ``rows``."""

    def __init__(self, value: Any = _UNSET, *, rows: Iterable[Any] = (), rowcount: int = 0) -> None:
        self.rows = list(rows)
        self.value = (self.rows[0] if self.rows else None) if value is _UNSET else value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self.value

    def one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeSessionContext:
    """``async with async_session() as db`` yielding ``db``, or failing on entry."""

    def __init__(self, db: Any = None, *, enter_error: Exception | None = None) -> None:
        self.db = db
        self.enter_error = enter_error

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        return False


# ── Server monitoring ───────────────────────────────────────────────────────


def monitored_host(name="web1", address="10.0.0.1:9100", enabled=True, labels=None, **overrides):
    """A MonitoredHost-shaped row; per-host thresholds default to unset."""
    return SimpleNamespace(
        name=name, address=address, enabled=enabled, labels=labels,
        description=overrides.get("description", ""),
        disk_warn_pct=overrides.get("disk_warn_pct"),
        disk_crit_pct=overrides.get("disk_crit_pct"),
        cpu_warn_pct=overrides.get("cpu_warn_pct"),
        mem_warn_pct=overrides.get("mem_warn_pct"),
        disk_mountpoints=overrides.get("disk_mountpoints"),
        gpu_address=overrides.get("gpu_address"),
        gpu_util_warn_pct=overrides.get("gpu_util_warn_pct"),
        gpu_mem_warn_pct=overrides.get("gpu_mem_warn_pct"),
    )


def host_series(host: str, value) -> dict:
    """One Prometheus instant-vector sample labelled ``host``."""
    return {"metric": {"host": host}, "value": [0, str(value)]}


# ── Network map ─────────────────────────────────────────────────────────────

REGIONS_AB = {"A": "10.1.0.0/16", "B": "10.2.0.0/16"}
HOSTS_A2_B1 = {"a1": "10.1.0.1", "a2": "10.1.0.2", "b1": "10.2.0.1"}


async def seed_network(
    client, headers: dict[str, str], regions: dict[str, str], hosts: dict[str, str]
) -> dict[str, int]:
    """Create ``regions`` (name → CIDR) and then ``hosts`` (name → IP) through
    the API; returns every new id by name."""
    ids: dict[str, int] = {}
    for name, cidr in regions.items():
        resp = await client.post("/network/regions", headers=headers, json={"name": name, "cidrs": [cidr]})
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]
    for name, ip in hosts.items():
        resp = await client.post("/network/hosts", headers=headers, json={"name": name, "ip": ip})
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]
    return ids
