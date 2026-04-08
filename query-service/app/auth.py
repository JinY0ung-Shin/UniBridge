from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db

security = HTTPBearer()

# ── All permissions ─────────────────────────────────────────────────────────

ALL_PERMISSIONS = [
    "query.databases.read",
    "query.databases.write",
    "query.permissions.read",
    "query.permissions.write",
    "query.audit.read",
    "query.execute",
    "gateway.routes.read",
    "gateway.routes.write",
    "gateway.upstreams.read",
    "gateway.upstreams.write",
    "gateway.consumers.read",
    "gateway.consumers.write",
    "gateway.monitoring.read",
    "admin.roles.read",
    "admin.roles.write",
]

# ── Permission cache ────────────────────────────────────────────────────────

_perm_cache: dict[str, set[str]] = {}
_perm_cache_ts: float = 0.0
_CACHE_TTL = 60.0  # seconds
_cache_lock = asyncio.Lock()


async def get_role_permissions(db: AsyncSession, role_name: str) -> set[str]:
    """Get permissions for a role, using in-memory cache with 60s TTL."""
    now = time.time()
    if now - _perm_cache_ts > _CACHE_TTL:
        async with _cache_lock:
            # Double-check after acquiring lock to avoid thundering herd
            if time.time() - _perm_cache_ts > _CACHE_TTL:
                await _refresh_cache(db)

    return _perm_cache.get(role_name, set())


async def _refresh_cache(db: AsyncSession) -> None:
    global _perm_cache, _perm_cache_ts
    from app.models import Role, RolePermission

    result = await db.execute(
        select(Role.name, RolePermission.permission)
        .join(RolePermission, Role.id == RolePermission.role_id)
    )
    cache: dict[str, set[str]] = {}
    for role_name, permission in result.all():
        cache.setdefault(role_name, set()).add(permission)
    _perm_cache = cache
    _perm_cache_ts = time.time()


def invalidate_permission_cache() -> None:
    """Call after role/permission changes to force cache refresh."""
    global _perm_cache, _perm_cache_ts
    _perm_cache = {}
    _perm_cache_ts = 0.0


# ── User ────────────────────────────────────────────────────────────────────

@dataclass
class CurrentUser:
    username: str
    role: str


def create_token(username: str, role: str, expires_delta: timedelta | None = None) -> str:
    """Create a JWT token for the given user and role."""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=8))
    payload = {
        "sub": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentUser:
    """FastAPI dependency: verify JWT and return the current user."""
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        username: str | None = payload.get("sub")
        role: str | None = payload.get("role")
        if username is None or role is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject or role",
            )
        return CurrentUser(username=username, role=role)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc


# ── Permission dependencies ─────────────────────────────────────────────────

def require_permission(*perms: str) -> Callable:
    """FastAPI dependency factory: require any of the given permissions."""
    async def checker(
        user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> CurrentUser:
        user_perms = await get_role_permissions(db, user.role)
        if not any(p in user_perms for p in perms):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required permission: {' or '.join(perms)}",
            )
        return user
    return checker
