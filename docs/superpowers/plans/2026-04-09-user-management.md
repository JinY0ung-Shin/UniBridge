# User Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** API Hub UI에서 Keycloak 사용자를 관리(조회/생성/role 변경/비밀번호 초기화/삭제)할 수 있는 기능 추가

**Architecture:** query-service가 Keycloak Admin REST API를 프록시. Service Account(Client Credentials Grant)로 인증. 프론트엔드는 기존 테이블+모달 패턴으로 Users 페이지 추가.

**Tech Stack:** FastAPI, httpx, Pydantic, React 19, TypeScript, TanStack React Query, Keycloak 26 Admin REST API

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `query-service/app/keycloak_admin.py` | Keycloak Admin REST API client (token 관리 + CRUD) |
| Create | `query-service/app/routers/users.py` | User management endpoints |
| Create | `query-service/tests/test_users.py` | Backend user endpoint tests |
| Create | `query-ui/src/pages/Users.tsx` | Users 페이지 컴포넌트 |
| Create | `query-ui/src/pages/Users.css` | Users 페이지 스타일 |
| Modify | `query-service/app/config.py` | Keycloak Service Account 환경변수 추가 |
| Modify | `query-service/app/schemas.py` | User 관련 Pydantic 모델 추가 |
| Modify | `query-service/app/main.py` | users router 등록 |
| Modify | `query-ui/src/api/client.ts` | User API 함수 + 타입 추가 |
| Modify | `query-ui/src/App.tsx` | `/users` 라우트 추가 |
| Modify | `query-ui/src/components/Layout.tsx` | 사이드바 Users 메뉴 추가 |
| Modify | `keycloak/realm-export.json` | apihub-service client 추가 |

---

### Task 1: Keycloak realm에 Service Account client 추가

**Files:**
- Modify: `keycloak/realm-export.json`

- [ ] **Step 1: realm-export.json에 apihub-service client 추가**

`keycloak/realm-export.json`의 `clients` 배열(현재 `apihub-ui` 하나)에 두 번째 client를 추가한다.

```json
{
  "clientId": "apihub-service",
  "name": "API Hub Service Account",
  "enabled": true,
  "publicClient": false,
  "secret": "apihub-service-secret",
  "serviceAccountsEnabled": true,
  "directAccessGrantsEnabled": false,
  "standardFlowEnabled": false
}
```

기존 `clients` 배열 닫는 `]` 앞에 추가.

- [ ] **Step 2: Service Account에 realm-management role 매핑 추가**

같은 파일 최상위에 `servicAccountClientScopes` 대신 `clientScopeMappings` 블록을 추가한다. `"roles"` 키 옆, `"users"` 키 뒤의 top-level에:

```json
"scopeMappings": [
  {
    "clientScope": "apihub-service",
    "roles": ["admin"]
  }
]
```

> Note: Keycloak realm import에서 service account에 manage-users 권한을 부여하는 가장 간단한 방법은 admin realm role을 매핑하는 것이다. 프로덕션에서는 realm-management client role로 세분화할 수 있지만, 초기 구현에서는 admin role로 충분하다.

- [ ] **Step 3: Commit**

```bash
git add keycloak/realm-export.json
git commit -m "feat(keycloak): add apihub-service client for user management API"
```

---

### Task 2: Backend 환경변수 및 Pydantic 스키마 추가

**Files:**
- Modify: `query-service/app/config.py`
- Modify: `query-service/app/schemas.py`

- [ ] **Step 1: config.py에 Keycloak Service Account 설정 추가**

`query-service/app/config.py`의 `Settings` 클래스에 Keycloak 관련 설정 3개 추가. 기존 `KEYCLOAK_JWT_AUDIENCE` (line 21) 아래에:

```python
    # Keycloak Service Account (for user management)
    KEYCLOAK_URL: str = ""
    KEYCLOAK_REALM: str = "apihub"
    KEYCLOAK_SERVICE_CLIENT_ID: str = "apihub-service"
    KEYCLOAK_SERVICE_CLIENT_SECRET: str = ""
```

- [ ] **Step 2: schemas.py에 User management 스키마 추가**

`query-service/app/schemas.py` 끝에 (line 159 `TokenResponse` 뒤) 추가:

