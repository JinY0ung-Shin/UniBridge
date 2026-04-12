from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from httpx import HTTPStatusError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_permission
from app.database import get_db
from app.models import ApiKeyAccess
from app.schemas import ApiKeyCreate, ApiKeyResponse, ApiKeyUpdate
from app.services import apisix_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api-keys", tags=["API Keys"])

MASK_KEEP = 4


def _mask_key(value: str) -> str:
    if len(value) <= MASK_KEEP:
        return "***"
    return "***" + value[-MASK_KEEP:]


def _extract_api_key(consumer: dict, mask: bool = True) -> str | None:
    plugins = consumer.get("plugins", {})
    key = plugins.get("key-auth", {}).get("key")
    if not key:
        return None
    return _mask_key(key) if mask else key


def _to_response(
    access: ApiKeyAccess,
    api_key: str | None = None,
    key_created: bool = False,
) -> ApiKeyResponse:
    return ApiKeyResponse(
        name=access.consumer_name,
        description=access.description or "",
        api_key=api_key,
        key_created=key_created,
        allowed_databases=json.loads(access.allowed_databases) if access.allowed_databases else [],
        allowed_routes=json.loads(access.allowed_routes) if access.allowed_routes else [],
        created_at=access.created_at,
    )


async def _sync_consumer_restriction(allowed_routes: list[str], consumer_name: str) -> None:
    """Update consumer-restriction plugin on routes to include/exclude this consumer.

    Raises HTTPException on failure so the caller can abort before committing DB changes.
    On partial failure, attempts best-effort rollback of already-applied changes.
    """
    try:
        result = await apisix_client.list_resources("routes")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to list APISIX routes for consumer-restriction sync: {exc}",
        )

    # Collect changes needed: [(route_id, old_body, new_body)]
    changes: list[tuple[str, dict, dict]] = []
    for route in result.get("items", []):
        route_id = route.get("id")
        if not route_id:
            continue
        plugins = route.get("plugins", {})
        if "key-auth" not in plugins:
            continue

        old_body = {k: v for k, v in route.items() if k not in ("id", "create_time", "update_time")}

        new_plugins = dict(plugins)
        cr = new_plugins.get("consumer-restriction", {})
        whitelist = set(cr.get("whitelist", []))

        if route_id in allowed_routes:
            whitelist.add(consumer_name)
        else:
            whitelist.discard(consumer_name)

        if whitelist:
            new_plugins["consumer-restriction"] = {"whitelist": sorted(whitelist)}
        else:
            new_plugins.pop("consumer-restriction", None)

        new_body = dict(old_body)
        new_body["plugins"] = new_plugins
        changes.append((route_id, old_body, new_body))

    # Apply changes, tracking which succeeded for rollback
    applied: list[tuple[str, dict]] = []  # [(route_id, old_body)]
    for route_id, old_body, new_body in changes:
        try:
            await apisix_client.put_resource("routes", route_id, new_body)
            applied.append((route_id, old_body))
        except Exception as exc:
            # Best-effort rollback of already-applied changes
            for rb_route_id, rb_old_body in applied:
                try:
                    await apisix_client.put_resource("routes", rb_route_id, rb_old_body)
                except Exception:
                    logger.error("Rollback failed for route %s during consumer-restriction sync", rb_route_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to update consumer-restriction on route {route_id}: {exc}. "
                       f"Rolled back {len(applied)} previously applied change(s).",
            )


@router.get("", response_model=list[ApiKeyResponse])
async def list_api_keys(
    _admin: CurrentUser = Depends(require_permission("apikeys.read")),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKeyResponse]:
    result = await db.execute(select(ApiKeyAccess).order_by(ApiKeyAccess.created_at.desc()))
    keys = result.scalars().all()

    responses = []
    for access in keys:
        masked_key = None
        try:
            consumer = await apisix_client.get_resource("consumers", access.consumer_name)
            masked_key = _extract_api_key(consumer, mask=True)
        except Exception:
            pass
        responses.append(_to_response(access, api_key=masked_key))
    return responses


