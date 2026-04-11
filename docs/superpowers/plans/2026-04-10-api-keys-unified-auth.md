# API Keys - Unified Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace "Gateway Consumers" with a unified "API Keys" system that controls access to both databases (query API) and gateway routes through a single API key, eliminating the need for Keycloak OAuth for external consumers.

**Architecture:** API keys are stored as APISIX consumers (key-auth) with access metadata (allowed DBs, allowed routes) in the local meta DB (`api_key_access` table). When a request arrives via APISIX (`/api/*`), query-service reads `X-Consumer-Username` header to identify the key and enforces access rules. The existing `/_api/*` Keycloak JWT path remains for web UI. Route-level consumer restriction is enforced via APISIX's `consumer-restriction` plugin, auto-synced when API key access changes.

**Tech Stack:** Python/FastAPI, SQLAlchemy async, APISIX Admin API, React/TypeScript, React Query, react-i18next

---

## File Structure

### Backend (query-service/app/)

| File | Action | Responsibility |
|---|---|---|
| `models.py` | Modify | Add `ApiKeyAccess` model |
| `schemas.py` | Modify | Add API key request/response schemas |
| `auth.py` | Modify | Add APISIX header-based auth, new permission `apikeys.read/write` |
| `database.py` | Modify | Update seed roles with new permissions |
| `routers/api_keys.py` | **Create** | API Keys CRUD (syncs APISIX consumers + meta DB) |
| `routers/gateway.py` | Modify | Remove consumer endpoints, add consumer-restriction sync helper |
| `routers/query.py` | Modify | Support API key users with DB-level access check |
| `main.py` | Modify | Register api_keys router, auto-provision APISIX query route |

### Backend Tests (query-service/tests/)

| File | Action | Responsibility |
|---|---|---|
| `test_api_keys.py` | **Create** | API Keys CRUD + access control tests |
| `test_gateway.py` | Modify | Remove consumer-related tests |
| `conftest.py` | Modify | Add api key fixtures |

### Frontend (query-ui/src/)

| File | Action | Responsibility |
|---|---|---|
| `api/client.ts` | Modify | Add API key endpoints, remove gateway consumer endpoints |
| `pages/ApiKeys.tsx` | **Create** | API Keys management page |
| `pages/ApiKeys.css` | **Create** | API Keys page styles (based on GatewayConsumers.css) |
| `pages/GatewayConsumers.tsx` | **Delete** | Replaced by ApiKeys |
| `pages/GatewayConsumers.css` | **Delete** | Replaced by ApiKeys.css |
| `pages/Connections.tsx` | Modify | Update cURL sample to API key based |
| `components/Layout.tsx` | Modify | Restructure nav: remove Consumers, add API Keys section |
| `App.tsx` | Modify | Update routes |
| `locales/ko.json` | Modify | Replace gatewayConsumers with apiKeys |
| `locales/en.json` | Modify | Replace gatewayConsumers with apiKeys |

---

## Task 1: Backend — ApiKeyAccess Model + Schemas

**Files:**
- Modify: `query-service/app/models.py`
- Modify: `query-service/app/schemas.py`

- [ ] **Step 1: Add ApiKeyAccess model to models.py**

Add after the `Permission` class:

```python
class ApiKeyAccess(Base):
    __tablename__ = "api_key_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    consumer_name = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(String(255), default="")
    allowed_databases = Column(Text, nullable=True)  # JSON array: ["mydb", "analytics"], null = none
    allowed_routes = Column(Text, nullable=True)  # JSON array: ["route-id-1", "route-id-2"], null = none
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```

- [ ] **Step 2: Add API key schemas to schemas.py**

Add at the end, before the Users section:

```python
# ── API Keys ────────────────────────────────────────────────────────────────

class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Unique key name (becomes APISIX consumer username)")
    description: str = ""
    api_key: str | None = Field(None, description="Custom API key value; auto-generated if omitted")
    allowed_databases: list[str] = Field(default_factory=list, description="Database aliases this key can query")
    allowed_routes: list[str] = Field(default_factory=list, description="Gateway route IDs this key can access")


class ApiKeyUpdate(BaseModel):
    description: str | None = None
    api_key: str | None = Field(None, description="New API key; omit to keep current")
    allowed_databases: list[str] | None = None
    allowed_routes: list[str] | None = None


class ApiKeyResponse(BaseModel):
    name: str
    description: str
    api_key: str | None = None
    key_created: bool = False
    allowed_databases: list[str]
    allowed_routes: list[str]
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
```

- [ ] **Step 3: Commit**

```bash
git add query-service/app/models.py query-service/app/schemas.py
git commit -m "feat: add ApiKeyAccess model and API key schemas"
```

---

## Task 2: Backend — Permission Updates + Seed Roles

**Files:**
- Modify: `query-service/app/auth.py`
- Modify: `query-service/app/database.py`

- [ ] **Step 1: Add new permissions to auth.py ALL_PERMISSIONS**

In `auth.py`, add to the `ALL_PERMISSIONS` list:

```python
ALL_PERMISSIONS = [
    # ... existing permissions ...
    "gateway.monitoring.read",
    "apikeys.read",
    "apikeys.write",
    "admin.roles.read",
    "admin.roles.write",
]
```

Insert `"apikeys.read"` and `"apikeys.write"` between `"gateway.monitoring.read"` and `"admin.roles.read"`.

- [ ] **Step 2: Update seed roles in database.py**

In `database.py` `_seed_roles()`, update the developer role to include `apikeys.read`:

```python
"developer": {
    "description": "Read access to queries and gateway, can execute queries",
    "permissions": [
        "query.databases.read", "query.permissions.read", "query.audit.read",
        "query.execute",
        "gateway.routes.read", "gateway.upstreams.read",
        "gateway.monitoring.read",
        "apikeys.read",
    ],
},
```

Note: `"gateway.consumers.read"` is removed since consumers are replaced by API Keys.

- [ ] **Step 3: Commit**

```bash
git add query-service/app/auth.py query-service/app/database.py
git commit -m "feat: add apikeys.read/write permissions and update seed roles"
```

---

## Task 3: Backend — APISIX Header-Based Auth

**Files:**
- Modify: `query-service/app/auth.py`
- Test: `query-service/tests/test_auth.py`

- [ ] **Step 1: Write failing tests for APISIX header auth**