```python
# ── Users (Keycloak) ────────────────────────────────────────────────────────

class KeycloakUser(BaseModel):
    id: str
    username: str
    email: str | None = None
    enabled: bool = True
    role: str | None = None
    createdTimestamp: int | None = None


class KeycloakUserList(BaseModel):
    users: list[KeycloakUser]
    total: int


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    email: str | None = None
    password: str = Field(..., min_length=8)
    role: str = Field(..., min_length=1)


class ChangeRoleRequest(BaseModel):
    role: str = Field(..., min_length=1)


class ResetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=8)
    temporary: bool = True
```

- [ ] **Step 3: Commit**

```bash
git add query-service/app/config.py query-service/app/schemas.py
git commit -m "feat: add Keycloak service account config and user management schemas"
```

---

### Task 3: Keycloak Admin Client 모듈 구현

**Files:**
- Create: `query-service/app/keycloak_admin.py`
- Create: `query-service/tests/test_keycloak_admin.py`

- [ ] **Step 1: test_keycloak_admin.py에 토큰 발급 테스트 작성**

```python
"""Tests for Keycloak Admin Client."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from app.keycloak_admin import KeycloakAdminClient


@pytest.fixture
def kc_client():
    return KeycloakAdminClient(
        base_url="http://keycloak:8080",
        realm="apihub",
        client_id="apihub-service",
        client_secret="test-secret",
    )


class TestGetToken:
    @pytest.mark.asyncio
    async def test_fetches_token_on_first_call(self, kc_client):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new-token",
            "expires_in": 300,
        }
        mock_response.raise_for_status = AsyncMock()

        with patch("app.keycloak_admin.httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            mock_client_instance.post.return_value = mock_response
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client_instance

            token = await kc_client.get_token()
            assert token == "new-token"
            mock_client_instance.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_cached_token_if_not_expired(self, kc_client):
        kc_client._token = "cached-token"
        kc_client._token_expires_at = time.time() + 300

        token = await kc_client.get_token()
        assert token == "cached-token"
```

- [ ] **Step 2: 토큰 테스트 실행 — 실패 확인**

```bash
cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_keycloak_admin.py -v
```

Expected: FAIL (module not found)

- [ ] **Step 3: keycloak_admin.py 구현**

`query-service/app/keycloak_admin.py`:

```python
"""Keycloak Admin REST API client using Service Account credentials."""
from __future__ import annotations

import logging
import time

import httpx
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


class KeycloakAdminClient:
    """Keycloak Admin REST API client.

    Uses Client Credentials Grant to obtain a service account token,
    then proxies user management operations to Keycloak.
    """

    def __init__(
        self,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ── Token management ──────────────────────────────────────────────

    async def get_token(self) -> str:
        """Get a valid access token, refreshing if expired."""
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token

        token_url = f"{self.base_url}/realms/{self.realm}/protocol/openid-connect/token"
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        self._token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 300)
        return self._token

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an authenticated request to Keycloak Admin API."""
        token = await self.get_token()
        url = f"{self.base_url}/admin/realms/{self.realm}{path}"
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            resp = await getattr(client, method)(
                url,
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        return resp

    # ── User CRUD ─────────────────────────────────────────────────────

    async def list_users(
        self, search: str | None = None, first: int = 0, max_results: int = 50
    ) -> tuple[list[dict], int]:
        """List users with optional search. Returns (users, total_count)."""
        params: dict = {"first": first, "max": max_results, "briefRepresentation": "false"}
        if search:
            params["search"] = search

        resp = await self._request("get", "/users", params=params)
        resp.raise_for_status()
        users = resp.json()

        # Get total count
        count_params: dict = {}
        if search:
            count_params["search"] = search
        count_resp = await self._request("get", "/users/count", params=count_params)
        count_resp.raise_for_status()
        total = count_resp.json()

        return users, total

    async def create_user(
        self, username: str, email: str | None, password: str, enabled: bool = True
    ) -> str:
        """Create a user and return their Keycloak ID."""
        payload: dict = {
            "username": username,
            "enabled": enabled,
            "credentials": [{"type": "password", "value": password, "temporary": True}],
        }
        if email:
            payload["email"] = email

        resp = await self._request("post", "/users", json=payload)
        if resp.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"User '{username}' already exists",
            )
        resp.raise_for_status()

        # Extract user ID from Location header
        location = resp.headers.get("Location", "")
        user_id = location.rsplit("/", 1)[-1]
        return user_id

    async def delete_user(self, user_id: str) -> None:
        """Delete a user by ID."""
        resp = await self._request("delete", f"/users/{user_id}")
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        resp.raise_for_status()

    async def reset_password(
        self, user_id: str, password: str, temporary: bool = True
    ) -> None:
        """Reset a user's password."""
        resp = await self._request(
            "put",
            f"/users/{user_id}/reset-password",
            json={"type": "password", "value": password, "temporary": temporary},
        )
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        resp.raise_for_status()

    # ── Role management ───────────────────────────────────────────────

    async def get_realm_roles(self) -> list[dict]:
        """Get all realm roles."""
        resp = await self._request("get", "/roles")
        resp.raise_for_status()
        return resp.json()

    async def get_user_realm_roles(self, user_id: str) -> list[dict]:
        """Get realm roles assigned to a user."""
        resp = await self._request("get", f"/users/{user_id}/role-mappings/realm")
        resp.raise_for_status()
        return resp.json()

    async def assign_realm_role(self, user_id: str, role_name: str) -> None:
        """Assign a realm role to a user."""
        # First, get the role representation (Keycloak needs id + name)
        roles = await self.get_realm_roles()
        role_rep = next((r for r in roles if r["name"] == role_name), None)
        if role_rep is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Role '{role_name}' not found in Keycloak",
            )
        resp = await self._request(
            "post",
            f"/users/{user_id}/role-mappings/realm",
            json=[{"id": role_rep["id"], "name": role_rep["name"]}],
        )
        resp.raise_for_status()

    async def remove_realm_role(self, user_id: str, role_name: str) -> None:
        """Remove a realm role from a user."""
        roles = await self.get_realm_roles()
        role_rep = next((r for r in roles if r["name"] == role_name), None)
        if role_rep is None:
            return  # Role doesn't exist, nothing to remove
        resp = await self._request(
            "delete",
            f"/users/{user_id}/role-mappings/realm",
            json=[{"id": role_rep["id"], "name": role_rep["name"]}],
        )
        resp.raise_for_status()
```

- [ ] **Step 4: 토큰 테스트 실행 — 성공 확인**

```bash
cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_keycloak_admin.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add query-service/app/keycloak_admin.py query-service/tests/test_keycloak_admin.py
git commit -m "feat: implement Keycloak Admin REST API client with token caching"
```

---

### Task 4: Users router 엔드포인트 구현

**Files:**
- Create: `query-service/app/routers/users.py`
- Modify: `query-service/app/main.py`

- [ ] **Step 1: users.py router 구현**

`query-service/app/routers/users.py`:

