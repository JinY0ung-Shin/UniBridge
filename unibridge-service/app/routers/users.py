"""User management endpoints (Keycloak Admin REST API proxy)."""
from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.auth import CurrentUser, is_application_role_name, require_permission, resolve_application_role
from app.config import settings
from app.keycloak_admin import KeycloakAdminClient
from app.schemas import (
    ChangeRoleRequest,
    CreateUserRequest,
    KeycloakUser,
    KeycloakUserList,
    ResetPasswordRequest,
    ToggleEnabledRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Users"])

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# ── Helpers ──────────────────────────────────────────────────────────────────

_kc_admin: KeycloakAdminClient | None = None
_kc_admin_lock = threading.Lock()


def _get_kc_admin() -> KeycloakAdminClient:
    """Return a module-level singleton KeycloakAdminClient.

    Raises HTTP 503 if KEYCLOAK_URL is not configured.
    """
    global _kc_admin
    if _kc_admin is None:
        with _kc_admin_lock:
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
    """Pick the application role from a list of realm role dicts."""
    return resolve_application_role([r["name"] for r in realm_roles])


def _is_same_user(target: dict, user: CurrentUser) -> bool:
    """Compare Keycloak user data with the authenticated principal."""
    if target.get("id") == user.username:
        return True
    if user.display_username and target.get("username") == user.display_username:
        return True
    return target.get("username") == user.username


async def _user_has_role(kc: KeycloakAdminClient, user_id: str, role_name: str) -> bool:
    roles = await kc.get_user_realm_roles(user_id)
    return any(role.get("name") == role_name for role in roles)


async def _count_users_with_role(
    kc: KeycloakAdminClient,
    role_name: str,
    *,
    enabled_only: bool = True,
) -> int:
    count = 0
    first = 0
    page_size = 100
    while True:
        users, total = await kc.list_users(first=first, max_results=page_size)
        if not users:
            break
        users_to_check = [
            user for user in users if not enabled_only or user.get("enabled", True)
        ]
        role_lists = await asyncio.gather(
            *(kc.get_user_realm_roles(user["id"]) for user in users_to_check)
        )
        count += sum(
            any(role.get("name") == role_name for role in roles)
            for roles in role_lists
        )
        first += len(users)
        if first >= total:
            break
    return count


async def _ensure_not_last_admin(kc: KeycloakAdminClient, user_id: str, action: str) -> None:
    if not await _user_has_role(kc, user_id, "admin"):
        return
    admin_count = await _count_users_with_role(kc, "admin")
    if admin_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot {action} the last admin user",
        )


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

    if body.role != "admin":
        await _ensure_not_last_admin(kc, user_id, "demote")

    # Assign new role first (so user is never without a role)
    await kc.assign_realm_role(user_id, body.role)

    # Then remove old app roles (excluding the newly assigned one)
    current_roles = await kc.get_user_realm_roles(user_id)
    for role_dict in current_roles:
        if is_application_role_name(role_dict["name"]) and role_dict["name"] != body.role:
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


@router.put("/admin/users/{user_id}/enabled", response_model=KeycloakUser)
async def toggle_enabled(
    user_id: str = Path(..., pattern=_UUID_PATTERN),
    body: ToggleEnabledRequest = ...,
    user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> KeycloakUser:
    """Enable or disable a user. Prevents self-deactivation."""
    kc = _get_kc_admin()

    target = await kc.get_user(user_id)
    if _is_same_user(target, user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change your own enabled status",
        )
    if body.enabled is False:
        await _ensure_not_last_admin(kc, user_id, "disable")

    await kc.update_user_enabled(user_id, body.enabled)
    # Refresh and return enriched user
    updated = await kc.get_user(user_id)
    return await _enrich_user(kc, updated)


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
    if _is_same_user(target, user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    await _ensure_not_last_admin(kc, user_id, "delete")

    await kc.delete_user(user_id)