Add to `tests/test_auth.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.auth import get_current_user_or_apikey, ApiKeyUser


@pytest.mark.asyncio
async def test_apikey_user_from_apisix_header():
    """When X-Consumer-Username header is present, return ApiKeyUser."""
    mock_request = MagicMock()
    mock_request.headers = {"x-consumer-username": "my-app-key"}

    mock_db = AsyncMock()
    # Simulate ApiKeyAccess record found
    mock_access = MagicMock()
    mock_access.consumer_name = "my-app-key"
    mock_access.allowed_databases = '["mydb"]'
    mock_access.allowed_routes = '["route-1"]'

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_access
    mock_db.execute = AsyncMock(return_value=mock_result)

    user = await get_current_user_or_apikey(
        request=mock_request, credentials=None, db=mock_db
    )
    assert isinstance(user, ApiKeyUser)
    assert user.consumer_name == "my-app-key"
    assert user.allowed_databases == ["mydb"]


@pytest.mark.asyncio
async def test_apikey_user_unknown_consumer_returns_401():
    """When X-Consumer-Username header has unknown consumer, raise 401."""
    from fastapi import HTTPException

    mock_request = MagicMock()
    mock_request.headers = {"x-consumer-username": "unknown-key"}

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_apikey(
            request=mock_request, credentials=None, db=mock_db
        )
    assert exc_info.value.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_auth.py -v -k "apikey_user" --no-header`
Expected: FAIL — `get_current_user_or_apikey` and `ApiKeyUser` don't exist yet.

- [ ] **Step 3: Implement ApiKeyUser and get_current_user_or_apikey in auth.py**

Add `ApiKeyUser` dataclass after `CurrentUser`:

```python
@dataclass
class ApiKeyUser:
    consumer_name: str
    allowed_databases: list[str]
    allowed_routes: list[str]
```

Add `get_current_user_or_apikey` dependency — this is the new unified auth dependency for endpoints that accept both JWT users and API key users:

```python
async def get_current_user_or_apikey(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser | ApiKeyUser:
    """Unified auth: APISIX header (API key) OR Bearer JWT.

    Priority:
    1. X-Consumer-Username header (set by APISIX after key-auth) → ApiKeyUser
    2. Bearer token → CurrentUser (existing JWT flow)
    """
    consumer_name = request.headers.get("x-consumer-username")
    if consumer_name:
        from app.models import ApiKeyAccess
        result = await db.execute(
            select(ApiKeyAccess).where(ApiKeyAccess.consumer_name == consumer_name)
        )
        access = result.scalar_one_or_none()
        if access is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Unknown API key consumer: {consumer_name}",
            )
        import json
        return ApiKeyUser(
            consumer_name=access.consumer_name,
            allowed_databases=json.loads(access.allowed_databases) if access.allowed_databases else [],
            allowed_routes=json.loads(access.allowed_routes) if access.allowed_routes else [],
        )

    # Fall back to JWT
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication",
        )
    return await get_current_user(credentials)
```

Add `Request` to the imports at the top of auth.py:

