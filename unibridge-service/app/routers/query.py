from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.auth import ApiKeyUser, CurrentUser, get_current_user_or_apikey, get_role_permissions, require_permission
from app.database import get_db
from app.db_types import utcnow
from app.models import DBConnection, Permission, QueryTemplate
from app.schemas import (
    DBConnectionResponse,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    QueryTemplateAgentUpdate,
    QueryTemplateExecuteRequest,
    QueryTemplateResponse,
    normalize_query_template_path,
)
from app.middleware.rate_limiter import rate_limiter
from app.services.apisix_system_resources import (
    QUERY_API_ROUTE_ID,
    QUERY_TEMPLATE_WRITE_ROUTE_ID,
)
from app.services.audit import log_admin_action, log_query
from app.services.connection_manager import connection_manager
from app.services.query_executor import (
    check_permission,
    detect_statement_type,
    execute_clickhouse_query,
    execute_graphdb_query,
    execute_neo4j_query,
    execute_query,
)
from app.services.settings_manager import settings_manager
from app.services.sparql_analysis import (
    detect_sparql_statement_type,
    strip_sparql_strings_and_comments,
)
from app.services.sql_validator import validate_sql
from app.services.table_access import check_table_access, extract_tables

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Query"])

_TEMPLATE_AGENT_GUIDE_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "query-template-agent-guide.md"
)


def _api_key_has_route(user: ApiKeyUser, route_id: str) -> bool:
    return "*" in user.allowed_routes or route_id in user.allowed_routes


async def _require_template_read_access(
    db: AsyncSession, user: CurrentUser | ApiKeyUser
) -> None:
    if isinstance(user, ApiKeyUser):
        if not _api_key_has_route(user, QUERY_API_ROUTE_ID):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required API key route: {QUERY_API_ROUTE_ID}",
            )
        return

    user_perms = await get_role_permissions(db, user.role)
    if "query.execute" not in user_perms:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Required permission: query.execute",
        )


def _strip_neo4j_literals_and_comments(sql: str) -> str:
    result: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]
        if char == "/" and i + 1 < length and sql[i + 1] == "/":
            i = sql.find("\n", i)
            if i == -1:
                break
            result.append(" ")
            continue
        if char == "/" and i + 1 < length and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                break
            result.append(" ")
            i = end + 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            i += 1
            while i < length:
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == quote:
                    i += 1
                    break
                i += 1
            result.append("''")
            continue
        result.append(char)
        i += 1
    return "".join(result)


def _contains_neo4j_clause(sql: str, pattern: str) -> bool:
    return re.search(rf"(?<!\S){pattern}\b", sql) is not None


def _detect_neo4j_statement_type(sql: str) -> str:
    normalized = re.sub(
        r"\s+",
        " ",
        _strip_neo4j_literals_and_comments(sql).strip(),
    ).upper()
    if _contains_neo4j_clause(
        normalized,
        r"DETACH\s+DELETE",
    ) or _contains_neo4j_clause(normalized, "DELETE"):
        return "delete"
    if _contains_neo4j_clause(normalized, "SET") or _contains_neo4j_clause(
        normalized,
        "REMOVE",
    ):
        return "update"
    if _contains_neo4j_clause(normalized, "CREATE") or _contains_neo4j_clause(
        normalized,
        "MERGE",
    ):
        return "insert"
    if (
        _contains_neo4j_clause(normalized, r"LOAD\s+CSV")
        or _contains_neo4j_clause(normalized, "CALL")
        or _contains_neo4j_clause(normalized, "DROP")
    ):
        return "execute"
    if re.match(
        r"^(OPTIONAL\s+MATCH\b.*\bRETURN\b|MATCH\b.*\bRETURN\b|RETURN\b|WITH\b.*\bRETURN\b|UNWIND\b.*\bRETURN\b)",
        normalized,
    ):
        return "select"
    return "unknown"


def _detect_statement_type(sql: str, db_type: str) -> str:
    if db_type == "neo4j":
        return _detect_neo4j_statement_type(sql)
    if db_type == "graphdb":
        raw = detect_sparql_statement_type(sql)
        if raw == "reject":
            raise HTTPException(
                status_code=422,
                detail="Unsupported SPARQL statement",
            )
        # All read SPARQL forms (select/ask/construct/describe) normalize to
        # "select" so downstream gates (API-key SELECT check, check_permission,
        # _validate_read_only_template_sql) treat them uniformly. The raw form
        # is re-derived locally in the graphdb executor dispatch.
        return "select"
    return detect_statement_type(sql)


