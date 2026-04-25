from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt.exceptions import PyJWTError as JWTError
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db

logger = logging.getLogger(__name__)

security = HTTPBearer()

# ── All permissions ─────────────────────────────────────────────────────────

ALL_PERMISSIONS = [
    "query.databases.read",
    "query.databases.write",
    "query.permissions.read",
    "query.permissions.write",
    "query.audit.read",
    "query.execute",
    "query.settings.read",
    "query.settings.write",
    "gateway.routes.read",
    "gateway.routes.write",
    "gateway.upstreams.read",
    "gateway.upstreams.write",
    "gateway.monitoring.read",
    "apikeys.read",
    "apikeys.write",
    "admin.roles.read",
    "admin.roles.write",
    "alerts.read",
    "alerts.write",
    "s3.connections.read",
    "s3.connections.write",
    "s3.browse",
]

# ── Permission cache ────────────────────────────────────────────────────────

_perm_cache: dict[str, set[str]] = {}
_perm_cache_ts: float = 0.0
_CACHE_TTL = 60.0  # seconds
_CACHE_ERROR_BACKOFF = 1.0  # seconds
_cache_lock = asyncio.Lock()


async def get_role_permissions(db: AsyncSession, role_name: str) -> set[str]:
    """Get permissions for a role, using in-memory cache with 60s TTL."""
    global _perm_cache_ts
    now = time.time()
    if now - _perm_cache_ts > _CACHE_TTL:
        async with _cache_lock:
            # Double-check after acquiring lock to avoid thundering herd
            if time.time() - _perm_cache_ts > _CACHE_TTL:
                try:
                    await _refresh_cache(db)
                except Exception:
                    _perm_cache_ts = time.time() - _CACHE_TTL + _CACHE_ERROR_BACKOFF
                    raise

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


async def invalidate_permission_cache() -> None:
    """Call after role/permission changes to force cache refresh."""
    global _perm_cache, _perm_cache_ts
    async with _cache_lock:
        _perm_cache = {}
        _perm_cache_ts = 0.0


# ── User ────────────────────────────────────────────────────────────────────

@dataclass
class CurrentUser:
    username: str
    role: str
    display_username: str | None = None


@dataclass
class ApiKeyUser:
    consumer_name: str
    allowed_databases: list[str]
    allowed_routes: list[str]