```python
"""User management endpoints — proxies Keycloak Admin REST API."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import CurrentUser, require_permission, ROLE_PRIORITY
from app.config import settings
from app.keycloak_admin import KeycloakAdminClient
from app.schemas import (
    ChangeRoleRequest,
    CreateUserRequest,
    KeycloakUser,
    KeycloakUserList,
    ResetPasswordRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Users"])


def _get_kc_admin() -> KeycloakAdminClient:
    """Create a Keycloak Admin client from settings."""
    if not settings.KEYCLOAK_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Keycloak URL not configured",
        )
    return KeycloakAdminClient(
        base_url=settings.KEYCLOAK_URL,
        realm=settings.KEYCLOAK_REALM,
        client_id=settings.KEYCLOAK_SERVICE_CLIENT_ID,
        client_secret=settings.KEYCLOAK_SERVICE_CLIENT_SECRET,
    )


def _resolve_role(realm_roles: list[dict]) -> str | None:
    """Pick the highest-priority app role from Keycloak realm roles."""
    role_names = {r["name"] for r in realm_roles}
    return next((r for r in ROLE_PRIORITY if r in role_names), None)


async def _enrich_user(kc: KeycloakAdminClient, user: dict) -> KeycloakUser:
    """Convert a Keycloak user dict to our response model with role."""
    user_roles = await kc.get_user_realm_roles(user["id"])
    role = _resolve_role(user_roles)
    return KeycloakUser(
        id=user["id"],
        username=user["username"],
        email=user.get("email"),
        enabled=user.get("enabled", True),
        role=role,
        createdTimestamp=user.get("createdTimestamp"),
    )


@router.get("/admin/users", response_model=KeycloakUserList)
async def list_users(
    search: str | None = None,
    first: int = 0,
    max: int = 50,
    _user: CurrentUser = Depends(require_permission("admin.roles.read")),
) -> KeycloakUserList:
    """List Keycloak users with their app roles."""
    kc = _get_kc_admin()
    users, total = await kc.list_users(search=search, first=first, max_results=max)
    enriched = [await _enrich_user(kc, u) for u in users]
    return KeycloakUserList(users=enriched, total=total)


@router.post(
    "/admin/users",
    response_model=KeycloakUser,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    body: CreateUserRequest,
    _user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> KeycloakUser:
    """Create a Keycloak user with a role assignment."""
    kc = _get_kc_admin()
    user_id = await kc.create_user(
        username=body.username,
        email=body.email,
        password=body.password,
    )
    await kc.assign_realm_role(user_id, body.role)

    return KeycloakUser(
        id=user_id,
        username=body.username,
        email=body.email,
        enabled=True,
        role=body.role,
    )


@router.put("/admin/users/{user_id}/role", response_model=KeycloakUser)
async def change_user_role(
    user_id: str,
    body: ChangeRoleRequest,
    _user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> KeycloakUser:
    """Change a user's realm role."""
    kc = _get_kc_admin()

    # Remove existing app roles
    current_roles = await kc.get_user_realm_roles(user_id)
    for role in current_roles:
        if role["name"] in ROLE_PRIORITY:
            await kc.remove_realm_role(user_id, role["name"])

    # Assign new role
    await kc.assign_realm_role(user_id, body.role)

    # Fetch updated user info
    users, _ = await kc.list_users()
    user_data = next((u for u in users if u["id"] == user_id), None)
    if user_data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return await _enrich_user(kc, user_data)


@router.put(
    "/admin/users/{user_id}/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def reset_password(
    user_id: str,
    body: ResetPasswordRequest,
    _user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> None:
    """Reset a user's password."""
    kc = _get_kc_admin()
    await kc.reset_password(user_id, body.password, body.temporary)


@router.delete(
    "/admin/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_user(
    user_id: str,
    user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> None:
    """Delete a Keycloak user. Cannot delete yourself."""
    # Prevent self-deletion: compare current user's username with target
    kc = _get_kc_admin()
    users, _ = await kc.list_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if target and target["username"] == user.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )
    await kc.delete_user(user_id)
```

- [ ] **Step 2: main.py에 users router 등록**

`query-service/app/main.py` line 14의 import에 users 추가:

```python
from app.routers import admin, gateway, query, roles, users
```

line 67 `app.include_router(roles.router)` 뒤에 추가:

```python
app.include_router(users.router)
```

- [ ] **Step 3: Commit**

```bash
git add query-service/app/routers/users.py query-service/app/main.py
git commit -m "feat: add user management API endpoints proxying Keycloak Admin API"
```

---

### Task 5: Backend 엔드포인트 테스트

**Files:**
- Create: `query-service/tests/test_users.py`

- [ ] **Step 1: test_users.py 작성**

Keycloak Admin Client를 모킹하여 엔드포인트 로직을 테스트한다.

