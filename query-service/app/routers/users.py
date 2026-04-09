"""User management endpoints (Keycloak Admin REST API proxy)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_kc_admin() -> KeycloakAdminClient:
    """Create a KeycloakAdminClient from application settings.

    Raises HTTP 503 if KEYCLOAK_URL is not configured.
    """
    if not settings.KEYCLOAK_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Keycloak is not configured (KEYCLOAK_URL is empty)",
        )
    return KeycloakAdminClient(
        base_url=settings.KEYCLOAK_URL,
        realm=settings.KEYCLOAK_REALM,
        client_id=settings.KEYCLOAK_SERVICE_CLIENT_ID,
        client_secret=settings.KEYCLOAK_SERVICE_CLIENT_SECRET,
    )


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
    enriched = [await _enrich_user(kc, u) for u in users]
    return KeycloakUserList(users=enriched, total=total)


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
    # Assign the requested role
    await kc.assign_realm_role(user_id, body.role)

    return KeycloakUser(
        id=user_id,
        username=body.username,
        email=body.email,
        enabled=True,
        role=body.role,
    )


@router.put("/admin/users/{user_id}/role", response_model=KeycloakUser)
async def change_role(
    user_id: str,
    body: ChangeRoleRequest,
    user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> KeycloakUser:
    """Change a user's application role (remove old app roles, assign new)."""
    kc = _get_kc_admin()

    # Remove existing application roles
    current_roles = await kc.get_user_realm_roles(user_id)
    for role_dict in current_roles:
        if role_dict["name"] in ROLE_PRIORITY:
            await kc.remove_realm_role(user_id, role_dict["name"])

    # Assign the new role
    await kc.assign_realm_role(user_id, body.role)

    # Return updated user info
    # Fetch user details to build the response
    users, _ = await kc.list_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return KeycloakUser(
        id=user_id,
        username=target.get("username", ""),
        email=target.get("email"),
        enabled=target.get("enabled", True),
        role=body.role,
    )


@router.put(
    "/admin/users/{user_id}/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def reset_password(
    user_id: str,
    body: ResetPasswordRequest,
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
    user_id: str,
    user: CurrentUser = Depends(require_permission("admin.roles.write")),
) -> None:
    """Delete a Keycloak user. Prevents self-deletion."""
    kc = _get_kc_admin()

    # Prevent self-deletion: look up the user to check username
    users, _ = await kc.list_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if target and target.get("username") == user.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    await kc.delete_user(user_id)