def create_token(username: str, role: str, expires_delta: timedelta | None = None) -> str:
    """Create a JWT token for the given user and role (dev/testing only)."""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=8))
    payload = {
        "sub": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# ── Keycloak JWKS cache ───────────────────────────────────────────────────

_jwks_cache: dict | None = None
_jwks_cache_ts: float = 0.0
_JWKS_CACHE_TTL = 300.0  # 5 minutes
_jwks_lock = asyncio.Lock()

# Priority order: first match wins (highest privilege first).
ROLE_PRIORITY = [
    role.strip()
    for role in settings.ROLE_PRIORITY.split(",")
    if role.strip()
]
_KEYCLOAK_BUILTIN_ROLES = {"offline_access", "uma_authorization"}


def is_application_role_name(role_name: str) -> bool:
    """Return whether a Keycloak realm role should be treated as an app role."""
    if not role_name:
        return False
    return (
        role_name not in _KEYCLOAK_BUILTIN_ROLES
        and not role_name.startswith("default-roles-")
    )


def resolve_application_role(role_names: list[str]) -> str | None:
    """Resolve the app role from Keycloak role names.

    Known roles keep their configured privilege priority. Unknown custom roles
    are accepted as app roles so adding a Keycloak/DB role does not require a
    backend code change.
    """
    role_set = set(role_names)
    priority_match = next((r for r in ROLE_PRIORITY if r in role_set), None)
    if priority_match:
        return priority_match
    return next((r for r in role_names if is_application_role_name(r)), None)


def _jwks_has_kid(jwks: dict | None, kid: str | None) -> bool:
    if not jwks or not kid:
        return False
    return any(key.get("kid") == kid for key in jwks.get("keys", []))


async def _get_jwks(
    *,
    force_refresh: bool = False,
    required_kid: str | None = None,
) -> dict:
    global _jwks_cache, _jwks_cache_ts
    now = time.time()
    if not force_refresh and _jwks_cache and (now - _jwks_cache_ts < _JWKS_CACHE_TTL):
        return _jwks_cache
    async with _jwks_lock:
        if force_refresh and _jwks_has_kid(_jwks_cache, required_kid):
            return _jwks_cache
        # Double-check after acquiring lock
        if not force_refresh and _jwks_cache and (time.time() - _jwks_cache_ts < _JWKS_CACHE_TTL):
            return _jwks_cache
        ssl_verify: str | bool = settings.SSL_CA_CERT_PATH or settings.SSL_VERIFY
        async with httpx.AsyncClient(timeout=10.0, verify=ssl_verify) as client:
            try:
                resp = await client.get(settings.KEYCLOAK_JWKS_URL)
                resp.raise_for_status()
            except Exception:
                if _jwks_cache:
                    logger.warning("Using cached JWKS after refresh failure", exc_info=True)
                    return _jwks_cache
                raise
            else:
                _jwks_cache = resp.json()
                _jwks_cache_ts = time.time()
                return _jwks_cache


async def _verify_keycloak_token(token: str) -> CurrentUser:
    """Verify a Keycloak-issued JWT using RS256 + JWKS."""
    try:
        jwks = await _get_jwks()
    except Exception as exc:
        logger.error("Failed to fetch JWKS from Keycloak: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Auth service unavailable")

    # Find signing key by kid
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    rsa_key = None
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            rsa_key = key
            break

    if rsa_key is None:
        # Key rotated? Force refresh once
        jwks = await _get_jwks(force_refresh=True, required_kid=kid)
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = key
                break

    if rsa_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token signing key not found")

    try:
        # Debug: log claims for troubleshooting (debug level to avoid leaking in prod)
        unverified = jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
        logger.debug("JWT iss=%s aud=%s", unverified.get("iss"), unverified.get("aud"))
        logger.debug("Expected iss=%s aud=%s", settings.KEYCLOAK_ISSUER_URL, settings.KEYCLOAK_JWT_AUDIENCE)

        public_key = RSAAlgorithm.from_jwk(rsa_key)
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=settings.KEYCLOAK_JWT_AUDIENCE,
            issuer=settings.KEYCLOAK_ISSUER_URL,
        )
    except JWTError as exc:
        logger.error("JWT verification failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc

    subject = payload.get("sub")
    display_username = payload.get("preferred_username") or subject
    logger.debug("JWT sub=%s preferred_username=%s roles claim=%s", subject, display_username, payload.get("roles"))

    # Extract role: check custom "roles" claim, then standard realm_access
    role = None
    role_claim = payload.get("roles")
    if isinstance(role_claim, list):
        role = resolve_application_role([str(r) for r in role_claim])
    elif isinstance(role_claim, str):
        role = resolve_application_role([role_claim])

    if not role:
        realm_roles = payload.get("realm_access", {}).get("roles", [])
        role = resolve_application_role([str(r) for r in realm_roles])

    logger.debug("Resolved role=%s for sub=%s", role, subject)

    if not subject or not role:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing username or valid role")

    return CurrentUser(username=subject, role=role, display_username=display_username)


# ── Unified user dependency ────────────────────────────────────────────────

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentUser:
    """FastAPI dependency: verify JWT and return the current user.

    Uses Keycloak RS256 verification when KEYCLOAK_ISSUER_URL is configured,
    falls back to HS256 shared-secret for dev/testing.
    """
    token = credentials.credentials

    if settings.KEYCLOAK_ISSUER_URL:
        return await _verify_keycloak_token(token)

    # Dev fallback: HS256 self-signed tokens
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
        return CurrentUser(username=username, role=role, display_username=username)
    except JWTError as exc:
        logger.error("Dev JWT verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc


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
    if consumer_name and credentials is None:
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