```python
"""Tests for user management endpoints."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import auth_header


@pytest.fixture
def mock_kc_client():
    """Mocked KeycloakAdminClient."""
    mock = AsyncMock()
    mock.list_users.return_value = (
        [
            {
                "id": "user-1",
                "username": "testuser",
                "email": "test@example.com",
                "enabled": True,
                "createdTimestamp": 1700000000000,
            }
        ],
        1,
    )
    mock.get_user_realm_roles.return_value = [
        {"id": "role-id-1", "name": "developer"},
        {"id": "role-id-default", "name": "default-roles-apihub"},
    ]
    mock.create_user.return_value = "new-user-id"
    mock.assign_realm_role.return_value = None
    mock.remove_realm_role.return_value = None
    mock.delete_user.return_value = None
    mock.reset_password.return_value = None
    mock.get_realm_roles.return_value = [
        {"id": "r1", "name": "admin"},
        {"id": "r2", "name": "developer"},
        {"id": "r3", "name": "viewer"},
    ]
    return mock


class TestListUsers:
    @pytest.mark.asyncio
    async def test_list_users_success(self, client, admin_token, mock_kc_client):
        with patch("app.routers.users._get_kc_admin", return_value=mock_kc_client):
            resp = await client.get("/admin/users", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["users"][0]["username"] == "testuser"
        assert data["users"][0]["role"] == "developer"

    @pytest.mark.asyncio
    async def test_list_users_forbidden_for_viewer(self, client, viewer_token):
        resp = await client.get("/admin/users", headers=auth_header(viewer_token))
        assert resp.status_code == 403


class TestCreateUser:
    @pytest.mark.asyncio
    async def test_create_user_success(self, client, admin_token, mock_kc_client):
        with patch("app.routers.users._get_kc_admin", return_value=mock_kc_client):
            resp = await client.post(
                "/admin/users",
                json={
                    "username": "newuser",
                    "email": "new@example.com",
                    "password": "password123",
                    "role": "developer",
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "newuser"
        assert data["role"] == "developer"
        mock_kc_client.create_user.assert_called_once()
        mock_kc_client.assign_realm_role.assert_called_once_with("new-user-id", "developer")

    @pytest.mark.asyncio
    async def test_create_user_short_password(self, client, admin_token, mock_kc_client):
        with patch("app.routers.users._get_kc_admin", return_value=mock_kc_client):
            resp = await client.post(
                "/admin/users",
                json={
                    "username": "newuser",
                    "password": "short",
                    "role": "viewer",
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 422


class TestChangeRole:
    @pytest.mark.asyncio
    async def test_change_role_success(self, client, admin_token, mock_kc_client):
        with patch("app.routers.users._get_kc_admin", return_value=mock_kc_client):
            resp = await client.put(
                "/admin/users/user-1/role",
                json={"role": "admin"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        mock_kc_client.remove_realm_role.assert_called_once_with("user-1", "developer")
        mock_kc_client.assign_realm_role.assert_called_once_with("user-1", "admin")


class TestResetPassword:
    @pytest.mark.asyncio
    async def test_reset_password_success(self, client, admin_token, mock_kc_client):
        with patch("app.routers.users._get_kc_admin", return_value=mock_kc_client):
            resp = await client.put(
                "/admin/users/user-1/reset-password",
                json={"password": "newpassword123", "temporary": True},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 204
        mock_kc_client.reset_password.assert_called_once_with("user-1", "newpassword123", True)


class TestDeleteUser:
    @pytest.mark.asyncio
    async def test_delete_user_success(self, client, admin_token, mock_kc_client):
        with patch("app.routers.users._get_kc_admin", return_value=mock_kc_client):
            resp = await client.delete(
                "/admin/users/user-1",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 204
        mock_kc_client.delete_user.assert_called_once_with("user-1")

    @pytest.mark.asyncio
    async def test_cannot_delete_self(self, client, admin_token, mock_kc_client):
        # mock_kc_client.list_users returns user with username "testuser",
        # but the admin token has username "testadmin" — so we need to adjust
        mock_kc_client.list_users.return_value = (
            [{"id": "self-id", "username": "testadmin", "enabled": True}],
            1,
        )
        with patch("app.routers.users._get_kc_admin", return_value=mock_kc_client):
            resp = await client.delete(
                "/admin/users/self-id",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert "own account" in resp.json()["detail"]
```

- [ ] **Step 2: 테스트 실행**

```bash
cd /home/jinyoung/apihub/query-service && python -m pytest tests/test_users.py -v
```

Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add query-service/tests/test_users.py
git commit -m "test: add user management endpoint tests with mocked Keycloak client"
```

---

### Task 6: Frontend API client 함수 및 타입 추가

**Files:**
- Modify: `query-ui/src/api/client.ts`

- [ ] **Step 1: client.ts에 User 관련 타입과 API 함수 추가**

`query-ui/src/api/client.ts` line 396 (`export default client;` 바로 위) 앞에 추가:

```typescript
/* ── Admin: Users (Keycloak) ── */

export interface KeycloakUser {
  id: string;
  username: string;
  email: string | null;
  enabled: boolean;
  role: string | null;
  createdTimestamp: number | null;
}

export interface KeycloakUserList {
  users: KeycloakUser[];
  total: number;
}

export interface CreateUserBody {
  username: string;
  email?: string;
  password: string;
  role: string;
}

export interface ResetPasswordBody {
  password: string;
  temporary: boolean;
}

