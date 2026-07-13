from __future__ import annotations

import logging
from typing import Any, NoReturn
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ApiKeyUser, CurrentUser, get_current_user_or_apikey, get_role_permissions, require_permission
from app.config import settings
from app.database import get_db
from app.models import NASConnection
from app.schemas import NasConnectionCreate, NasConnectionResponse, NasConnectionUpdate
from app.services.audit import log_admin_action
from app.services.nas_manager import nas_manager
from app.services.nas_security import NasSecurityError, NasTooLargeError, NasUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["NAS"])


def _to_response(conn: NASConnection) -> NasConnectionResponse:
    return NasConnectionResponse.model_validate(conn)


def _audit_snapshot(conn: NASConnection) -> dict[str, Any]:
    return {
        "alias": conn.alias,
        "base_path": conn.base_path,
        "read_only": conn.read_only,
        "max_download_bytes": conn.max_download_bytes,
        "show_hidden": conn.show_hidden,
        "follow_symlinks": conn.follow_symlinks,
    }


# ── Admin: Connection CRUD ──────────────────────────────────────────────────


@router.post("/admin/nas/connections", status_code=status.HTTP_201_CREATED)
async def create_nas_connection(
    body: NasConnectionCreate,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("nas.connections.write")),
) -> NasConnectionResponse:
    existing = await db.execute(
        select(NASConnection).where(NASConnection.alias == body.alias)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"NAS connection '{body.alias}' already exists",
        )

    conn = NASConnection(
        alias=body.alias,
        base_path=body.base_path,
        read_only=body.read_only,
        max_download_bytes=body.max_download_bytes,
        show_hidden=body.show_hidden,
        follow_symlinks=body.follow_symlinks,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)

    try:
        await nas_manager.add_connection(conn)
    except Exception as exc:
        logger.warning("NAS connection registration failed for '%s': %s", body.alias, exc)

    await log_admin_action(
        db,
        actor=_admin.username,
        action="create",
        resource_type="nas_connection",
        resource_id=conn.alias,
        summary=conn.base_path,
        before=None,
        after=_audit_snapshot(conn),
    )
    resp = _to_response(conn)
    resp.status = "registered" if nas_manager.has_connection(body.alias) else "error"
    return resp


@router.get("/admin/nas/connections")
async def list_nas_connections(
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("nas.connections.read")),
) -> list[NasConnectionResponse]:
    result = await db.execute(select(NASConnection))
    connections = result.scalars().all()
    items = []
    for conn in connections:
        resp = _to_response(conn)
        resp.status = "registered" if nas_manager.has_connection(conn.alias) else "disconnected"
        items.append(resp)
    return items


@router.get("/admin/nas/connections/{alias}")
async def get_nas_connection(
    alias: str,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("nas.connections.read")),
) -> NasConnectionResponse:
    result = await db.execute(
        select(NASConnection).where(NASConnection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail=f"NAS connection '{alias}' not found")
    resp = _to_response(conn)
    resp.status = "registered" if nas_manager.has_connection(alias) else "disconnected"
    return resp


@router.put("/admin/nas/connections/{alias}")
async def update_nas_connection(
    alias: str,
    body: NasConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("nas.connections.write")),
) -> NasConnectionResponse:
    result = await db.execute(
        select(NASConnection).where(NASConnection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail=f"NAS connection '{alias}' not found")

    before_snapshot = _audit_snapshot(conn)

    provided = body.model_fields_set
    if "base_path" in provided and body.base_path is not None:
        conn.base_path = body.base_path
    if "max_download_bytes" in provided:
        conn.max_download_bytes = body.max_download_bytes
    if "show_hidden" in provided and body.show_hidden is not None:
        conn.show_hidden = body.show_hidden
    if "follow_symlinks" in provided and body.follow_symlinks is not None:
        conn.follow_symlinks = body.follow_symlinks

    await db.commit()
    await db.refresh(conn)

    try:
        await nas_manager.add_connection(conn)
    except Exception as exc:
        logger.warning("NAS connection re-registration failed for '%s': %s", alias, exc)

    await log_admin_action(
        db,
        actor=_admin.username,
        action="update",
        resource_type="nas_connection",
        resource_id=alias,
        summary=conn.base_path,
        before=before_snapshot,
        after=_audit_snapshot(conn),
    )
    resp = _to_response(conn)
    resp.status = "registered" if nas_manager.has_connection(alias) else "error"
    return resp


@router.delete(
    "/admin/nas/connections/{alias}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_nas_connection(
    alias: str,
    db: AsyncSession = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission("nas.connections.write")),
) -> None:
    result = await db.execute(
        select(NASConnection).where(NASConnection.alias == alias)
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail=f"NAS connection '{alias}' not found")

    before_snapshot = _audit_snapshot(conn)

    await nas_manager.remove_connection(alias)
    await db.delete(conn)
    await db.commit()
    logger.info("NAS connection deleted: alias=%s user=%s", alias, _admin.username)
    await log_admin_action(
        db,
        actor=_admin.username,
        action="delete",
        resource_type="nas_connection",
        resource_id=alias,
        summary=before_snapshot["base_path"],
        before=before_snapshot,
        after=None,
    )


