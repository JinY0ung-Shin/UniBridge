from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, get_role_permissions, require_permission
from app.database import get_db
from app.models import Permission
from app.schemas import DBConnectionResponse, HealthResponse, QueryRequest, QueryResponse
from app.services.audit import log_query
from app.services.connection_manager import connection_manager
from app.services.query_executor import check_permission, detect_statement_type, execute_query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Query"])


@router.post("/query/execute", response_model=QueryResponse)
async def execute(
    req: QueryRequest,
    user: CurrentUser = Depends(require_permission("query.execute")),
    db: AsyncSession = Depends(get_db),
) -> QueryResponse:
    """Execute an SQL query against a registered database."""
    # 1. Verify the database alias exists in the connection manager
    try:
        engine = connection_manager.get_engine(req.database)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database '{req.database}' is not registered or not connected",
        )

    db_type = connection_manager.get_db_type(req.database)

    # 2. Check per-database permissions (users with query.databases.write bypass)
    statement_type = detect_statement_type(req.sql)
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

    # 3. Execute the query
    try:
        response = await execute_query(
            engine=engine,
            sql=req.sql,
            params=req.params,
            limit=req.limit,
            timeout=req.timeout,
            db_type=db_type,
        )
    except asyncio.TimeoutError:
        try:
            await log_query(
                db,
                user=user.username,
                database_alias=req.database,
                sql=req.sql,
                params=req.params,
                status="error",
                error_message="Query timed out",
            )
        except Exception:
            logger.exception("Failed to write audit log for timed-out query")
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail="Query timed out",
        )
    except Exception as exc:
        try:
            await log_query(
                db,
                user=user.username,
                database_alias=req.database,
                sql=req.sql,
                params=req.params,
                status="error",
                error_message=str(exc),
            )
        except Exception:
            logger.exception("Failed to write audit log for failed query")
        logger.exception("Query execution failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query execution failed. Check server logs for details.",
        )

    # 4. Audit log (success)
    await log_query(
        db,
        user=user.username,
        database_alias=req.database,
        sql=req.sql,
        params=req.params,
        row_count=response.row_count,
        elapsed_ms=response.elapsed_ms,
        status="success",
    )

    return response


@router.get("/query/databases", response_model=list[DBConnectionResponse])
async def list_databases(
    user: CurrentUser = Depends(require_permission("query.databases.read")),
    db: AsyncSession = Depends(get_db),
) -> list[DBConnectionResponse]:
    """List available databases filtered by the current user's permissions."""
    from app.models import DBConnection as DBConn

    user_perms = await get_role_permissions(db, user.role)
    if "query.databases.write" in user_perms:
        # Users with write permission see all databases
        result = await db.execute(select(DBConn))
        connections = result.scalars().all()
    else:
        # Non-admin: only databases they have permissions on
        result = await db.execute(
            select(Permission.db_alias).where(Permission.role == user.role)
        )
        allowed_aliases = [row[0] for row in result.all()]
        if not allowed_aliases:
            return []
        result = await db.execute(
            select(DBConn).where(DBConn.alias.in_(allowed_aliases))
        )
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
                pool_size=conn.pool_size if conn.pool_size is not None else 5,
                max_overflow=conn.max_overflow if conn.max_overflow is not None else 3,
                query_timeout=conn.query_timeout if conn.query_timeout is not None else 30,
                status=pool_status.get("status", "unknown"),
            )
        )

    return responses


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Service-level health check."""
    return HealthResponse(status="ok")


@router.get("/health/databases", response_model=HealthResponse)
async def health_databases(
    _user: CurrentUser = Depends(require_permission("query.databases.read")),
) -> HealthResponse:
    """Per-database health check."""
    aliases = connection_manager.list_aliases()
    db_statuses: dict = {}
    for alias in aliases:
        try:
            ok = await connection_manager.test_connection(alias)
            db_statuses[alias] = {"status": "ok" if ok else "error"}
        except Exception as exc:
            db_statuses[alias] = {"status": "error", "detail": str(exc)}

    overall = "ok" if all(d["status"] == "ok" for d in db_statuses.values()) else "degraded"
    if not db_statuses:
        overall = "ok"

    return HealthResponse(status=overall, databases=db_statuses)