export async function getUsers(params?: { search?: string; first?: number; max?: number }): Promise<KeycloakUserList> {
  const { data } = await client.get('/admin/users', { params });
  return data;
}

export async function createKeycloakUser(body: CreateUserBody): Promise<KeycloakUser> {
  const { data } = await client.post('/admin/users', body);
  return data;
}

export async function changeUserRole(userId: string, role: string): Promise<KeycloakUser> {
  const { data } = await client.put(`/admin/users/${userId}/role`, { role });
  return data;
}

export async function resetUserPassword(userId: string, body: ResetPasswordBody): Promise<void> {
  await client.put(`/admin/users/${userId}/reset-password`, body);
}

export async function deleteKeycloakUser(userId: string): Promise<void> {
  await client.delete(`/admin/users/${userId}`);
}
```

- [ ] **Step 2: Commit**

```bash
git add query-ui/src/api/client.ts
git commit -m "feat(ui): add user management API client functions and types"
```

---

### Task 7: Users 페이지 컴포넌트 구현

**Files:**
- Create: `query-ui/src/pages/Users.tsx`
- Create: `query-ui/src/pages/Users.css`

- [ ] **Step 1: Users.css 스타일 작성**

`query-ui/src/pages/Users.css`:

```css
.users-page .search-bar {
  margin-bottom: 16px;
}

.users-page .search-bar input {
  width: 100%;
  max-width: 400px;
}

.users-page .role-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 12px;
  font-size: 0.8rem;
  font-weight: 500;
}

.users-page .role-badge--admin {
  background: rgba(99, 102, 241, 0.2);
  color: #818cf8;
}

.users-page .role-badge--developer {
  background: rgba(14, 116, 144, 0.2);
  color: #22d3ee;
}

.users-page .role-badge--viewer {
  background: rgba(107, 114, 128, 0.2);
  color: #9ca3af;
}

.users-page .role-badge--default {
  background: rgba(107, 114, 128, 0.15);
  color: #6b7280;
}

.users-page .status-active {
  color: #22c55e;
}

.users-page .status-disabled {
  color: #ef4444;
}

.users-page .row-disabled {
  opacity: 0.6;
}
```

- [ ] **Step 2: Users.tsx 컴포넌트 작성**

`query-ui/src/pages/Users.tsx`:

```tsx
import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getUsers,
  createKeycloakUser,
  changeUserRole,
  resetUserPassword,
  deleteKeycloakUser,
  getAuthRoles,
  type KeycloakUser,
} from '../api/client';
import { usePermissions } from '../components/PermissionContext';
import { useAuth } from '../components/AuthProvider';
import './Users.css';

type ModalMode = 'create' | 'role' | 'password' | null;

