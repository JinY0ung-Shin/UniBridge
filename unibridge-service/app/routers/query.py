from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ApiKeyUser, CurrentUser, get_current_user_or_apikey, get_role_permissions, require_permission
from app.database import get_db
from app.models import Permission
from app.schemas import DBConnectionResponse, HealthResponse, QueryRequest, QueryResponse
from app.middleware.rate_limiter import rate_limiter
from app.services.audit import log_query
from app.services.connection_manager import connection_manager
from app.services.query_executor import check_permission, detect_statement_type, execute_clickhouse_query, execute_query
from app.services.settings_manager import settings_manager
from app.services.sql_validator import validate_sql
from app.services.table_access import check_table_access, extract_tables

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Query"])


@router.post("/query/execute", response_model=QueryResponse)
async def execute(
    req: QueryRequest,
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> QueryResponse:
    """Execute an SQL query against a registered database."""

    username = f"apikey:{user.consumer_name}" if isinstance(user, ApiKeyUser) else user.username

    # JWT users are not pre-counted by RateLimitMiddleware because it must not
    # trust unverified Bearer claims. API key users are still pre-counted from
    # the APISIX consumer header to preserve existing route-edge behavior.
    if isinstance(user, CurrentUser):
        allowed, msg, _stamp = rate_limiter.check_rate_limit(username)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=msg,
                headers={"Retry-After": "60"},
            )

    # API Key user: check allowed databases
    if isinstance(user, ApiKeyUser):
        if req.database not in user.allowed_databases:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key '{user.consumer_name}' is not allowed to access database '{req.database}'",
            )
    else:
        # JWT user: check role-based permission
        user_perms = await get_role_permissions(db, user.role)
        if "query.execute" not in user_perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Required permission: query.execute",
            )
        username = user.username

    # 1. Verify the database alias exists
    db_type = connection_manager.get_db_type(req.database)
    if db_type == "unknown":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database '{req.database}' is not registered or not connected",
        )

    # 2. Check per-database permissions
    perm = None
    if isinstance(user, ApiKeyUser):
        # API key users: only SELECT allowed
        statement_type = detect_statement_type(req.sql)
        if statement_type != "select":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key users can only execute SELECT queries",
            )
    else:
        # JWT user: role-based per-DB permissions
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

    # 2b. SQL keyword blacklist check
    blocked_error = validate_sql(req.sql, extra_blocked=settings_manager.blocked_sql_keywords)
    if blocked_error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=blocked_error,
        )

    # 2c. Table-level access check (JWT non-admin users only)
    if isinstance(user, CurrentUser):
        user_perms_for_tables = await get_role_permissions(db, user.role)
        if "query.databases.write" not in user_perms_for_tables and perm is not None:
            allowed_tables_raw = perm.allowed_tables
            allowed_tables = json.loads(allowed_tables_raw) if allowed_tables_raw else None
            if allowed_tables is not None:
                referenced = extract_tables(req.sql, db_type=db_type)
                table_error = check_table_access(referenced, allowed_tables)
                if table_error:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=table_error,
                    )

    # 3. Acquire concurrent query slot (post-auth to prevent forged-token DoS)
    if not rate_limiter.try_acquire(username):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many concurrent queries (max {rate_limiter._max_concurrent})",
        )

    # 4. Execute the query
    try:
        if db_type == "clickhouse":
            ch_client = connection_manager.get_clickhouse_client(req.database)
            response = await execute_clickhouse_query(
                client=ch_client, sql=req.sql, params=req.params,
                limit=req.limit, timeout=req.timeout,
            )
        else:
            engine = connection_manager.get_engine(req.database)
            response = await execute_query(
                engine=engine, sql=req.sql, params=req.params,
                limit=req.limit, timeout=req.timeout, db_type=db_type,
            )
    except asyncio.TimeoutError:
        try:
            await log_query(db, user=username, database_alias=req.database,
                            sql=req.sql, params=req.params, status="error",
                            error_message="Query timed out")
        except Exception:
            logger.exception("Failed to write audit log for timed-out query")
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail="Query timed out",
        )
    except Exception as exc:
        try:
            await log_query(db, user=username, database_alias=req.database,
                            sql=req.sql, params=req.params, status="error",
                            error_message=str(exc))
        except Exception:
            logger.exception("Failed to write audit log for failed query")
        logger.exception("Query execution failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query execution failed. Check server logs for details.",
        )
    finally:
        rate_limiter.release(username)

    # 5. Audit log (success)
    await log_query(db, user=username, database_alias=req.database,
                    sql=req.sql, params=req.params, row_count=response.row_count,
                    elapsed_ms=response.elapsed_ms, status="success")

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
                protocol=conn.protocol,
                secure=conn.secure,
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
            ok, _msg = await connection_manager.test_connection(alias)
            db_statuses[alias] = {"status": "ok" if ok else "error"}
        except Exception as exc:
            logger.warning("Health check failed for '%s': %s", alias, exc)
            db_statuses[alias] = {"status": "error", "detail": "Connection failed"}

    overall = "ok" if all(d["status"] == "ok" for d in db_statuses.values()) else "degraded"
    if not db_statuses:
        overall = "ok"

    return HealthResponse(status=overall, databases=db_statuses)