```python
from fastapi import Depends, HTTPException, Request, status
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_auth.py -v -k "apikey_user" --no-header`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add query-service/app/auth.py query-service/tests/test_auth.py
git commit -m "feat: add APISIX header-based auth for API key users"
```

---

## Task 4: Backend — API Keys CRUD Router

**Files:**
- Create: `query-service/app/routers/api_keys.py`
- Modify: `query-service/app/main.py`
- Create: `query-service/tests/test_api_keys.py`

- [ ] **Step 1: Write failing tests for API keys CRUD**

Create `tests/test_api_keys.py`:

```python
"""Tests for API Keys CRUD router."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from tests.conftest import auth_header


# ── List API keys ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_api_keys_empty(client, admin_token):
    resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_api_keys_requires_permission(client, viewer_token):
    resp = await client.get("/admin/api-keys", headers=auth_header(viewer_token))
    assert resp.status_code == 403


# ── Create API key ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_api_key(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "test-app",
            "plugins": {"key-auth": {"key": "key-abc123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "test-app",
                "description": "Test application",
                "api_key": "key-abc123",
                "allowed_databases": ["mydb"],
                "allowed_routes": [],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-app"
        assert data["description"] == "Test application"
        assert data["api_key"] == "key-abc123"
        assert data["key_created"] is True
        assert data["allowed_databases"] == ["mydb"]


@pytest.mark.asyncio
async def test_create_api_key_duplicate(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "dup-app",
            "plugins": {"key-auth": {"key": "key-1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))

        await client.post(
            "/admin/api-keys",
            json={"name": "dup-app", "api_key": "key-1"},
            headers=auth_header(admin_token),
        )
        # Second create with same name
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "dup-app", "api_key": "key-2"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 409


# ── Update API key ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_api_key_access(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "update-app",
            "plugins": {"key-auth": {"key": "key-u1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))

        # Create first
        await client.post(
            "/admin/api-keys",
            json={"name": "update-app", "api_key": "key-u1"},
            headers=auth_header(admin_token),
        )

        # Update
        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "update-app",
            "plugins": {"key-auth": {"key": "key-u1"}},
        })
        resp = await client.put(
            "/admin/api-keys/update-app",
            json={"allowed_databases": ["db1", "db2"], "description": "Updated"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed_databases"] == ["db1", "db2"]
        assert data["description"] == "Updated"


# ── Delete API key ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_api_key(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "del-app",
            "plugins": {"key-auth": {"key": "key-d1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()

        # Create
        await client.post(
            "/admin/api-keys",
            json={"name": "del-app", "api_key": "key-d1"},
            headers=auth_header(admin_token),
        )
        # Delete
        resp = await client.delete(
            "/admin/api-keys/del-app",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204

        # Verify gone
        resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))
        assert all(k["name"] != "del-app" for k in resp.json())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_api_keys.py -v --no-header`
Expected: FAIL — router doesn't exist.

- [ ] **Step 3: Create api_keys.py router**

Create `query-service/app/routers/api_keys.py`:

```python
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from httpx import HTTPStatusError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_permission
from app.database import get_db
from app.models import ApiKeyAccess
from app.schemas import ApiKeyCreate, ApiKeyResponse, ApiKeyUpdate
from app.services import apisix_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api-keys", tags=["API Keys"])

MASK_KEEP = 4


def _mask_key(value: str) -> str:
    if len(value) <= MASK_KEEP:
        return "***"
    return "***" + value[-MASK_KEEP:]


def _extract_api_key(consumer: dict, mask: bool = True) -> str | None:
    plugins = consumer.get("plugins", {})
    key = plugins.get("key-auth", {}).get("key")
    if not key:
        return None
    return _mask_key(key) if mask else key


def _to_response(
    access: ApiKeyAccess,
    api_key: str | None = None,
    key_created: bool = False,
) -> ApiKeyResponse:
    return ApiKeyResponse(
        name=access.consumer_name,
        description=access.description or "",
        api_key=api_key,
        key_created=key_created,
        allowed_databases=json.loads(access.allowed_databases) if access.allowed_databases else [],
        allowed_routes=json.loads(access.allowed_routes) if access.allowed_routes else [],
        created_at=access.created_at,
    )


async def _sync_consumer_restriction(allowed_routes: list[str], consumer_name: str) -> None:
    """Update consumer-restriction plugin on routes to include/exclude this consumer."""
    try:
        result = await apisix_client.list_resources("routes")
    except Exception:
        logger.warning("Failed to list routes for consumer-restriction sync")
        return

    for route in result.get("items", []):
        route_id = route.get("id")
        if not route_id:
            continue
        plugins = route.get("plugins", {})
        has_key_auth = "key-auth" in plugins

        if not has_key_auth:
            continue

        cr = plugins.get("consumer-restriction", {})
        whitelist = set(cr.get("whitelist", []))

        if route_id in allowed_routes:
            whitelist.add(consumer_name)
        else:
            whitelist.discard(consumer_name)

        if whitelist:
            plugins["consumer-restriction"] = {"whitelist": sorted(whitelist)}
        else:
            plugins.pop("consumer-restriction", None)

        try:
            body = {k: v for k, v in route.items() if k not in ("id", "create_time", "update_time")}
            body["plugins"] = plugins
            await apisix_client.put_resource("routes", route_id, body)
        except Exception:
            logger.warning("Failed to update consumer-restriction on route %s", route_id)


@router.get("", response_model=list[ApiKeyResponse])
async def list_api_keys(
    _admin: CurrentUser = Depends(require_permission("apikeys.read")),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKeyResponse]:
    result = await db.execute(select(ApiKeyAccess).order_by(ApiKeyAccess.created_at.desc()))
    keys = result.scalars().all()

    responses = []
    for access in keys:
        # Get masked key from APISIX
        masked_key = None
        try:
            consumer = await apisix_client.get_resource("consumers", access.consumer_name)
            masked_key = _extract_api_key(consumer, mask=True)
        except Exception:
            pass
        responses.append(_to_response(access, api_key=masked_key))
    return responses


@router.post("", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate,
    _admin: CurrentUser = Depends(require_permission("apikeys.write")),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyResponse:
    # Check duplicate in meta DB
    existing = await db.execute(
        select(ApiKeyAccess).where(ApiKeyAccess.consumer_name == body.name)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"API key '{body.name}' already exists")

    # Create APISIX consumer
    consumer_body: dict = {"username": body.name}
    if body.api_key:
        consumer_body["plugins"] = {"key-auth": {"key": body.api_key}}

    try:
        consumer = await apisix_client.put_resource("consumers", body.name, consumer_body)
    except HTTPStatusError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"APISIX error: {exc.response.text}")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to create APISIX consumer: {exc}")

    # Save access metadata to meta DB
    access = ApiKeyAccess(
        consumer_name=body.name,
        description=body.description,
        allowed_databases=json.dumps(body.allowed_databases) if body.allowed_databases else None,
        allowed_routes=json.dumps(body.allowed_routes) if body.allowed_routes else None,
    )
    db.add(access)
    await db.commit()
    await db.refresh(access)

    # Sync consumer-restriction on routes
    if body.allowed_routes:
        await _sync_consumer_restriction(body.allowed_routes, body.name)

    api_key = _extract_api_key(consumer, mask=False)
    return _to_response(access, api_key=api_key, key_created=True)


@router.put("/{name}", response_model=ApiKeyResponse)
async def update_api_key(
    name: str,
    body: ApiKeyUpdate,
    _admin: CurrentUser = Depends(require_permission("apikeys.write")),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyResponse:
    result = await db.execute(
        select(ApiKeyAccess).where(ApiKeyAccess.consumer_name == name)
    )
    access = result.scalar_one_or_none()
    if access is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key '{name}' not found")

    # Update APISIX consumer if new key provided
    key_created = False
    api_key_display: str | None = None
    if body.api_key:
        try:
            existing_consumer = await apisix_client.get_resource("consumers", name)
            existing_plugins = existing_consumer.get("plugins", {})
        except Exception:
            existing_plugins = {}
        existing_plugins["key-auth"] = {"key": body.api_key}
        try:
            consumer = await apisix_client.put_resource("consumers", name, {
                "username": name, "plugins": existing_plugins,
            })
            api_key_display = _extract_api_key(consumer, mask=False)
            key_created = True
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to update APISIX consumer: {exc}")

    # Update meta DB
    if body.description is not None:
        access.description = body.description
    if body.allowed_databases is not None:
        access.allowed_databases = json.dumps(body.allowed_databases) if body.allowed_databases else None
    if body.allowed_routes is not None:
        access.allowed_routes = json.dumps(body.allowed_routes) if body.allowed_routes else None
        await _sync_consumer_restriction(body.allowed_routes, name)

    await db.commit()
    await db.refresh(access)

    if not key_created:
        try:
            consumer = await apisix_client.get_resource("consumers", name)
            api_key_display = _extract_api_key(consumer, mask=True)
        except Exception:
            pass

    return _to_response(access, api_key=api_key_display, key_created=key_created)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_api_key(
    name: str,
    _admin: CurrentUser = Depends(require_permission("apikeys.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(ApiKeyAccess).where(ApiKeyAccess.consumer_name == name)
    )
    access = result.scalar_one_or_none()
    if access is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key '{name}' not found")

    # Remove consumer-restriction references before deleting
    old_routes = json.loads(access.allowed_routes) if access.allowed_routes else []
    if old_routes:
        await _sync_consumer_restriction([], name)  # empty = remove from all

    # Delete APISIX consumer
    try:
        await apisix_client.delete_resource("consumers", name)
    except HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"APISIX error: {exc.response.text}")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to delete APISIX consumer: {exc}")

    await db.delete(access)
    await db.commit()
```

- [ ] **Step 4: Register the router in main.py**

In `main.py`, add the import and include:

```python
from app.routers import admin, api_keys, gateway, query, roles, users
```

```python
app.include_router(api_keys.router)
```

- [ ] **Step 5: Update conftest.py to seed apikeys permissions**

In `tests/conftest.py`, update the `SEED_ROLES` in `seeded_db` fixture:

```python
"admin": ALL_PERMISSIONS,
"developer": [
    "query.databases.read", "query.permissions.read", "query.audit.read",
    "query.execute",
    "gateway.routes.read", "gateway.upstreams.read",
    "gateway.monitoring.read",
    "apikeys.read",
],
"viewer": ["gateway.monitoring.read", "query.audit.read"],
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_api_keys.py -v --no-header`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add query-service/app/routers/api_keys.py query-service/app/main.py query-service/tests/test_api_keys.py query-service/tests/conftest.py
git commit -m "feat: add API Keys CRUD router with APISIX consumer sync"
```

---

## Task 5: Backend — Query API Supports API Key Users

**Files:**
- Modify: `query-service/app/routers/query.py`
- Test: `query-service/tests/test_api_keys.py` (add query execution tests)

- [ ] **Step 1: Write failing test for query execution via API key**

Add to `tests/test_api_keys.py`:

```python
@pytest.mark.asyncio
async def test_query_execute_via_apikey_header(client, admin_token):
    """Simulate APISIX-forwarded request with X-Consumer-Username."""
    # First, create the API key access record via admin API
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "query-app",
            "plugins": {"key-auth": {"key": "qk-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        await client.post(
            "/admin/api-keys",
            json={"name": "query-app", "api_key": "qk-123", "allowed_databases": ["testdb"]},
            headers=auth_header(admin_token),
        )

    # Now simulate APISIX-forwarded request (no Bearer token, just X-Consumer-Username)
    resp = await client.post(
        "/query/execute",
        json={"database": "testdb", "sql": "SELECT 1"},
        headers={"X-Consumer-Username": "query-app"},
    )
    # Will get 404 because "testdb" engine doesn't exist in connection_manager,
    # but the auth should pass (not 401/403)
    assert resp.status_code == 404  # database not registered
    assert "not registered" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_query_execute_apikey_db_not_allowed(client, admin_token):
    """API key user cannot query databases not in their allowed list."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "restricted-app",
            "plugins": {"key-auth": {"key": "rk-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        await client.post(
            "/admin/api-keys",
            json={"name": "restricted-app", "api_key": "rk-123", "allowed_databases": ["allowed-db"]},
            headers=auth_header(admin_token),
        )

    resp = await client.post(
        "/query/execute",
        json={"database": "forbidden-db", "sql": "SELECT 1"},
        headers={"X-Consumer-Username": "restricted-app"},
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_api_keys.py -v -k "query_execute" --no-header`
Expected: FAIL — query router still uses `require_permission("query.execute")` which expects JWT.

- [ ] **Step 3: Modify query.py to support API key users**

In `query-service/app/routers/query.py`, update the `execute` endpoint:

Replace the import and dependency:

```python
from app.auth import ApiKeyUser, CurrentUser, get_current_user_or_apikey, get_role_permissions, require_permission
```

Change the execute function signature to use the unified auth:

```python
@router.post("/query/execute", response_model=QueryResponse)
async def execute(
    req: QueryRequest,
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> QueryResponse:
    """Execute an SQL query against a registered database."""

    # API Key user: check allowed databases
    if isinstance(user, ApiKeyUser):
        if req.database not in user.allowed_databases:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key '{user.consumer_name}' is not allowed to access database '{req.database}'",
            )
        # API key users get SELECT-only by default
        username = f"apikey:{user.consumer_name}"
    else:
        # JWT user: check role-based permission
        user_perms = await get_role_permissions(db, user.role)
        if "query.execute" not in user_perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Required permission: query.execute",
            )
        username = user.username

    # 1. Verify the database alias exists in the connection manager
    try:
        engine = connection_manager.get_engine(req.database)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database '{req.database}' is not registered or not connected",
        )

    db_type = connection_manager.get_db_type(req.database)

    # 2. Check per-database permissions (JWT users only — API key users already checked above)
    if isinstance(user, CurrentUser):
        statement_type = detect_statement_type(req.sql)
        perm = None
        user_perms = await get_role_permissions(db, user.role)
        if "query.databases.write" not in user_perms:
            result = await db.execute(
                select(Permission).where(
                    Permission.role == user.role,
                    Permission.db_alias == req.database,
                )
            )
            perm = result.scalar_one_or_none()
            if perm is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"No permissions configured for role '{user.role}' on database '{req.database}'",
                )
            if not check_permission(
                statement_type,
                perm.allow_select,
                perm.allow_insert,
                perm.allow_update,
                perm.allow_delete,
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Role '{user.role}' is not allowed to execute {statement_type.upper()} on '{req.database}'",
                )
    else:
        # API key users: only SELECT allowed
        statement_type = detect_statement_type(req.sql)
        if statement_type != "select":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key users can only execute SELECT queries",
            )
        perm = None

    # 2b. SQL keyword blacklist check
    blocked_error = validate_sql(req.sql, extra_blocked=settings_manager.blocked_sql_keywords)
    if blocked_error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=blocked_error)

    # 2c. Table-level access check (JWT non-admin users only)
    if isinstance(user, CurrentUser) and "query.databases.write" not in (await get_role_permissions(db, user.role)) and perm is not None:
        allowed_tables_raw = perm.allowed_tables
        allowed_tables = json.loads(allowed_tables_raw) if allowed_tables_raw else None
        if allowed_tables is not None:
            referenced = extract_tables(req.sql)
            table_error = check_table_access(referenced, allowed_tables)
            if table_error:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=table_error)

    # 3. Execute the query
    try:
        response = await execute_query(
            engine=engine, sql=req.sql, params=req.params,
            limit=req.limit, timeout=req.timeout, db_type=db_type,
        )
    except asyncio.TimeoutError:
        try:
            await log_query(db, user=username, database_alias=req.database,
                            sql=req.sql, params=req.params, status="error", error_message="Query timed out")
        except Exception:
            logger.exception("Failed to write audit log for timed-out query")
        raise HTTPException(status_code=status.HTTP_408_REQUEST_TIMEOUT, detail="Query timed out")
    except Exception as exc:
        try:
            await log_query(db, user=username, database_alias=req.database,
                            sql=req.sql, params=req.params, status="error", error_message=str(exc))
        except Exception:
            logger.exception("Failed to write audit log for failed query")
        logger.exception("Query execution failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Query execution failed. Check server logs for details.")

    # 4. Audit log (success)
    await log_query(db, user=username, database_alias=req.database,
                    sql=req.sql, params=req.params, row_count=response.row_count,
                    elapsed_ms=response.elapsed_ms, status="success")

    return response
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_api_keys.py -v -k "query_execute" --no-header`
Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/ -v --no-header`
Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add query-service/app/routers/query.py query-service/tests/test_api_keys.py
git commit -m "feat: query execute endpoint supports API key auth with DB access control"
```

---

## Task 6: Backend — Remove Consumer Endpoints from Gateway Router

**Files:**
- Modify: `query-service/app/routers/gateway.py`
- Modify: `query-service/tests/test_gateway.py`

- [ ] **Step 1: Remove consumer endpoints from gateway.py**

Delete everything from the `# ── Consumers ───` comment to the line before `# ── Metrics ───` in `gateway.py`. This removes:
- `_extract_api_key`, `_inject_consumer_key`, `_strip_consumer_secrets` functions
- `list_consumers`, `get_consumer`, `save_consumer`, `delete_consumer` endpoints

