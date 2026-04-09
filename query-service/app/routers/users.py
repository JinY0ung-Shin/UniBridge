"""User management endpoints (Keycloak Admin REST API proxy)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.auth import ROLE_PRIORITY, CurrentUser, require_permission
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

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# ── Helpers ──────────────────────────────────────────────────────────────────

_kc_admin: KeycloakAdminClient | None = None


def _get_kc_admin() -> KeycloakAdminClient:
    """Return a module-level singleton KeycloakAdminClient.

    Raises HTTP 503 if KEYCLOAK_URL is not configured.
    """
    global _kc_admin
    if _kc_admin is None:
        if not settings.KEYCLOAK_URL:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Keycloak is not configured (KEYCLOAK_URL is empty)",
            )
        _kc_admin = KeycloakAdminClient(
            base_url=settings.KEYCLOAK_URL,
            realm=settings.KEYCLOAK_REALM,
            client_id=settings.KEYCLOAK_SERVICE_CLIENT_ID,
            client_secret=settings.KEYCLOAK_SERVICE_CLIENT_SECRET,
        )
    return _kc_admin


def _resolve_role(realm_roles: list[dict]) -> str | None:
    """Pick the highest-priority application role from a list of realm role dicts.

    Uses ROLE_PRIORITY from auth.py (admin > developer > viewer).
    """
    role_names = {r["name"] for r in realm_roles}
    return next((r for r in ROLE_PRIORITY if r in role_names), None)


async def _enrich_user(kc: KeycloakAdminClient, user: dict) -> KeycloakUser:
    """Convert a raw Keycloak user dict to a KeycloakUser with resolved role."""
    user_roles = await kc.get_user_realm_roles(user["id"])
    role = _resolve_role(user_roles)
    return KeycloakUser(
        id=user["id"],
        username=user.get("username", ""),
        email=user.get("email"),
        enabled=user.get("enabled", True),
        role=role,
        createdTimestamp=user.get("createdTimestamp"),
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/admin/users", response_model=KeycloakUserList)
async def list_users(
    search: str | None = None,
    first: int = 0,
    max: int = 50,
    user: CurrentUser = Depends(require_permission("admin.roles.read")),
) -> KeycloakUserList:
    """List Keycloak users with role enrichment."""
    kc = _get_kc_admin()
    users, total = await kc.list_users(search=search, first=first, max_results=max)
    enriched = await asyncio.gather(*[_enrich_user(kc, u) for u in users])
    return KeycloakUserList(users=list(enriched), total=total)


@router.post("/admin/users", response_model=KeycloakUser, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> KeycloakUser:
    """Create a Keycloak user and assign a realm role."""
    kc = _get_kc_admin()
    user_id = await kc.create_user(
        username=body.username,
        email=body.email,
        password=body.password,
        enabled=True,
    )
    # Assign the requested role — rollback user creation on failure
    try:
        await kc.assign_realm_role(user_id, body.role)
    except Exception:
        logger.error("Failed to assign role '%s' to new user '%s', rolling back user creation", body.role, body.username)
        try:
            await kc.delete_user(user_id)
        except Exception:
            logger.error("Failed to rollback user creation for '%s' (id=%s)", body.username, user_id)
        raise

    return KeycloakUser(
        id=user_id,
        username=body.username,
        email=body.email,
        enabled=True,
        role=body.role,
    )


@router.put("/admin/users/{user_id}/role", response_model=KeycloakUser)
async def change_role(
    user_id: str = Path(..., pattern=_UUID_PATTERN),
    body: ChangeRoleRequest = ...,
    user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> KeycloakUser:
    """Change a user's application role (assign new first, then remove old)."""
    kc = _get_kc_admin()

    # Assign new role first (so user is never without a role)
    await kc.assign_realm_role(user_id, body.role)

    # Then remove old app roles (excluding the newly assigned one)
    current_roles = await kc.get_user_realm_roles(user_id)
    for role_dict in current_roles:
        if role_dict["name"] in ROLE_PRIORITY and role_dict["name"] != body.role:
            await kc.remove_realm_role(user_id, role_dict["name"])

    # Return updated user info via direct get_user lookup
    user_data = await kc.get_user(user_id)
    return await _enrich_user(kc, user_data)


@router.put(
    "/admin/users/{user_id}/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def reset_password(
    user_id: str = Path(..., pattern=_UUID_PATTERN),
    body: ResetPasswordRequest = ...,
    user: CurrentUser = Depends(require_permission("admin.roles.write")),
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
    user_id: str = Path(..., pattern=_UUID_PATTERN),
    user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> None:
    """Delete a Keycloak user. Prevents self-deletion."""
    kc = _get_kc_admin()

    # Prevent self-deletion: look up the user to check username
    target = await kc.get_user(user_id)
    if target["username"] == user.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    await kc.delete_user(user_id)