@router.post("", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate,
    _admin: CurrentUser = Depends(require_permission("apikeys.write")),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyResponse:
    existing = await db.execute(
        select(ApiKeyAccess).where(ApiKeyAccess.consumer_name == body.name)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"API key '{body.name}' already exists")

    consumer_body: dict = {"username": body.name}
    if body.api_key:
        consumer_body["plugins"] = {"key-auth": {"key": body.api_key}}

    try:
        consumer = await apisix_client.put_resource("consumers", body.name, consumer_body)
    except HTTPStatusError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"APISIX error: {exc.response.text}")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to create APISIX consumer: {exc}")

    # Sync consumer-restriction BEFORE committing DB — abort on failure
    # If sync fails, clean up the APISIX consumer we just created
    if body.allowed_routes:
        try:
            await _sync_consumer_restriction(body.allowed_routes, body.name)
        except HTTPException:
            try:
                await apisix_client.delete_resource("consumers", body.name)
            except Exception:
                logger.error("Failed to clean up APISIX consumer '%s' after sync failure", body.name)
            raise

    access = ApiKeyAccess(
        consumer_name=body.name,
        description=body.description,
        allowed_databases=json.dumps(body.allowed_databases) if body.allowed_databases else None,
        allowed_routes=json.dumps(body.allowed_routes) if body.allowed_routes else None,
    )
    db.add(access)
    await db.commit()
    await db.refresh(access)

    # Use the key we sent, not the PUT response (APISIX 3.x encrypts it in PUT responses)
    return _to_response(access, api_key=body.api_key, key_created=True)


@router.put("/{name}", response_model=ApiKeyResponse)
async def update_api_key(
    name: str,
    body: ApiKeyUpdate,
    _admin: CurrentUser = Depends(require_permission("apikeys.write")),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyResponse:
    result = await db.execute(
        select(ApiKeyAccess).where(ApiKeyAccess.consumer_name == name)
    )
    access = result.scalar_one_or_none()
    if access is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key '{name}' not found")

    key_created = False
    api_key_display: str | None = None
    old_consumer_plugins: dict | None = None
    if body.api_key:
        try:
            existing_consumer = await apisix_client.get_resource("consumers", name)
            existing_plugins = existing_consumer.get("plugins", {})
            old_consumer_plugins = dict(existing_plugins)  # snapshot for rollback
        except Exception:
            existing_plugins = {}
        existing_plugins["key-auth"] = {"key": body.api_key}
        try:
            consumer = await apisix_client.put_resource("consumers", name, {
                "username": name, "plugins": existing_plugins,
            })
            api_key_display = body.api_key  # Use the key we sent, not PUT response (APISIX 3.x encrypts it)
            key_created = True
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to update APISIX consumer: {exc}")

    # Sync consumer-restriction BEFORE committing DB — abort on failure
    # If sync fails and we already updated the consumer key, rollback the key change
    if body.allowed_routes is not None:
        try:
            await _sync_consumer_restriction(body.allowed_routes, name)
        except HTTPException:
            if key_created and old_consumer_plugins is not None:
                try:
                    await apisix_client.put_resource("consumers", name, {
                        "username": name, "plugins": old_consumer_plugins,
                    })
                except Exception:
                    logger.error("Failed to rollback APISIX consumer '%s' key after sync failure", name)
            raise

    if body.description is not None:
        access.description = body.description
    if body.allowed_databases is not None:
        access.allowed_databases = json.dumps(body.allowed_databases) if body.allowed_databases else None
    if body.allowed_routes is not None:
        access.allowed_routes = json.dumps(body.allowed_routes) if body.allowed_routes else None

    await db.commit()
    await db.refresh(access)

    if not key_created:
        try:
            consumer = await apisix_client.get_resource("consumers", name)
            api_key_display = _extract_api_key(consumer, mask=True)
        except Exception:
            pass

    return _to_response(access, api_key=api_key_display, key_created=key_created)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_api_key(
    name: str,
    _admin: CurrentUser = Depends(require_permission("apikeys.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(ApiKeyAccess).where(ApiKeyAccess.consumer_name == name)
    )
    access = result.scalar_one_or_none()
    if access is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key '{name}' not found")

    old_routes = json.loads(access.allowed_routes) if access.allowed_routes else []
    if old_routes:
        await _sync_consumer_restriction([], name)

    try:
        await apisix_client.delete_resource("consumers", name)
    except HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"APISIX error: {exc.response.text}")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to delete APISIX consumer: {exc}")

    await db.delete(access)
    await db.commit()