Also remove `_mask_value` and `MASK_KEEP` from gateway.py since they're now in `api_keys.py`.

- [ ] **Step 2: Remove consumer tests from test_gateway.py**

Delete the following test classes/functions from `test_gateway.py`:
- `TestExtractApiKey`
- `TestInjectConsumerKey`
- `TestStripConsumerSecrets`
- Any `test_*consumer*` endpoint tests

Also remove the unused imports from the test file:
- `_extract_api_key`, `_inject_consumer_key`, `_strip_consumer_secrets`, `_mask_value`

- [ ] **Step 3: Run gateway tests**

Run: `cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_gateway.py -v --no-header`
Expected: PASS (remaining route/upstream/metrics tests unaffected)

- [ ] **Step 4: Commit**

```bash
git add query-service/app/routers/gateway.py query-service/tests/test_gateway.py
git commit -m "refactor: remove consumer endpoints from gateway router (moved to api_keys)"
```

---

## Task 7: Backend — Auto-Provision APISIX Query Route

**Files:**
- Modify: `query-service/app/main.py`

- [ ] **Step 1: Add APISIX query route provisioning at startup**

In `main.py`, inside the `lifespan` function, after loading system settings, add:

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add query-service/app/main.py
git commit -m "feat: auto-provision APISIX route for /api/query/* at startup"
```

---

## Task 8: Frontend — API Client Updates

**Files:**
- Modify: `query-ui/src/api/client.ts`

- [ ] **Step 1: Replace gateway consumer types/functions with API key types/functions**

Remove from `client.ts`:
- `GatewayConsumer` interface
- `getGatewayConsumers`, `getGatewayConsumer`, `saveGatewayConsumer`, `deleteGatewayConsumer` functions

Add new types and functions:

```typescript
/* ── API Keys ── */