def _extra_blocked_keyword_error(sql: str, blocked_keywords: list[str]) -> str | None:
    if not blocked_keywords:
        return None
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(keyword) for keyword in blocked_keywords) + r")\b",
        re.IGNORECASE,
    )
    match = pattern.search(sql)
    if match:
        return f"Blocked SQL keyword: {match.group(0).upper()}"
    return None


async def _record_failed_query(
    db: AsyncSession,
    *,
    username: str,
    database_alias: str,
    db_type: str,
    sql: str,
    params: dict | None,
    metric_status: str,
    audit_status: str,
    error_message: str,
    duration_seconds: float = 0,
) -> None:
    metrics.record_query(
        db_alias=database_alias,
        db_type=db_type,
        status=metric_status,
        duration_seconds=duration_seconds,
    )
    try:
        await log_query(
            db,
            user=username,
            database_alias=database_alias,
            sql=sql,
            params=params,
            status=audit_status,
            error_message=error_message,
        )
    except Exception:
        logger.exception("Failed to write audit log for failed query")


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

    # APISIX enforces route access at the edge; repeat it here so direct
    # internal calls cannot bypass the independently managed query grant.
    if isinstance(user, ApiKeyUser):
        if not _api_key_has_route(user, QUERY_API_ROUTE_ID):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required API key route: {QUERY_API_ROUTE_ID}",
            )
        if "*" not in user.allowed_databases and req.database not in user.allowed_databases:
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
    if db_type == "graphdb" and req.params:
        raise HTTPException(
            status_code=422,
            detail=(
                "GraphDB does not support bind parameters; pass the SPARQL inline"
            ),
        )
    try:
        statement_type = _detect_statement_type(req.sql, db_type)
    except HTTPException as exc:
        await _record_failed_query(
            db,
            username=username,
            database_alias=req.database,
            db_type=db_type,
            sql=req.sql,
            params=req.params,
            metric_status="error",
            audit_status="error",
            error_message=str(exc.detail),
        )
        raise
    if db_type == "neo4j" and statement_type != "select":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Neo4j queries are read-only",
        )

    # 2. Check per-database permissions
    perm = None
    if isinstance(user, ApiKeyUser):
        # API key users: SELECT always allowed; INSERT/UPDATE/DELETE only when
        # the per-key flag grants it; everything else (DDL, EXECUTE, ...) is
        # always forbidden — unlike roles, keys can never run DDL.
        apikey_allowed = statement_type == "select" or (
            (statement_type == "insert" and user.allow_insert)
            or (statement_type == "update" and user.allow_update)
            or (statement_type == "delete" and user.allow_delete)
        )
        if not apikey_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"API key '{user.consumer_name}' is not allowed to execute "
                    f"{statement_type.upper()} queries"
                ),
            )
    else:
        # JWT user: role-based per-DB permissions
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

    # 2b. SQL keyword blacklist check. For SPARQL, keep the operator-defined
    # deny-list but skip SQL parser/default keyword rules that are SQL-specific.
    if db_type == "graphdb":
        blocked_error = _extra_blocked_keyword_error(
            strip_sparql_strings_and_comments(req.sql),
            settings_manager.blocked_sql_keywords,
        )
        if blocked_error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=blocked_error,
            )
    else:
        blocked_error = validate_sql(req.sql, extra_blocked=settings_manager.blocked_sql_keywords)
        if blocked_error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=blocked_error,
            )

    # 2c. Table-level access check (API keys and JWT non-admin users), applied
    # to every statement type — extract_tables covers FROM/JOIN/INTO/UPDATE.
    allowed_tables: list[str] | None = None
    if isinstance(user, ApiKeyUser):
        allowed_tables = user.allowed_tables  # None = no table restriction
    else:
        user_perms_for_tables = await get_role_permissions(db, user.role)
        if "query.databases.write" not in user_perms_for_tables and perm is not None:
            allowed_tables_raw = perm.allowed_tables
            allowed_tables = json.loads(allowed_tables_raw) if allowed_tables_raw else None
    if allowed_tables is not None and db_type not in ("neo4j", "graphdb"):
        referenced = extract_tables(req.sql, db_type=db_type)
        table_error = check_table_access(referenced, allowed_tables)
        if table_error:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=table_error,
            )

    # Release the meta-store connection before the (potentially long) user query.
    # Auth + the permission/table reads above run on the request-scoped `db`
    # session, which SQLAlchemy autobegins a transaction for — pinning a
    # meta-pool connection. Without this rollback that connection stays checked
    # out idle-in-transaction for the whole query duration (up to the 300s
    # timeout cap). Under concurrency that exhausts the meta QueuePool
    # ("QueuePool limit of size N overflow M reached, connection timed out").
    # The audit writes below use `db.bind` (a fresh short-lived session), so
    # `db`'s own connection is not needed again; it re-acquires lazily if used.
    await db.rollback()

    # 3. Acquire concurrent query slot (post-auth to prevent forged-token DoS)
    if not rate_limiter.try_acquire(username):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many concurrent queries (max {rate_limiter._max_concurrent})",
        )

    # 4. Execute the query
    query_started_at = time.monotonic()
    try:
        if db_type == "clickhouse":
            ch_client = connection_manager.get_clickhouse_client(req.database)
            ch_lock = connection_manager.get_clickhouse_lock(req.database)
            response = await execute_clickhouse_query(
                client=ch_client, sql=req.sql, params=req.params,
                limit=req.limit, timeout=req.timeout, lock=ch_lock,
            )
        elif db_type == "neo4j":
            neo4j_driver = connection_manager.get_neo4j_driver(req.database)
            response = await execute_neo4j_query(
                driver=neo4j_driver,
                database=connection_manager.get_database_name(req.database),
                query=req.sql,
                params=req.params,
                limit=req.limit,
                timeout=req.timeout,
            )
        elif db_type == "graphdb":
            graphdb_client = connection_manager.get_graphdb_client(req.database)
            repo = connection_manager.get_database_name(req.database)
            # Re-derive the raw SPARQL form (select/ask/construct/describe) for
            # the executor; statement_type was normalized to "select" by
            # _detect_statement_type so the upstream gates pass uniformly.
            raw_form = detect_sparql_statement_type(req.sql)
            effective_limit = req.limit or settings_manager.default_row_limit
            response = await execute_graphdb_query(
                client=graphdb_client,
                repo=repo,
                sparql=req.sql,
                statement_type=raw_form,
                limit=effective_limit,
                timeout=req.timeout,
            )
        else:
            engine = connection_manager.get_engine(req.database)
            response = await execute_query(
                engine=engine, sql=req.sql, params=req.params,
                limit=req.limit, timeout=req.timeout, db_type=db_type,
            )
    except asyncio.TimeoutError:
        metrics.record_query(
            db_alias=req.database,
            db_type=db_type,
            status="timeout",
            duration_seconds=time.monotonic() - query_started_at,
        )
        try:
            await log_query(db, user=username, database_alias=req.database,
                            sql=req.sql, params=req.params, status="timeout",
                            error_message="Query timed out")
        except Exception:
            logger.exception("Failed to write audit log for timed-out query")
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail="Query timed out",
        )
    except HTTPException as exc:
        status_label = (
            "timeout"
            if exc.status_code == status.HTTP_504_GATEWAY_TIMEOUT
            else "error"
        )
        await _record_failed_query(
            db,
            username=username,
            database_alias=req.database,
            db_type=db_type,
            sql=req.sql,
            params=req.params,
            metric_status=status_label,
            audit_status=status_label,
            error_message=str(exc.detail),
            duration_seconds=time.monotonic() - query_started_at,
        )
        raise
    except Exception as exc:
        metrics.record_query(
            db_alias=req.database,
            db_type=db_type,
            status="error",
            duration_seconds=time.monotonic() - query_started_at,
        )
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
        connection_manager.update_pool_metrics(req.database)

    # 5. Audit log (success)
    metrics.record_query(
        db_alias=req.database,
        db_type=db_type,
        status="success",
        duration_seconds=response.elapsed_ms / 1000,
        row_count=response.row_count,
    )
    try:
        await log_query(db, user=username, database_alias=req.database,
                        sql=req.sql, params=req.params, row_count=response.row_count,
                        elapsed_ms=response.elapsed_ms, status="success")
    except Exception:
        logger.exception("Failed to write audit log for successful query")

    return response


