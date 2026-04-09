from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ALL_PERMISSIONS,
    CurrentUser,
    get_current_user,
    get_role_permissions,
    invalidate_permission_cache,
    require_permission,
)
from app.database import get_db
from app.models import Role, RolePermission
from app.schemas import RoleCreate, RoleResponse, RoleUpdate, UserInfoResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Roles"])


async def _role_to_response(db: AsyncSession, role: Role) -> RoleResponse:
    result = await db.execute(
        select(RolePermission.permission).where(RolePermission.role_id == role.id)
    )
    permissions = [row[0] for row in result.all()]
    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description or "",
        is_system=role.is_system,
        permissions=permissions,
    )


# ── Public: role list for login ─────────────────────────────────────────────

@router.get("/auth/roles", response_model=list[str])
async def list_auth_roles(
    _user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """List role names. Requires authentication."""
    result = await db.execute(select(Role.name).order_by(Role.name))
    return [row[0] for row in result.all()]


# ── Current user info ───────────────────────────────────────────────────────

@router.get("/auth/me", response_model=UserInfoResponse)
async def get_me(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserInfoResponse:
    """Get current user info including permissions."""
    perms = await get_role_permissions(db, user.role)
    return UserInfoResponse(
        username=user.username,
        role=user.role,
        permissions=sorted(perms),
    )


# ── Permission list ─────────────────────────────────────────────────────────

@router.get("/admin/permissions", response_model=list[str])
async def list_all_permissions(
    _user: CurrentUser = Depends(require_permission("admin.roles.read")),
) -> list[str]:
    """List all available permissions."""
    return ALL_PERMISSIONS


# ── Role CRUD ───────────────────────────────────────────────────────────────

@router.get("/admin/roles", response_model=list[RoleResponse])
async def list_roles(
    _user: CurrentUser = Depends(require_permission("admin.roles.read")),
    db: AsyncSession = Depends(get_db),
) -> list[RoleResponse]:
    result = await db.execute(select(Role).order_by(Role.name))
    roles = result.scalars().all()
    return [await _role_to_response(db, r) for r in roles]


@router.get("/admin/roles/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: int,
    _user: CurrentUser = Depends(require_permission("admin.roles.read")),
    db: AsyncSession = Depends(get_db),
) -> RoleResponse:
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return await _role_to_response(db, role)


@router.post("/admin/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreate,
    _user: CurrentUser = Depends(require_permission("admin.roles.write")),
    db: AsyncSession = Depends(get_db),
) -> RoleResponse:
    # Check duplicate
    existing = await db.execute(select(Role).where(Role.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Role '{body.name}' already exists")

    # Validate permissions
    invalid = [p for p in body.permissions if p not in ALL_PERMISSIONS]
    if invalid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid permissions: {invalid}")

    role = Role(name=body.name, description=body.description, is_system=False)
    db.add(role)
    await db.flush()

    for perm in body.permissions:
        db.add(RolePermission(role_id=role.id, permission=perm))

    await db.commit()
    await db.refresh(role)
    invalidate_permission_cache()
    return await _role_to_response(db, role)


@router.put("/admin/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: int,
    body: RoleUpdate,
    _user: CurrentUser = Depends(require_permission("admin.roles.write")),
    db: AsyncSession = Depends(get_db),
) -> RoleResponse:
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    # Prevent users from modifying their own role's permissions (privilege escalation)
    if role.name == _user.role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify permissions of your own role",
        )

    if body.description is not None:
        role.description = body.description

    if body.permissions is not None:
        invalid = [p for p in body.permissions if p not in ALL_PERMISSIONS]
        if invalid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid permissions: {invalid}")

        # Replace all permissions
        await db.execute(
            delete(RolePermission).where(RolePermission.role_id == role.id)
        )
        for perm in body.permissions:
            db.add(RolePermission(role_id=role.id, permission=perm))

    await db.commit()
    await db.refresh(role)
    invalidate_permission_cache()
    return await _role_to_response(db, role)


@router.delete("/admin/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_role(
    role_id: int,
    _user: CurrentUser = Depends(require_permission("admin.roles.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.is_system:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete system role")
    if role.name == _user.role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete your own role",
        )

    await db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    await db.delete(role)
    await db.commit()
    invalidate_permission_cache()
