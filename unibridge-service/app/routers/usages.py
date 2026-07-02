"""API-key-facing usage endpoint, exposed through the APISIX gateway.

``GET /usages`` is provisioned as the ``usages-api`` gateway route
(``/api/usages``, key-auth) so API-key consumers can check their own per-route
request counts without a Keycloak JWT — grant a key the ``usages-api`` route
like any other. Regular API-key callers are always scoped to their own
consumer regardless of the ``consumer`` query param (and cannot see LLM
routes); master keys (``*``/``*``, admin-created only — self-service keys can
never be master) get the unrestricted admin view: all consumers combined by
default, any ``consumer`` filter, and ``include_llm``. JWT callers get the
same read/self scoping as the admin metrics endpoints.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ApiKeyUser, CurrentUser, get_current_user_or_apikey
from app.database import get_db
from app.routers.api_keys import _is_master_access
from app.routers.gateway import _monitoring_scope_for, _MonitoringScope, usages_payload

router = APIRouter(tags=["Usages"])


@router.get("/usages")
async def get_usages(
    date: str | None = Query(
        None,
        description="KST calendar date (YYYY-MM-DD). Defaults to today (KST).",
    ),
    consumer: str | None = Query(
        None,
        description="Consumer filter (JWT admins only; API-key callers are always self-scoped)",
    ),
    include_llm: bool = Query(False, description="Include LLM routes (JWT admins only)"),
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-route request counts for one KST calendar day."""
    if isinstance(user, ApiKeyUser):
        if _is_master_access(user.allowed_databases, user.allowed_routes):
            scope = _MonitoringScope(forced_consumer=None, restricted=False)
        else:
            scope = _MonitoringScope(forced_consumer=user.consumer_name, restricted=True)
    else:
        scope = await _monitoring_scope_for(user, db)
    return await usages_payload(scope, date=date, consumer=consumer, include_llm=include_llm)