def _validate_read_only_template_sql(sql: str, db_type: str) -> None:
    """Reject any template SQL that is not a read-only SELECT/EXPLAIN.

    Shared by the admin template CRUD and the query-user content edit so both
    surfaces enforce the same read-only guarantee.
    """
    try:
        statement_type = _detect_statement_type(sql, db_type)
    except HTTPException as exc:
        if db_type == "graphdb" and exc.status_code == 422:
            statement_type = "reject"
        else:
            raise
    if statement_type not in {"select", "explain"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query templates must be read-only SELECT/EXPLAIN statements",
        )


def _template_audit_snapshot(template: QueryTemplate) -> dict:
    return {
        "path": template.path,
        "name": template.name,
        "description": template.description or "",
        "database": template.db_alias,
        "sql": template.sql,
        "default_limit": template.default_limit,
        "timeout": template.timeout,
        "enabled": template.enabled,
    }


def _to_template_response(template: QueryTemplate) -> QueryTemplateResponse:
    return QueryTemplateResponse(
        id=template.id,
        path=template.path,
        name=template.name,
        description=template.description or "",
        database=template.db_alias,
        sql=template.sql,
        default_limit=template.default_limit,
        timeout=template.timeout,
        enabled=template.enabled,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _decode_allowed_tables(value: str | None) -> list[str] | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring malformed allowed_tables while listing query templates")
        return []
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        logger.warning("Ignoring non-string allowed_tables while listing query templates")
        return []
    return decoded


async def _template_access_scope(
    db: AsyncSession, user: CurrentUser | ApiKeyUser
) -> tuple[set[str] | None, dict[str, list[str] | None], list[str] | None]:
    """Return DB and table scopes used by template discovery.

    JWT permissions without SELECT are omitted, matching the execution path.
    """
    if isinstance(user, ApiKeyUser):
        aliases = None if "*" in user.allowed_databases else set(user.allowed_databases)
        return aliases, {}, user.allowed_tables

    user_perms = await get_role_permissions(db, user.role)
    if "query.databases.write" in user_perms:
        return None, {}, None

    result = await db.execute(
        select(Permission).where(
            Permission.role == user.role,
            Permission.allow_select.is_(True),
        )
    )
    permissions = list(result.scalars().all())
    table_limits = {
        permission.db_alias: _decode_allowed_tables(permission.allowed_tables)
        for permission in permissions
    }
    return set(table_limits), table_limits, None


def _template_tables_allowed(
    template: QueryTemplate,
    *,
    db_types: dict[str, str],
    per_database_limits: dict[str, list[str] | None],
    api_key_table_limit: list[str] | None,
) -> bool:
    db_type = db_types.get(template.db_alias)
    if db_type is None:
        return False
    if db_type in {"neo4j", "graphdb"}:
        return True
    allowed_tables = (
        api_key_table_limit
        if api_key_table_limit is not None
        else per_database_limits.get(template.db_alias)
    )
    if allowed_tables is None:
        return True
    referenced = extract_tables(template.sql, db_type=db_type)
    return check_table_access(referenced, allowed_tables) is None


@router.get(
    "/query/templates/guide",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/markdown": {}}}},
)
async def query_template_agent_guide(
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """Serve the API-key agent guide as Markdown on a stable gateway URL."""
    await _require_template_read_access(db, user)
    return PlainTextResponse(
        _TEMPLATE_AGENT_GUIDE_PATH.read_text(encoding="utf-8"),
        media_type="text/markdown",
    )


@router.post("/query/templates/{template_path:path}", response_model=QueryResponse)
async def execute_template(
    template_path: str,
    body: QueryTemplateExecuteRequest | None = None,
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> QueryResponse:
    """Execute a saved read-only query template by stable path."""
    await _require_template_read_access(db, user)
    try:
        normalized_path = normalize_query_template_path(template_path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    result = await db.execute(
        select(QueryTemplate).where(QueryTemplate.path == normalized_path)
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Query template path '{normalized_path}' not found",
        )
    if not template.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Query template path '{normalized_path}' is disabled",
        )

    execute_body = body or QueryTemplateExecuteRequest()
    request = QueryRequest(
        database=template.db_alias,
        sql=template.sql,
        params=execute_body.params,
        limit=execute_body.limit if execute_body.limit is not None else template.default_limit,
        timeout=execute_body.timeout if execute_body.timeout is not None else template.timeout,
    )
    return await execute(request, user=user, db=db)


@router.get("/query/templates", response_model=list[QueryTemplateResponse])
async def list_accessible_query_templates(
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> list[QueryTemplateResponse]:
    """List the enabled query templates the caller may execute.

    Discovery endpoint for programmatic callers: an LLM agent using an API key
    can enumerate the templates it is allowed to run (then POST to
    ``/query/templates/{path}``), and JWT users get the same view. Scope
    mirrors the execute path, so a template only appears when the caller could
    actually run it — API keys are limited by ``allowed_databases`` and
    ``allowed_tables``; JWT users need SELECT access within their per-database
    table scope. Disabled templates are never listed.
    """
    await _require_template_read_access(db, user)
    accessible, per_database_limits, api_key_table_limit = await _template_access_scope(
        db, user
    )
    stmt = select(QueryTemplate).where(QueryTemplate.enabled.is_(True))
    if accessible is not None:
        if not accessible:
            return []
        stmt = stmt.where(QueryTemplate.db_alias.in_(accessible))
    result = await db.execute(stmt.order_by(QueryTemplate.path.asc()))
    templates = list(result.scalars().all())
    if not templates:
        return []

    aliases = {template.db_alias for template in templates}
    db_types_result = await db.execute(
        select(DBConnection.alias, DBConnection.db_type).where(DBConnection.alias.in_(aliases))
    )
    db_types = {alias: db_type for alias, db_type in db_types_result.all()}
    visible = [
        template
        for template in templates
        if _template_tables_allowed(
            template,
            db_types=db_types,
            per_database_limits=per_database_limits,
            api_key_table_limit=api_key_table_limit,
        )
    ]
    return [_to_template_response(template) for template in visible]


@router.patch(
    "/query/templates/{template_path:path}",
    response_model=QueryTemplateResponse,
)
async def update_query_template_as_agent(
    template_path: str,
    body: QueryTemplateAgentUpdate,
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> QueryTemplateResponse:
    """Edit safe content fields through the independently granted write route."""
    if isinstance(user, ApiKeyUser):
        if not _api_key_has_route(user, QUERY_TEMPLATE_WRITE_ROUTE_ID):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required API key route: {QUERY_TEMPLATE_WRITE_ROUTE_ID}",
            )
    else:
        user_perms = await get_role_permissions(db, user.role)
        if "query.settings.write" not in user_perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Required permission: query.settings.write",
            )

    try:
        normalized_path = normalize_query_template_path(template_path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    result = await db.execute(
        select(QueryTemplate).where(QueryTemplate.path == normalized_path)
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Query template path '{normalized_path}' not found",
        )
    if isinstance(user, ApiKeyUser) and not template.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Query template path '{normalized_path}' is disabled",
        )

    if isinstance(user, ApiKeyUser):
        if "*" not in user.allowed_databases and template.db_alias not in user.allowed_databases:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"API key '{user.consumer_name}' is not allowed to access "
                    f"database '{template.db_alias}'"
                ),
            )

    connection_result = await db.execute(
        select(DBConnection).where(DBConnection.alias == template.db_alias)
    )
    connection = connection_result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database '{template.db_alias}' not found",
        )

    new_sql = template.sql
    if "sql" in body.model_fields_set:
        assert body.sql is not None
        new_sql = body.sql
        _validate_read_only_template_sql(new_sql, connection.db_type)

    if (
        isinstance(user, ApiKeyUser)
        and user.allowed_tables is not None
        and connection.db_type not in {"neo4j", "graphdb"}
    ):
        referenced = extract_tables(new_sql, db_type=connection.db_type)
        table_error = check_table_access(referenced, user.allowed_tables)
        if table_error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=table_error)

    before_snapshot = _template_audit_snapshot(template)
    changes: dict[str, object] = {}
    if "sql" in body.model_fields_set:
        changes["sql"] = new_sql
    if "description" in body.model_fields_set:
        changes["description"] = body.description
    if "default_limit" in body.model_fields_set:
        changes["default_limit"] = body.default_limit
    if "timeout" in body.model_fields_set:
        changes["timeout"] = body.timeout

    if body.expected_updated_at is not None:
        result = await db.execute(
            update(QueryTemplate)
            .where(
                QueryTemplate.id == template.id,
                QueryTemplate.updated_at == body.expected_updated_at,
            )
            .values(**changes, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Query template changed since it was discovered; fetch it again before editing",
            )
    else:
        for field_name, value in changes.items():
            setattr(template, field_name, value)

    await db.commit()
    await db.refresh(template)

    actor = f"apikey:{user.consumer_name}" if isinstance(user, ApiKeyUser) else user.username
    await log_admin_action(
        db,
        actor=actor,
        action="update",
        resource_type="query_template",
        resource_id=normalized_path,
        summary=template.name,
        before=before_snapshot,
        after=_template_audit_snapshot(template),
    )
    return _to_template_response(template)


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