export interface ApiKey {
  name: string;
  description: string;
  api_key: string | null;
  key_created: boolean;
  allowed_databases: string[];
  allowed_routes: string[];
  created_at: string | null;
}

export interface ApiKeyCreate {
  name: string;
  description?: string;
  api_key?: string;
  allowed_databases: string[];
  allowed_routes: string[];
}

export interface ApiKeyUpdate {
  description?: string;
  api_key?: string;
  allowed_databases?: string[];
  allowed_routes?: string[];
}

export async function getApiKeys(): Promise<ApiKey[]> {
  const { data } = await client.get('/admin/api-keys');
  return data;
}

export async function createApiKey(body: ApiKeyCreate): Promise<ApiKey> {
  const { data } = await client.post('/admin/api-keys', body);
  return data;
}

export async function updateApiKey(name: string, body: ApiKeyUpdate): Promise<ApiKey> {
  const { data } = await client.put(`/admin/api-keys/${name}`, body);
  return data;
}

export async function deleteApiKey(name: string): Promise<void> {
  await client.delete(`/admin/api-keys/${name}`);
}
```

- [ ] **Step 2: Commit**

```bash
git add query-ui/src/api/client.ts
git commit -m "feat: replace gateway consumer API with API Keys API in client"
```

---

## Task 9: Frontend — i18n Updates

**Files:**
- Modify: `query-ui/src/locales/ko.json`
- Modify: `query-ui/src/locales/en.json`

- [ ] **Step 1: Update ko.json — replace gatewayConsumers with apiKeys, add nav.apiKeys**

Remove the entire `"gatewayConsumers"` section. Remove `"gatewayConsumers"` from `"nav"`.

Add to `"nav"`:
```json
"apiKeys": "API Keys"
```

Add new `"apiKeys"` section:
```json
"apiKeys": {
  "title": "API Keys",
  "subtitle": "API 키를 생성하고 데이터베이스 및 라우트 접근 권한을 관리합니다",
  "addKey": "+ API 키 추가",
  "loadingKeys": "API 키를 불러오는 중...",
  "loadFailed": "API 키를 불러오지 못했습니다.",
  "keyName": "이름",
  "description": "설명",
  "apiKey": "API 키",
  "allowedDatabases": "허용 데이터베이스",
  "allowedRoutes": "허용 라우트",
  "editTitle": "API 키 편집",
  "addTitle": "API 키 추가",
  "descriptionPlaceholder": "이 키의 용도를 입력하세요",
  "apiKeyPlaceholderEdit": "현재 값을 유지하려면 비워두세요",
  "apiKeyPlaceholderNew": "자동 생성된 키",
  "generateKey": "새 키 생성",
  "keyCreatedMessage": "API 키가 생성되었습니다. 지금 복사하세요 — 다시 볼 수 없습니다.",
  "copied": "복사됨!",
  "copy": "복사",
  "noKeys": "API 키가 없습니다",
  "noKeysDesc": "\"API 키 추가\"를 클릭하여 첫 번째 API 키를 생성하세요.",
  "deleteConfirm": "API 키 \"{{name}}\"을 삭제하시겠습니까?",
  "saveFailed": "API 키 저장에 실패했습니다",
  "deleteFailed": "API 키 삭제에 실패했습니다",
  "noneSelected": "선택 없음",
  "selectDatabases": "데이터베이스를 선택하세요",
  "selectRoutes": "라우트를 선택하세요",
  "allSelected": "{{count}}개 선택"
}
```

- [ ] **Step 2: Update en.json — same structure**

Remove `"gatewayConsumers"`. Remove `"gatewayConsumers"` from `"nav"`.

Add to `"nav"`:
```json
"apiKeys": "API Keys"
```

Add new `"apiKeys"` section:
```json
"apiKeys": {
  "title": "API Keys",
  "subtitle": "Create API keys and manage access to databases and routes",
  "addKey": "+ Add API Key",
  "loadingKeys": "Loading API keys...",
  "loadFailed": "Failed to load API keys.",
  "keyName": "Name",
  "description": "Description",
  "apiKey": "API Key",
  "allowedDatabases": "Allowed Databases",
  "allowedRoutes": "Allowed Routes",
  "editTitle": "Edit API Key",
  "addTitle": "Add API Key",
  "descriptionPlaceholder": "Describe what this key is for",
  "apiKeyPlaceholderEdit": "Leave empty to keep current",
  "apiKeyPlaceholderNew": "Auto-generated key",
  "generateKey": "Generate New Key",
  "keyCreatedMessage": "API key created. Copy it now — you won't be able to see it again.",
  "copied": "Copied!",
  "copy": "Copy",
  "noKeys": "No API keys",
  "noKeysDesc": "Click \"Add API Key\" to create your first API key.",
  "deleteConfirm": "Delete API key \"{{name}}\"?",
  "saveFailed": "Failed to save API key",
  "deleteFailed": "Failed to delete API key",
  "noneSelected": "None selected",
  "selectDatabases": "Select databases",
  "selectRoutes": "Select routes",
  "allSelected": "{{count}} selected"
}
```

- [ ] **Step 3: Commit**

```bash
git add query-ui/src/locales/ko.json query-ui/src/locales/en.json
git commit -m "feat: replace gatewayConsumers i18n with apiKeys"
```

---

## Task 10: Frontend — ApiKeys Page

**Files:**
- Create: `query-ui/src/pages/ApiKeys.tsx`
- Create: `query-ui/src/pages/ApiKeys.css`
- Delete: `query-ui/src/pages/GatewayConsumers.tsx`
- Delete: `query-ui/src/pages/GatewayConsumers.css`

- [ ] **Step 1: Create ApiKeys.css**

Create `query-ui/src/pages/ApiKeys.css` — based on GatewayConsumers.css with additions for multi-select:

```css
.api-keys {
  max-width: 1200px;
}

