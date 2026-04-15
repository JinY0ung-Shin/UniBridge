from __future__ import annotations

import asyncio
import logging
from typing import Any, NoReturn
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ApiKeyUser, CurrentUser, get_current_user_or_apikey, get_role_permissions, require_permission
from app.database import get_db
from app.models import S3Connection
from app.schemas import S3ConnectionCreate, S3ConnectionResponse, S3ConnectionUpdate
from app.services.connection_manager import decrypt_password, encrypt_password
from app.services.s3_manager import s3_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["S3"])

MASK_KEEP = 4


def _mask_access_key(key: str) -> str:
    if len(key) <= MASK_KEEP:
        return "***"
    return "***" + key[-MASK_KEEP:]


def _to_response(conn: S3Connection) -> S3ConnectionResponse:
    resp = S3ConnectionResponse.model_validate(conn)
    try:
        plain_key = decrypt_password(conn.access_key_id_encrypted)
        resp.access_key_id_masked = _mask_access_key(plain_key)
    except Exception:
        resp.access_key_id_masked = "***"
    return resp


# ── Admin: Connection CRUD ──────────────────────────────────────────────────


@router.post("/admin/s3/connections", status_code=status.HTTP_201_CREATED)
async def create_s3_connection(
    body: S3ConnectionCreate,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("s3.connections.write")),
) -> S3ConnectionResponse:
    existing = await db.execute(
        select(S3Connection).where(S3Connection.alias == body.alias)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"S3 connection '{body.alias}' already exists",
        )

    conn = S3Connection(
        alias=body.alias,
        endpoint_url=body.endpoint_url or None,
        region=body.region,
        access_key_id_encrypted=encrypt_password(body.access_key_id),
        secret_access_key_encrypted=encrypt_password(body.secret_access_key),
        default_bucket=body.default_bucket or None,
        use_ssl=body.use_ssl,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)

    try:
        await s3_manager.add_connection(conn)
    except Exception as exc:
        logger.warning("S3 client creation failed for '%s': %s", body.alias, exc)

    resp = _to_response(conn)
    resp.status = "registered" if s3_manager.has_connection(body.alias) else "error"
    return resp


@router.get("/admin/s3/connections")
async def list_s3_connections(
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("s3.connections.read")),
) -> list[S3ConnectionResponse]:
    result = await db.execute(select(S3Connection))
    connections = result.scalars().all()
    items = []
    for conn in connections:
        resp = _to_response(conn)
        resp.status = "registered" if s3_manager.has_connection(conn.alias) else "disconnected"
        items.append(resp)
    return items


