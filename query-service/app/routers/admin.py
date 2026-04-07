from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_admin
from app.database import get_db
from app.models import AuditLog, DBConnection, Permission
from app.schemas import (
    AuditLogResponse,
    DBConnectionCreate,
    DBConnectionResponse,
    DBConnectionUpdate,
    PermissionCreate,
    PermissionResponse,
)
from app.services.connection_manager import connection_manager, encrypt_password

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin"])


# ── DB Connection CRUD ───────────────────────────────────────────────────────


@router.post(
    "/admin/query/databases",
    response_model=DBConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    body: DBConnectionCreate,
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DBConnectionResponse:
    """Register a new database connection."""
    # Check for duplicate alias
    existing = await db.execute(
        select(DBConnection).where(DBConnection.alias == body.alias)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Database alias '{body.alias}' already exists",
        )

    conn = DBConnection(
        alias=body.alias,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        database=body.database,
        username=body.username,
        password_encrypted=encrypt_password(body.password),
        pool_size=body.pool_size or 5,
        max_overflow=body.max_overflow or 3,
        query_timeout=body.query_timeout or 30,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)

    # Create engine in connection manager
    try:
        await connection_manager.add_connection(conn)
        conn_status = "registered"
    except Exception as exc:
        logger.warning("Engine creation failed for '%s': %s", body.alias, exc)
        conn_status = "error"

    return DBConnectionResponse(
        alias=conn.alias,
        db_type=conn.db_type,
        host=conn.host,
        port=conn.port,
        database=conn.database,
        username=conn.username,
        pool_size=conn.pool_size,
        max_overflow=conn.max_overflow,
        query_timeout=conn.query_timeout,
        status=conn_status,
    )


@router.get("/admin/query/databases", response_model=list[DBConnectionResponse])
async def list_connections(
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[DBConnectionResponse]:
    """List all registered database connections."""
    result = await db.execute(select(DBConnection))
    connections = result.scalars().all()

    responses = []
    for conn in connections:
        pool_status = connection_manager.get_status(conn.alias)
        responses.append(
            DBConnectionResponse(
                alias=conn.alias,
                db_type=conn.db_type,
                host=conn.host,
                port=conn.port,
                database=conn.database,
                username=conn.username,
                pool_size=conn.pool_size or 5,
                max_overflow=conn.max_overflow or 3,
                query_timeout=conn.query_timeout or 30,
                status=pool_status.get("status", "unknown"),
            )
        )
    return responses


@router.get("/admin/query/databases/{alias}", response_model=DBConnectionResponse)
async def get_connection(
    alias: str,
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DBConnectionResponse:
    """Get details of a single database connection."""
    result = await db.execute(
        select(DBConnection).where(DBConnection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database alias '{alias}' not found",
        )

    pool_status = connection_manager.get_status(alias)
    return DBConnectionResponse(
        alias=conn.alias,
        db_type=conn.db_type,
        host=conn.host,
        port=conn.port,
        database=conn.database,
        username=conn.username,
        pool_size=conn.pool_size or 5,
        max_overflow=conn.max_overflow or 3,
        query_timeout=conn.query_timeout or 30,
        status=pool_status.get("status", "unknown"),
    )


@router.put("/admin/query/databases/{alias}", response_model=DBConnectionResponse)
async def update_connection(
    alias: str,
    body: DBConnectionUpdate,
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DBConnectionResponse:
    """Update an existing database connection."""
    result = await db.execute(
        select(DBConnection).where(DBConnection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database alias '{alias}' not found",
        )

    # Apply updates
    update_data = body.model_dump(exclude_unset=True)
    if "password" in update_data:
        password = update_data.pop("password")
        if password is not None:
            conn.password_encrypted = encrypt_password(password)

    for key, value in update_data.items():
        if value is not None:
            setattr(conn, key, value)

    await db.commit()
    await db.refresh(conn)

    # Recreate engine with updated settings
    try:
        await connection_manager.add_connection(conn)
        conn_status = "registered"
    except Exception as exc:
        logger.warning("Engine recreation failed for '%s': %s", alias, exc)
        conn_status = "error"

    return DBConnectionResponse(
        alias=conn.alias,
        db_type=conn.db_type,
        host=conn.host,
        port=conn.port,
        database=conn.database,
        username=conn.username,
        pool_size=conn.pool_size or 5,
        max_overflow=conn.max_overflow or 3,
        query_timeout=conn.query_timeout or 30,
        status=conn_status,
    )


@router.delete(
    "/admin/query/databases/{alias}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_connection(
    alias: str,
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a database connection."""
    result = await db.execute(
        select(DBConnection).where(DBConnection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database alias '{alias}' not found",
        )

    # Remove engine
    await connection_manager.remove_connection(alias)

    # Delete from meta-DB
    await db.delete(conn)
    await db.commit()


@router.post("/admin/query/databases/{alias}/test")
async def test_connection(
    alias: str,
    _admin: CurrentUser = Depends(require_admin),
) -> dict:
    """Test connectivity to a registered database."""
    try:
        connection_manager.get_engine(alias)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database alias '{alias}' is not registered",
        )

    ok = await connection_manager.test_connection(alias)
    if ok:
        return {"alias": alias, "status": "ok", "message": "Connection successful"}
    else:
        return {"alias": alias, "status": "error", "message": "Connection failed"}


# ── Permissions ──────────────────────────────────────────────────────────────


@router.get("/admin/query/permissions", response_model=list[PermissionResponse])
async def list_permissions(
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[PermissionResponse]:
    """List all permission entries."""
    result = await db.execute(select(Permission))
    return [PermissionResponse.model_validate(p) for p in result.scalars().all()]


@router.put("/admin/query/permissions", response_model=PermissionResponse)
async def upsert_permission(
    body: PermissionCreate,
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PermissionResponse:
    """Create or update a permission entry (upsert by role + db_alias)."""
    result = await db.execute(
        select(Permission).where(
            Permission.role == body.role,
            Permission.db_alias == body.db_alias,
        )
    )
    perm = result.scalar_one_or_none()

    if perm is None:
        perm = Permission(
            role=body.role,
            db_alias=body.db_alias,
            allow_select=body.allow_select,
            allow_insert=body.allow_insert,
            allow_update=body.allow_update,
            allow_delete=body.allow_delete,
        )
        db.add(perm)
    else:
        perm.allow_select = body.allow_select
        perm.allow_insert = body.allow_insert
        perm.allow_update = body.allow_update
        perm.allow_delete = body.allow_delete

    await db.commit()
    await db.refresh(perm)
    return PermissionResponse.model_validate(perm)


@router.delete(
    "/admin/query/permissions/{permission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_permission(
    permission_id: int,
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a permission entry by ID."""
    result = await db.execute(
        select(Permission).where(Permission.id == permission_id)
    )
    perm = result.scalar_one_or_none()
    if perm is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Permission ID {permission_id} not found",
        )
    await db.delete(perm)
    await db.commit()


# ── Audit Logs ───────────────────────────────────────────────────────────────


@router.get("/admin/query/audit-logs", response_model=list[AuditLogResponse])
async def list_audit_logs(
    database: str | None = Query(None, description="Filter by database alias"),
    user: str | None = Query(None, description="Filter by username"),
    from_date: str | None = Query(None, description="Filter from date (ISO format)"),
    to_date: str | None = Query(None, description="Filter to date (ISO format)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _admin: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AuditLogResponse]:
    """Query audit logs with optional filters."""
    from datetime import datetime

    stmt = select(AuditLog)

    if database:
        stmt = stmt.where(AuditLog.database_alias == database)
    if user:
        stmt = stmt.where(AuditLog.user == user)
    if from_date:
        try:
            dt = datetime.fromisoformat(from_date)
            stmt = stmt.where(AuditLog.timestamp >= dt)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid from_date format. Use ISO format.",
            )
    if to_date:
        try:
            dt = datetime.fromisoformat(to_date)
            stmt = stmt.where(AuditLog.timestamp <= dt)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid to_date format. Use ISO format.",
            )

    stmt = stmt.order_by(AuditLog.id.desc()).offset(offset).limit(limit)

    result = await db.execute(stmt)
    return [AuditLogResponse.model_validate(log) for log in result.scalars().all()]