function Users() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const { username: currentUsername } = useAuth();
  const canWrite = permissions.includes('admin.roles.write');

  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [modalMode, setModalMode] = useState<ModalMode>(null);
  const [selectedUser, setSelectedUser] = useState<KeycloakUser | null>(null);
  const [error, setError] = useState('');

  // Form fields
  const [formUsername, setFormUsername] = useState('');
  const [formEmail, setFormEmail] = useState('');
  const [formPassword, setFormPassword] = useState('');
  const [formRole, setFormRole] = useState('');
  const [formTemporary, setFormTemporary] = useState(true);

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const usersQuery = useQuery({
    queryKey: ['users', debouncedSearch],
    queryFn: () => getUsers({ search: debouncedSearch || undefined }),
  });

  const rolesQuery = useQuery({
    queryKey: ['auth-roles'],
    queryFn: getAuthRoles,
  });

  const createMutation = useMutation({
    mutationFn: createKeycloakUser,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      closeModal();
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: string } } };
      setError(axiosErr.response?.data?.detail ?? 'Failed to create user');
    },
  });

  const roleMutation = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) =>
      changeUserRole(userId, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      closeModal();
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: string } } };
      setError(axiosErr.response?.data?.detail ?? 'Failed to change role');
    },
  });

  const passwordMutation = useMutation({
    mutationFn: ({ userId, password, temporary }: { userId: string; password: string; temporary: boolean }) =>
      resetUserPassword(userId, { password, temporary }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      closeModal();
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: string } } };
      setError(axiosErr.response?.data?.detail ?? 'Failed to reset password');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteKeycloakUser,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: string } } };
      alert(axiosErr.response?.data?.detail ?? 'Failed to delete user');
    },
  });

  const users = usersQuery.data?.users ?? [];
  const roles = rolesQuery.data ?? [];

  function openCreate() {
    setModalMode('create');
    setFormUsername('');
    setFormEmail('');
    setFormPassword('');
    setFormRole(roles[0] ?? '');
    setError('');
  }

  function openRoleChange(user: KeycloakUser) {
    setModalMode('role');
    setSelectedUser(user);
    setFormRole(user.role ?? '');
    setError('');
  }

  function openPasswordReset(user: KeycloakUser) {
    setModalMode('password');
    setSelectedUser(user);
    setFormPassword('');
    setFormTemporary(true);
    setError('');
  }

  function closeModal() {
    setModalMode(null);
    setSelectedUser(null);
    setError('');
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');

    if (modalMode === 'create') {
      createMutation.mutate({
        username: formUsername.trim(),
        email: formEmail.trim() || undefined,
        password: formPassword,
        role: formRole,
      });
    } else if (modalMode === 'role' && selectedUser) {
      roleMutation.mutate({ userId: selectedUser.id, role: formRole });
    } else if (modalMode === 'password' && selectedUser) {
      passwordMutation.mutate({
        userId: selectedUser.id,
        password: formPassword,
        temporary: formTemporary,
      });
    }
  }

  function handleDelete(user: KeycloakUser) {
    if (window.confirm(`Delete user "${user.username}"?`)) {
      deleteMutation.mutate(user.id);
    }
  }

  function roleBadgeClass(role: string | null): string {
    if (!role) return 'role-badge role-badge--default';
    if (role === 'admin') return 'role-badge role-badge--admin';
    if (role === 'developer') return 'role-badge role-badge--developer';
    if (role === 'viewer') return 'role-badge role-badge--viewer';
    return 'role-badge role-badge--default';
  }

  const isSaving = createMutation.isPending || roleMutation.isPending || passwordMutation.isPending;

  return (
    <div className="users-page">
      <div className="page-header">
        <div>
          <h1>Users</h1>
          <p className="page-subtitle">Manage Keycloak users and role assignments</p>
        </div>
        {canWrite && (
          <button className="btn btn-primary" onClick={openCreate}>+ Add User</button>
        )}
      </div>

      <div className="search-bar">
        <input
          type="text"
          placeholder="Search users..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {usersQuery.isLoading && <div className="loading-message">Loading users...</div>}
      {usersQuery.isError && <div className="error-banner">Failed to load users.</div>}

      {users.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Username</th>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id} className={!user.enabled ? 'row-disabled' : ''}>
                  <td className="cell-alias">{user.username}</td>
                  <td>{user.email || '—'}</td>
                  <td><span className={roleBadgeClass(user.role)}>{user.role || '—'}</span></td>
                  <td>
                    <span className={user.enabled ? 'status-active' : 'status-disabled'}>
                      {user.enabled ? '● Active' : '● Disabled'}
                    </span>
                  </td>
                  <td>
                    <div className="action-buttons">
                      {canWrite && (
                        <>
                          <button className="btn btn-sm btn-secondary" onClick={() => openRoleChange(user)}>
                            Role
                          </button>
                          <button className="btn btn-sm btn-secondary" onClick={() => openPasswordReset(user)}>
                            Reset PW
                          </button>
                          {user.username !== currentUsername && (
                            <button
                              className="btn btn-sm btn-danger"
                              onClick={() => handleDelete(user)}
                              disabled={deleteMutation.isPending}
                            >
                              Delete
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!usersQuery.isLoading && users.length === 0 && (
        <div className="loading-message">No users found.</div>
      )}

      {modalMode && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>
                {modalMode === 'create' && 'Add User'}
                {modalMode === 'role' && `Change Role — ${selectedUser?.username}`}
                {modalMode === 'password' && `Reset Password — ${selectedUser?.username}`}
              </h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handleSubmit}>
              <div className="form-grid">
                {modalMode === 'create' && (
                  <>
                    <div className="form-group form-group--full">
                      <label>Username</label>
                      <input
                        value={formUsername}
                        onChange={(e) => setFormUsername(e.target.value)}
                        placeholder="username"
                        required
                      />
                    </div>
                    <div className="form-group form-group--full">
                      <label>Email (optional)</label>
                      <input
                        type="email"
                        value={formEmail}
                        onChange={(e) => setFormEmail(e.target.value)}
                        placeholder="user@example.com"
                      />
                    </div>
                    <div className="form-group form-group--full">
                      <label>Password</label>
                      <input
                        type="password"
                        value={formPassword}
                        onChange={(e) => setFormPassword(e.target.value)}
                        placeholder="Minimum 8 characters"
                        required
                        minLength={8}
                      />
                    </div>
                    <div className="form-group form-group--full">
                      <label>Role</label>
                      <select value={formRole} onChange={(e) => setFormRole(e.target.value)} required>
                        {roles.map((r) => (
                          <option key={r} value={r}>{r}</option>
                        ))}
                      </select>
                    </div>
                  </>
                )}

                {modalMode === 'role' && (
                  <div className="form-group form-group--full">
                    <label>New Role</label>
                    <select value={formRole} onChange={(e) => setFormRole(e.target.value)} required>
                      {roles.map((r) => (
                        <option key={r} value={r}>{r}</option>
                      ))}
                    </select>
                  </div>
                )}

                {modalMode === 'password' && (
                  <>
                    <div className="form-group form-group--full">
                      <label>New Password</label>
                      <input
                        type="password"
                        value={formPassword}
                        onChange={(e) => setFormPassword(e.target.value)}
                        placeholder="Minimum 8 characters"
                        required
                        minLength={8}
                      />
                    </div>
                    <div className="form-group form-group--full">
                      <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <input
                          type="checkbox"
                          checked={formTemporary}
                          onChange={(e) => setFormTemporary(e.target.checked)}
                        />
                        Require password change on next login
                      </label>
                    </div>
                  </>
                )}
              </div>

              {error && <div className="form-error">{error}</div>}

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={isSaving}>
                  {isSaving ? 'Saving...' : modalMode === 'create' ? 'Create' : modalMode === 'role' ? 'Update Role' : 'Reset Password'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default Users;
```

- [ ] **Step 3: Commit**

```bash
git add query-ui/src/pages/Users.tsx query-ui/src/pages/Users.css
git commit -m "feat(ui): add Users page component with table, modals for CRUD operations"
```

---

### Task 8: 라우팅 및 사이드바 메뉴 연결

**Files:**
- Modify: `query-ui/src/App.tsx`
- Modify: `query-ui/src/components/Layout.tsx`

- [ ] **Step 1: App.tsx에 Users 라우트 추가**

`query-ui/src/App.tsx` line 14 (`import Roles`) 뒤에 import 추가:

```typescript
import Users from './pages/Users';
```

line 39 (`/roles` 라우트) 뒤에 라우트 추가:

```tsx
        <Route path="/users" element={<ProtectedRoute permission="admin.roles.read"><Users /></ProtectedRoute>} />
```

- [ ] **Step 2: Layout.tsx 사이드바에 Users 메뉴 추가**

`query-ui/src/components/Layout.tsx` line 18 (`Roles` 항목) 뒤에 추가:

```typescript
  { to: '/users', label: 'Users', section: 'admin', permission: 'admin.roles.read' },
```

같은 파일의 nav icon 영역 (line 127-134, `Roles` 아이콘 뒤)에 Users 아이콘 추가:

```tsx
                    {item.label === 'Users' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <circle cx="9" cy="5" r="3" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M3 16c0-2.8 2.7-5 6-5s6 2.2 6 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
```

- [ ] **Step 3: TypeScript 빌드 확인**

```bash
cd /home/jinyoung/apihub/query-ui && npx tsc --noEmit
```

Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/App.tsx query-ui/src/components/Layout.tsx
git commit -m "feat(ui): add Users route and sidebar navigation menu"
```

---

### Task 9: 통합 검증

- [ ] **Step 1: Backend 전체 테스트**

```bash
cd /home/jinyoung/apihub/query-service && python -m pytest tests/ -v
```

Expected: All tests pass

- [ ] **Step 2: Frontend 빌드 확인**

```bash
cd /home/jinyoung/apihub/query-ui && npm run build
```

Expected: Build succeeds without errors

- [ ] **Step 3: 최종 커밋 (필요 시)**

빌드/테스트에서 수정이 필요한 부분이 있다면 수정 후 커밋.