.api-keys .page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
}

.cell-key {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-tertiary);
}

.cell-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.tag {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  white-space: nowrap;
}

.tag-more {
  font-style: italic;
  color: var(--text-tertiary);
}

.key-created-banner {
  background: color-mix(in srgb, var(--accent-green) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent-green) 20%, transparent);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  margin: 0 24px 8px;
  font-size: 13px;
  color: var(--accent-green);
}

.key-created-banner p {
  margin-bottom: 8px;
  color: var(--text-secondary);
  font-size: 12px;
}

.key-display {
  display: flex;
  align-items: center;
  gap: 8px;
}

.key-display code {
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-primary);
  background: var(--bg-tertiary);
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  flex: 1;
  word-break: break-all;
}

.copy-btn {
  background: var(--bg-secondary);
  border: 1px solid var(--border-default);
  color: var(--text-secondary);
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}

.copy-btn:hover {
  border-color: var(--border-hover);
  color: var(--text-primary);
}

.generate-btn {
  margin-top: 4px;
}

.checkbox-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  padding: 8px;
}

.checkbox-list label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
}

.checkbox-list label:hover {
  color: var(--text-primary);
}

.checkbox-list input[type="checkbox"] {
  accent-color: var(--accent-blue);
}

.checkbox-list-empty {
  font-size: 12px;
  color: var(--text-tertiary);
  padding: 8px;
  text-align: center;
}
```

- [ ] **Step 2: Create ApiKeys.tsx**

Create `query-ui/src/pages/ApiKeys.tsx`:

```tsx
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getApiKeys,
  createApiKey,
  updateApiKey,
  deleteApiKey,
  getAdminDatabases,
  getGatewayRoutes,
  type ApiKey,
} from '../api/client';
import { useToast } from '../components/ToastContext';
import './ApiKeys.css';

function generateKey(): string {
  return 'key-' + crypto.randomUUID().replace(/-/g, '');
}

interface FormState {
  name: string;
  description: string;
  apiKey: string;
  allowedDatabases: string[];
  allowedRoutes: string[];
}

const emptyForm: FormState = {
  name: '',
  description: '',
  apiKey: '',
  allowedDatabases: [],
  allowedRoutes: [],
};