@router.post("/admin/nas/connections/{alias}/test")
async def test_nas_connection(
    alias: str,
    _admin: CurrentUser = Depends(require_permission("nas.connections.read")),
) -> dict[str, Any]:
    if not nas_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"NAS connection '{alias}' not registered")

    ok, message = await nas_manager.test_connection(alias)
    return {"status": "ok" if ok else "error", "message": message}


# ── Browse: Read-only NAS operations (API Key + JWT) ────────────────────────


async def _require_nas_browse(
    alias: str,
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser | ApiKeyUser:
    """Allow access via API key (APISIX consumer-restriction enforces route access)
    or JWT with nas.browse permission."""
    if isinstance(user, ApiKeyUser):
        if "*" not in user.allowed_databases and alias not in user.allowed_databases:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key '{user.consumer_name}' is not allowed to access NAS alias '{alias}'",
            )
    else:
        perms = await get_role_permissions(db, user.role)
        if "nas.browse" not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Required permission: nas.browse",
            )
    return user


def _handle_nas_error(alias: str, exc: Exception) -> NoReturn:
    if isinstance(exc, NasTooLargeError):
        logger.warning("NAS file too large for '%s': %s", alias, exc)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File too large for download",
        )
    if isinstance(exc, NasUnavailableError):
        logger.warning("NAS unavailable for '%s': %s", alias, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NAS mount unavailable",
        )
    if isinstance(exc, (NasSecurityError, ValueError)):
        logger.warning("NAS invalid path for '%s': %s", alias, exc)
        raise HTTPException(status_code=400, detail="Invalid path")
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        raise HTTPException(status_code=404, detail="Resource not found")
    if isinstance(exc, IsADirectoryError):
        raise HTTPException(status_code=400, detail="Target is a directory")
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail="NAS access denied")
    logger.error("Unexpected NAS error for '%s': %s", alias, exc)
    raise HTTPException(status_code=502, detail="NAS operation failed")


@router.get("/nas/{alias}/entries")
async def list_nas_entries(
    alias: str,
    path: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(settings.NAS_LIST_DEFAULT_LIMIT, ge=1, le=settings.NAS_MAX_LIST_ENTRIES),
    q: str = Query("", max_length=settings.NAS_MAX_PATH_BYTES),
    _user: CurrentUser | ApiKeyUser = Depends(_require_nas_browse),
) -> dict[str, Any]:
    if not nas_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"NAS connection '{alias}' not found")
    try:
        return await nas_manager.list_entries(alias, path, offset=offset, limit=limit, query=q)
    except Exception as exc:
        _handle_nas_error(alias, exc)


@router.get("/nas/{alias}/metadata")
async def get_nas_metadata(
    alias: str,
    path: str = Query(..., min_length=1),
    _user: CurrentUser | ApiKeyUser = Depends(_require_nas_browse),
) -> dict[str, Any]:
    if not nas_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"NAS connection '{alias}' not found")
    try:
        return await nas_manager.stat_path(alias, path)
    except Exception as exc:
        _handle_nas_error(alias, exc)


@router.get("/nas/{alias}/download")
async def download_nas_entry(
    alias: str,
    path: str = Query(..., min_length=1),
    _user: CurrentUser | ApiKeyUser = Depends(_require_nas_browse),
) -> StreamingResponse:
    """Proxy-download a NAS file through UniBridge (read-only, no Range)."""
    if not nas_manager.has_connection(alias):
        raise HTTPException(status_code=404, detail=f"NAS connection '{alias}' not found")

    try:
        gen, meta = await nas_manager.open_read_stream(alias, path)
    except Exception as exc:
        _handle_nas_error(alias, exc)

    filename = meta["filename"]
    headers: dict[str, str] = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        # Defense in depth: never let a browser MIME-sniff an untrusted NAS file
        # (e.g. a .svg/.html) into active content if this response is ever opened
        # as a top-level document.
        "X-Content-Type-Options": "nosniff",
    }
    size = meta.get("size")
    if size is not None:
        headers["Content-Length"] = str(size)

    return StreamingResponse(
        gen,
        media_type=meta["content_type"],
        headers=headers,
    )