@router.get("/admin/s3/connections/{alias}")
async def get_s3_connection(
    alias: str,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("s3.connections.read")),
) -> S3ConnectionResponse:
    result = await db.execute(
        select(S3Connection).where(S3Connection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not found")
    resp = _to_response(conn)
    resp.status = "registered" if s3_manager.has_connection(alias) else "disconnected"
    return resp


@router.put("/admin/s3/connections/{alias}")
async def update_s3_connection(
    alias: str,
    body: S3ConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("s3.connections.write")),
) -> S3ConnectionResponse:
    result = await db.execute(
        select(S3Connection).where(S3Connection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not found")

    provided = body.model_fields_set
    if "endpoint_url" in provided:
        conn.endpoint_url = body.endpoint_url or None
    if "region" in provided and body.region is not None:
        conn.region = body.region
    if "access_key_id" in provided and body.access_key_id is not None:
        conn.access_key_id_encrypted = encrypt_password(body.access_key_id)
    if "secret_access_key" in provided and body.secret_access_key is not None:
        conn.secret_access_key_encrypted = encrypt_password(body.secret_access_key)
    if "default_bucket" in provided:
        conn.default_bucket = body.default_bucket or None
    if "use_ssl" in provided and body.use_ssl is not None:
        conn.use_ssl = body.use_ssl

    await db.commit()
    await db.refresh(conn)

    try:
        await s3_manager.add_connection(conn)
    except Exception as exc:
        logger.warning("S3 client re-creation failed for '%s': %s", alias, exc)

    resp = _to_response(conn)
    resp.status = "registered" if s3_manager.has_connection(alias) else "error"
    return resp


@router.delete(
    "/admin/s3/connections/{alias}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_s3_connection(
    alias: str,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("s3.connections.write")),
) -> None:
    result = await db.execute(
        select(S3Connection).where(S3Connection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not found")

    await s3_manager.remove_connection(alias)
    await db.delete(conn)
    await db.commit()
    logger.info("S3 connection deleted: alias=%s user=%s", alias, _admin.username)


@router.post("/admin/s3/connections/{alias}/test")
async def test_s3_connection(
    alias: str,
    _admin: CurrentUser = Depends(require_permission("s3.connections.read")),
) -> dict[str, Any]:
    if not s3_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not registered")

    ok, message = await s3_manager.test_connection(alias)
    return {"status": "ok" if ok else "error", "message": message}


# ── Browse: Read-only S3 operations (API Key + JWT) ─────────────────────────


async def _require_s3_browse(
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser | ApiKeyUser:
    """Allow access via API key (APISIX consumer-restriction enforces route access)
    or JWT with s3.browse permission."""
    if isinstance(user, CurrentUser):
        perms = await get_role_permissions(db, user.role)
        if "s3.browse" not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Required permission: s3.browse",
            )
    return user


def _handle_s3_error(alias: str, exc: Exception) -> NoReturn:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        logger.warning("S3 error for '%s': %s %s", alias, code, msg)
        if code in ("NoSuchBucket", "NoSuchKey", "404"):
            raise HTTPException(status_code=404, detail="Resource not found")
        if code in ("AccessDenied", "403"):
            raise HTTPException(status_code=403, detail="S3 access denied")
        raise HTTPException(status_code=502, detail="S3 operation failed")
    logger.error("Unexpected S3 error for '%s': %s", alias, exc)
    raise HTTPException(status_code=502, detail="S3 operation failed")


@router.get("/s3/{alias}/buckets")
async def list_buckets(
    alias: str,
    _user: CurrentUser | ApiKeyUser = Depends(_require_s3_browse),
) -> list[dict[str, Any]]:
    if not s3_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not found")
    try:
        return await s3_manager.list_buckets(alias)
    except (BotoCoreError, ClientError) as exc:
        _handle_s3_error(alias, exc)
    except Exception:
        logger.exception("Failed to list buckets for '%s'", alias)
        raise HTTPException(status_code=502, detail="Failed to list buckets")


@router.get("/s3/{alias}/objects")
async def list_objects(
    alias: str,
    bucket: str = Query(..., min_length=1),
    prefix: str = Query(""),
    delimiter: str = Query("/"),
    max_keys: int = Query(200, ge=1, le=1000),
    continuation_token: str | None = Query(None),
    _user: CurrentUser | ApiKeyUser = Depends(_require_s3_browse),
) -> dict[str, Any]:
    if not s3_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not found")
    try:
        return await s3_manager.list_objects(
            alias, bucket, prefix, delimiter, max_keys, continuation_token
        )
    except (BotoCoreError, ClientError) as exc:
        _handle_s3_error(alias, exc)
    except Exception:
        logger.exception("Failed to list objects for '%s'", alias)
        raise HTTPException(status_code=502, detail="Failed to list objects")


@router.get("/s3/{alias}/objects/metadata")
async def get_object_metadata(
    alias: str,
    bucket: str = Query(..., min_length=1),
    key: str = Query(..., min_length=1),
    _user: CurrentUser | ApiKeyUser = Depends(_require_s3_browse),
) -> dict[str, Any]:
    if not s3_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not found")
    try:
        return await s3_manager.get_object_metadata(alias, bucket, key)
    except (BotoCoreError, ClientError) as exc:
        _handle_s3_error(alias, exc)
    except Exception:
        logger.exception("Failed to get object metadata for '%s'", alias)
        raise HTTPException(status_code=502, detail="Failed to get object metadata")


@router.get("/s3/{alias}/objects/presigned-url")
async def get_presigned_download_url(
    alias: str,
    bucket: str = Query(..., min_length=1),
    key: str = Query(..., min_length=1),
    expires_in: int = Query(3600, ge=60, le=43200),
    _user: CurrentUser | ApiKeyUser = Depends(_require_s3_browse),
) -> dict[str, Any]:
    if not s3_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not found")
    try:
        url = await s3_manager.generate_presigned_url(alias, bucket, key, expires_in)
        return {"url": url, "expires_in": expires_in}
    except (BotoCoreError, ClientError) as exc:
        _handle_s3_error(alias, exc)
    except Exception:
        logger.exception("Failed to generate presigned URL for '%s'", alias)
        raise HTTPException(status_code=502, detail="Failed to generate presigned URL")


MAX_PROXY_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MB


@router.get("/s3/{alias}/objects/download")
async def download_object(
    alias: str,
    bucket: str = Query(..., min_length=1),
    key: str = Query(..., min_length=1),
    _user: CurrentUser | ApiKeyUser = Depends(_require_s3_browse),
) -> StreamingResponse:
    """Proxy-download an S3 object through UniBridge (works for internal S3 endpoints)."""
    if not s3_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"S3 connection '{alias}' not found")

    # Check size before streaming
    try:
        meta = await s3_manager.get_object_metadata(alias, bucket, key)
    except (BotoCoreError, ClientError) as exc:
        _handle_s3_error(alias, exc)
    except Exception:
        logger.exception("Failed to check object size for '%s'", alias)
        raise HTTPException(status_code=502, detail="Failed to download object")

    file_size = meta.get("size")
    if file_size is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to determine object size",
        )
    if file_size > MAX_PROXY_DOWNLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large for proxy download (max {MAX_PROXY_DOWNLOAD_BYTES // (1024 * 1024)}MB). Use presigned-url instead.",
        )

    # Fetch the object
    try:
        resp = await s3_manager.get_object(alias, bucket, key)
    except (BotoCoreError, ClientError) as exc:
        _handle_s3_error(alias, exc)
    except Exception:
        logger.exception("Failed to download object for '%s'", alias)
        raise HTTPException(status_code=502, detail="Failed to download object")

    body = resp["Body"]
    content_type = resp.get("ContentType", "application/octet-stream")
    content_length = resp.get("ContentLength")
    filename = key.rsplit("/", 1)[-1] or "download"

    async def _stream_body():
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, 1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(body.close)

    headers: dict[str, str] = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
    }
    if content_length is not None:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(
        _stream_body(),
        media_type=content_type,
        headers=headers,
    )
