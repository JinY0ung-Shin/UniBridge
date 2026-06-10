"""Per-user query history and saved queries.

Both features are JWT-only (``get_current_user``): API-key consumers
authenticate via the APISIX ``X-Consumer-Username`` header and never reach
these endpoints — ``get_current_user`` only accepts a Bearer token. No extra
permission is required beyond authentication because every endpoint is scoped
to the caller's own rows:

- History reads ``audit_logs`` filtered by ``AuditLog.user``, which
  ``log_query`` fills with ``CurrentUser.username`` for JWT users (API-key
  executions are recorded as ``apikey:<consumer>`` and can never collide).
- Saved queries are keyed by owner (Keycloak ``sub``, matching
  ``ApiKeyAccess.owner``); reads return 404 for rows owned by someone else.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, get_current_user
from app.database import get_db
from app.models import AuditLog, SavedQuery
from app.schemas import (
    AuditLogResponse,
    QueryHistoryResponse,
    SavedQueryCreate,
    SavedQueryResponse,
    SavedQueryUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Query History"])


def _owner_key(user: CurrentUser) -> str:
    """Stable ownership key: Keycloak ``sub`` (like ApiKeyAccess.owner).

    Dev HS256 tokens carry ``sub == username``; fall back to username for
    robustness if a token somehow lacks a subject.
    """
    return user.sub or user.username


# ── My query history ─────────────────────────────────────────────────────────


@router.get("/query/history", response_model=QueryHistoryResponse)
async def list_my_query_history(
    database_alias: str | None = Query(None, description="Filter by database alias"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QueryHistoryResponse:
    """List the current user's own query executions, newest first."""
    filters = [AuditLog.user == user.username]
    if database_alias:
        filters.append(AuditLog.database_alias == database_alias)

    total = (
        await db.execute(select(func.count()).select_from(AuditLog).where(*filters))
    ).scalar_one()

    result = await db.execute(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items = [AuditLogResponse.model_validate(log) for log in result.scalars().all()]
    return QueryHistoryResponse(items=items, total=total)


# ── Saved queries ────────────────────────────────────────────────────────────


async def _get_owned_saved_query(
    db: AsyncSession, user: CurrentUser, saved_query_id: int
) -> SavedQuery:
    """Fetch a saved query owned by the caller, or 404.

    A 404 (not 403) is returned for rows owned by someone else so the endpoint
    does not leak which ids exist.
    """
    result = await db.execute(
        select(SavedQuery).where(
            SavedQuery.id == saved_query_id,
            SavedQuery.owner == _owner_key(user),
        )
    )
    saved = result.scalar_one_or_none()
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Saved query {saved_query_id} not found",
        )
    return saved


@router.get("/query/saved", response_model=list[SavedQueryResponse])
async def list_saved_queries(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SavedQueryResponse]:
    """List the current user's saved queries, most recently updated first."""
    result = await db.execute(
        select(SavedQuery)
        .where(SavedQuery.owner == _owner_key(user))
        .order_by(SavedQuery.updated_at.desc(), SavedQuery.id.desc())
    )
    return [SavedQueryResponse.model_validate(saved) for saved in result.scalars().all()]


@router.post(
    "/query/saved",
    response_model=SavedQueryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_saved_query(
    body: SavedQueryCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedQueryResponse:
    """Save a query for the current user."""
    saved = SavedQuery(
        owner=_owner_key(user),
        name=body.name,
        database_alias=body.database_alias,
        sql_text=body.sql_text,
        description=body.description,
    )
    db.add(saved)
    await db.commit()
    await db.refresh(saved)
    return SavedQueryResponse.model_validate(saved)


@router.put("/query/saved/{saved_query_id}", response_model=SavedQueryResponse)
async def update_saved_query(
    saved_query_id: int,
    body: SavedQueryUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SavedQueryResponse:
    """Update one of the current user's saved queries."""
    saved = await _get_owned_saved_query(db, user, saved_query_id)

    if body.name is not None:
        saved.name = body.name
    if "database_alias" in body.model_fields_set:
        saved.database_alias = body.database_alias
    if body.sql_text is not None:
        saved.sql_text = body.sql_text
    if body.description is not None:
        saved.description = body.description

    await db.commit()
    await db.refresh(saved)
    return SavedQueryResponse.model_validate(saved)


@router.delete(
    "/query/saved/{saved_query_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_saved_query(
    saved_query_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete one of the current user's saved queries."""
    saved = await _get_owned_saved_query(db, user, saved_query_id)
    await db.delete(saved)
    await db.commit()