function ApiKeys() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  const [showModal, setShowModal] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>({ ...emptyForm });
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const keysQuery = useQuery({ queryKey: ['api-keys'], queryFn: getApiKeys });
  const dbsQuery = useQuery({ queryKey: ['admin-databases'], queryFn: getAdminDatabases });
  const routesQuery = useQuery({ queryKey: ['gateway-routes'], queryFn: getGatewayRoutes });

  const createMut = useMutation({
    mutationFn: createApiKey,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['api-keys'] });
      if (result.key_created && result.api_key) {
        setCreatedKey(result.api_key);
      } else {
        closeModal();
      }
    },
    onError: () => addToast({ type: 'error', title: t('apiKeys.saveFailed') }),
  });

  const updateMut = useMutation({
    mutationFn: ({ name, body }: { name: string; body: Record<string, unknown> }) => updateApiKey(name, body),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['api-keys'] });
      if (result.key_created && result.api_key) {
        setCreatedKey(result.api_key);
      } else {
        closeModal();
      }
    },
    onError: () => addToast({ type: 'error', title: t('apiKeys.saveFailed') }),
  });

  const deleteMut = useMutation({
    mutationFn: deleteApiKey,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['api-keys'] }),
    onError: () => addToast({ type: 'error', title: t('apiKeys.deleteFailed') }),
  });

  const keys = keysQuery.data ?? [];
  const databases = dbsQuery.data ?? [];
  const routes = routesQuery.data?.items ?? [];

  function openCreate() {
    setForm({ ...emptyForm, apiKey: generateKey() });
    setEditingName(null);
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function openEdit(k: ApiKey) {
    setForm({
      name: k.name,
      description: k.description,
      apiKey: '',
      allowedDatabases: k.allowed_databases,
      allowedRoutes: k.allowed_routes,
    });
    setEditingName(k.name);
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingName(null);
    setCreatedKey(null);
    setCopied(false);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (editingName) {
      const body: Record<string, unknown> = {
        description: form.description,
        allowed_databases: form.allowedDatabases,
        allowed_routes: form.allowedRoutes,
      };
      if (form.apiKey.trim()) body.api_key = form.apiKey.trim();
      updateMut.mutate({ name: editingName, body });
    } else {
      createMut.mutate({
        name: form.name.trim(),
        description: form.description,
        api_key: form.apiKey.trim() || undefined,
        allowed_databases: form.allowedDatabases,
        allowed_routes: form.allowedRoutes,
      });
    }
  }

  function handleDelete(k: ApiKey) {
    if (window.confirm(t('apiKeys.deleteConfirm', { name: k.name }))) {
      deleteMut.mutate(k.name);
    }
  }

  async function handleCopy() {
    if (createdKey) {
      await navigator.clipboard.writeText(createdKey);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  function toggleDb(alias: string) {
    setForm((prev) => ({
      ...prev,
      allowedDatabases: prev.allowedDatabases.includes(alias)
        ? prev.allowedDatabases.filter((d) => d !== alias)
        : [...prev.allowedDatabases, alias],
    }));
  }

  function toggleRoute(id: string) {
    setForm((prev) => ({
      ...prev,
      allowedRoutes: prev.allowedRoutes.includes(id)
        ? prev.allowedRoutes.filter((r) => r !== id)
        : [...prev.allowedRoutes, id],
    }));
  }

  function renderTags(items: string[], max = 3) {
    if (items.length === 0) return <span className="tag tag-more">{t('apiKeys.noneSelected')}</span>;
    const visible = items.slice(0, max);
    const rest = items.length - max;
    return (
      <>
        {visible.map((item) => <span key={item} className="tag">{item}</span>)}
        {rest > 0 && <span className="tag tag-more">+{rest}</span>}
      </>
    );
  }

  const isSaving = createMut.isPending || updateMut.isPending;

  return (
    <div className="api-keys">
      <div className="page-header">
        <div>
          <h1>{t('apiKeys.title')}</h1>
          <p className="page-subtitle">{t('apiKeys.subtitle')}</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>{t('apiKeys.addKey')}</button>
      </div>

      {keysQuery.isLoading && <div className="loading-message">{t('apiKeys.loadingKeys')}</div>}
      {keysQuery.isError && <div className="error-banner">{t('apiKeys.loadFailed')}</div>}

      {keys.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('apiKeys.keyName')}</th>
                <th>{t('apiKeys.description')}</th>
                <th>{t('apiKeys.apiKey')}</th>
                <th>{t('apiKeys.allowedDatabases')}</th>
                <th>{t('apiKeys.allowedRoutes')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.name}>
                  <td className="cell-alias">{k.name}</td>
                  <td>{k.description || '—'}</td>
                  <td className="cell-key">{k.api_key || '—'}</td>
                  <td><div className="cell-tags">{renderTags(k.allowed_databases)}</div></td>
                  <td><div className="cell-tags">{renderTags(k.allowed_routes)}</div></td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-sm btn-secondary" onClick={() => openEdit(k)}>{t('common.edit')}</button>
                      <button className="btn btn-sm btn-danger" onClick={() => handleDelete(k)} disabled={deleteMut.isPending}>{t('common.delete')}</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!keysQuery.isLoading && keys.length === 0 && !keysQuery.isError && (
        <div className="empty-state">
          <h3>{t('apiKeys.noKeys')}</h3>
          <p>{t('apiKeys.noKeysDesc')}</p>
        </div>
      )}

      {showModal && (
        <div className="modal-overlay" onClick={createdKey ? undefined : closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingName ? t('apiKeys.editTitle') : t('apiKeys.addTitle')}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>

            {createdKey ? (
              <>
                <div className="key-created-banner">
                  <p>{t('apiKeys.keyCreatedMessage')}</p>
                  <div className="key-display">
                    <code>{createdKey}</code>
                    <button className="copy-btn" onClick={handleCopy}>
                      {copied ? t('apiKeys.copied') : t('apiKeys.copy')}
                    </button>
                  </div>
                </div>
                <div className="modal-actions">
                  <button className="btn btn-primary" onClick={closeModal}>{t('common.done')}</button>
                </div>
              </>
            ) : (
              <form onSubmit={handleSubmit}>
                <div className="form-grid">
                  <div className="form-group">
                    <label>{t('apiKeys.keyName')}</label>
                    <input
                      value={form.name}
                      onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
                      placeholder="my-app"
                      required
                      disabled={!!editingName}
                    />
                  </div>
                  <div className="form-group">
                    <label>{t('apiKeys.description')}</label>
                    <input
                      value={form.description}
                      onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))}
                      placeholder={t('apiKeys.descriptionPlaceholder')}
                    />
                  </div>
                  <div className="form-group form-group--full">
                    <label>{t('apiKeys.apiKey')}</label>
                    <input
                      value={form.apiKey}
                      onChange={(e) => setForm((p) => ({ ...p, apiKey: e.target.value }))}
                      placeholder={editingName ? t('apiKeys.apiKeyPlaceholderEdit') : t('apiKeys.apiKeyPlaceholderNew')}
                    />
                    <button type="button" className="btn btn-sm btn-secondary generate-btn" onClick={() => setForm((p) => ({ ...p, apiKey: generateKey() }))}>
                      {t('apiKeys.generateKey')}
                    </button>
                  </div>
                  <div className="form-group form-group--full">
                    <label>{t('apiKeys.allowedDatabases')}</label>
                    <div className="checkbox-list">
                      {databases.length === 0 && <div className="checkbox-list-empty">{t('apiKeys.noneSelected')}</div>}
                      {databases.map((db) => (
                        <label key={db.alias}>
                          <input
                            type="checkbox"
                            checked={form.allowedDatabases.includes(db.alias)}
                            onChange={() => toggleDb(db.alias)}
                          />
                          {db.alias} <span className="tag">{db.db_type}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                  <div className="form-group form-group--full">
                    <label>{t('apiKeys.allowedRoutes')}</label>
                    <div className="checkbox-list">
                      {routes.length === 0 && <div className="checkbox-list-empty">{t('apiKeys.noneSelected')}</div>}
                      {routes.map((r) => (
                        <label key={r.id}>
                          <input
                            type="checkbox"
                            checked={form.allowedRoutes.includes(r.id)}
                            onChange={() => toggleRoute(r.id)}
                          />
                          {r.name || r.id} <span className="tag">{r.uri}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                </div>

                <div className="modal-actions">
                  <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
                  <button type="submit" className="btn btn-primary" disabled={isSaving}>
                    {isSaving ? t('common.saving') : editingName ? t('common.update') : t('common.create')}
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default ApiKeys;
```

- [ ] **Step 3: Delete GatewayConsumers files**

```bash
rm query-ui/src/pages/GatewayConsumers.tsx query-ui/src/pages/GatewayConsumers.css
```

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/pages/ApiKeys.tsx query-ui/src/pages/ApiKeys.css
git add -u query-ui/src/pages/GatewayConsumers.tsx query-ui/src/pages/GatewayConsumers.css
git commit -m "feat: replace GatewayConsumers with ApiKeys page"
```

---

## Task 11: Frontend — Navigation + Routing Update

**Files:**
- Modify: `query-ui/src/components/Layout.tsx`
- Modify: `query-ui/src/App.tsx`

- [ ] **Step 1: Update Layout.tsx navItems**

Replace the `navItems` array. Remove the `gatewayConsumers` entry. Add `apiKeys` entry as a new `access` section between `gateway` and `admin`:

```typescript
const navItems = [
  { to: '/', labelKey: 'nav.dashboard', icon: 'Dashboard', section: 'data', permission: null },
  { to: '/connections', labelKey: 'nav.connections', icon: 'Connections', section: 'data', permission: 'query.databases.read' },
  { to: '/permissions', labelKey: 'nav.permissions', icon: 'Permissions', section: 'data', permission: 'query.permissions.read' },
  { to: '/audit-logs', labelKey: 'nav.auditLogs', icon: 'Audit Logs', section: 'data', permission: 'query.audit.read' },
  { to: '/query', labelKey: 'nav.queryPlayground', icon: 'Query Playground', section: 'data', permission: 'query.execute' },
  { to: '/query-settings', labelKey: 'nav.querySettings', icon: 'Query Settings', section: 'data', permission: 'query.settings.read' },
  { to: '/gateway/routes', labelKey: 'nav.gatewayRoutes', icon: 'Gateway Routes', section: 'gateway', permission: 'gateway.routes.read' },
  { to: '/gateway/upstreams', labelKey: 'nav.gatewayUpstreams', icon: 'Gateway Upstreams', section: 'gateway', permission: 'gateway.upstreams.read' },
  { to: '/gateway/monitoring', labelKey: 'nav.gatewayMonitoring', icon: 'Gateway Monitoring', section: 'gateway', permission: 'gateway.monitoring.read' },
  { to: '/api-keys', labelKey: 'nav.apiKeys', icon: 'API Keys', section: 'access', permission: 'apikeys.read' },
  { to: '/roles', labelKey: 'nav.roles', icon: 'Roles', section: 'admin', permission: 'admin.roles.read' },
  { to: '/users', labelKey: 'nav.users', icon: 'Users', section: 'admin', permission: 'admin.roles.read' },
];
```

- [ ] **Step 2: Add API Keys icon SVG in Layout.tsx**

In the icon rendering section, add after the `Gateway Monitoring` icon block:

```tsx
{item.icon === 'API Keys' && (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
    <path d="M7 2a5 5 0 014.33 7.5L16 14.17V17h-3v-2h-2v-2l-1.17-1.17A5 5 0 117 2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    <circle cx="6" cy="7" r="1.5" fill="currentColor" />
  </svg>
)}
```

- [ ] **Step 3: Update App.tsx routes**

Replace the `GatewayConsumers` import and route with `ApiKeys`:

Remove:
```tsx
import GatewayConsumers from './pages/GatewayConsumers';
```
```tsx
<Route path="/gateway/consumers" element={<ProtectedRoute permission="gateway.consumers.read"><GatewayConsumers /></ProtectedRoute>} />
```

Add:
```tsx
import ApiKeys from './pages/ApiKeys';
```
```tsx
<Route path="/api-keys" element={<ProtectedRoute permission="apikeys.read"><ApiKeys /></ProtectedRoute>} />
```

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/components/Layout.tsx query-ui/src/App.tsx
git commit -m "feat: update navigation — replace Gateway Consumers with API Keys"
```

---

## Task 12: Frontend — Update Connections cURL to API Key

**Files:**
- Modify: `query-ui/src/pages/Connections.tsx`

- [ ] **Step 1: Update handleCurl to generate API key-based cURL**

Change the `handleCurl` function in `Connections.tsx`:

```typescript
async function handleCurl(alias: string) {
  let tableName = '<TABLE>';
  try {
    const tables = await getDbTables(alias);
    if (tables.length > 0) tableName = tables[0];
  } catch { /* use placeholder */ }
  const base = `${window.location.origin}/api/query/execute`;
  const body = JSON.stringify({ database: alias, sql: `SELECT * FROM ${tableName} LIMIT 10` }, null, 2);
  const curl = `curl -k -X POST \\\n  -H 'Content-Type: application/json' \\\n  -H 'apikey: <YOUR_API_KEY>' \\\n  '${base}' \\\n  -d '${body}'`;
  setCurlModal({ alias, curl });
  setCurlCopied(false);
}
```

Changes:
- Path: `/_api/query/execute` → `/api/query/execute`
- Header: `Authorization: Bearer <TOKEN>` → `apikey: <YOUR_API_KEY>`

- [ ] **Step 2: Commit**

```bash
git add query-ui/src/pages/Connections.tsx
git commit -m "feat: update DB cURL sample to use API key auth"
```

---

## Task 13: Full Integration Verification

- [ ] **Step 1: Run all backend tests**

```bash
cd /home/jinyoung/apihub/query-service && python -m pytest tests/ -v --no-header
```

Expected: All tests pass.

- [ ] **Step 2: Check frontend builds**

```bash
cd /home/jinyoung/apihub/query-ui && npx tsc --noEmit
```

Expected: No type errors.

- [ ] **Step 3: Check for leftover GatewayConsumer references**

```bash
grep -rn "GatewayConsumer\|gatewayConsumer\|gateway.consumers" --include="*.ts" --include="*.tsx" --include="*.py" --include="*.json" /home/jinyoung/apihub/query-ui/src/ /home/jinyoung/apihub/query-service/app/
```

Expected: No matches (except possibly `gateway.consumers.read` in `auth.py` `ALL_PERMISSIONS` — verify whether to remove or keep for backward compat).

- [ ] **Step 4: Clean up — remove gateway.consumers permissions if no longer needed**

If `gateway.consumers.read` and `gateway.consumers.write` are in `ALL_PERMISSIONS`, remove them and replace with `apikeys.read` / `apikeys.write` (done in Task 2). Verify they're gone.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: clean up leftover gateway consumer references"
```
