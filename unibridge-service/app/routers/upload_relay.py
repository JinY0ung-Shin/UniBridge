"""Upload relay: a Noah user hands a file to the Noah server through UniBridge.

UniBridge's address is one the workstation file-security agent decrypts
uploads for; Noah's is not. So Noah opens ``/upload-relay`` on UniBridge in a
popup with a ticket it signed, the user signs in to Keycloak and picks the
files there, and UniBridge keeps each one for a few minutes until the Noah
server has collected it. UniBridge never calls Noah.

Browser endpoints (an APPROVED UniBridge account: ``get_current_user``, any app
role; an account still waiting for approval gets the usual 401. The login says
who uploads, the signed ticket says for whom and what):

- ``POST /upload-relay/ticket``  what a ticket allows, for the page to show.
- ``POST /upload-relay/files``   one file, as multipart/form-data. The ticket
  comes in the ``X-Upload-Relay-Ticket`` header and is verified BEFORE the
  body is read (its own size limit bounds the read); a ``ticket`` form field
  is accepted when the header is absent.

Noah-server endpoints (no Keycloak; each request is signed with the shared
secret, see app/services/upload_relay.py):

- ``GET  /upload-relay/tickets/{tid}/files``  the files waiting under a ticket.
- ``GET  /upload-relay/files/{id}``           one file's bytes (the first
  download is recorded: ``collected_at`` and a ``collect`` audit row).
- ``POST /upload-relay/files/{id}/ack``       delivered (or refused): delete it.

Every upload, refusal, collection, acknowledgement and expiry is written to
the admin audit log (resource type ``upload_relay``) and to the service log,
with the file name, size and SHA-256 — never the ticket or the secret. An
upload attempt WITHOUT a valid ticket is only logged (a warning): every
approved account can make one as often as it likes, and those must not be
able to fill the audit log. With
``UPLOAD_RELAY_SECRET`` empty the relay is off and every endpoint answers 503.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.auth import CurrentUser, get_current_user
from app.config import settings
from app.database import get_db
from app.db_types import utcnow
from app.models import UploadRelayFile
from app.services import upload_relay as relay
from app.services.audit import log_admin_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload-relay", tags=["Upload relay"])

TICKET_HEADER = "X-Upload-Relay-Ticket"
TIMESTAMP_HEADER = "X-Noah-Relay-Timestamp"
SIGNATURE_HEADER = "X-Noah-Relay-Signature"
_NO_STORE = {"Cache-Control": "no-store"}
# Room in an upload body for everything that is not the file: the ticket field
# and the multipart framing.
_BODY_OVERHEAD_BYTES = 64 * 1024

# The disabled relay is reported once per process, not once per request. Reset
# in tests.
_disabled_warning_logged = False


class TicketRequest(BaseModel):
    ticket: str = Field(..., max_length=relay.MAX_TICKET_CHARS)


class AckRequest(BaseModel):
    outcome: Literal["delivered", "refused"]
    reason: str | None = Field(None, max_length=200)


class _BodyTooLarge(MultiPartException):
    """The body passed the byte limit while it was being read.

    A MultiPartException so that the parser closes the temporary file it had
    already opened for the part, as it does for its own errors.
    """

    def __init__(self) -> None:
        super().__init__("The request body is too large")


class _UploadRefused(Exception):
    """An upload this handler turns away.

    ``ticket`` is set only when a valid (correctly signed, unexpired) ticket
    came with the attempt: those refusals are audited. Without one the attempt
    is anonymous as far as Noah is concerned and is only logged.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        ticket: relay.RelayTicket | None = None,
        tid: str | None = None,
        filename: str | None = None,
        size: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.ticket = ticket
        self.tid = ticket.tid if ticket else tid
        self.filename = filename
        self.size = size


def _require_configured() -> None:
    """503 while UPLOAD_RELAY_SECRET is empty (read at call time, for tests)."""
    if relay.relay_configured():
        return
    global _disabled_warning_logged
    if not _disabled_warning_logged:
        _disabled_warning_logged = True
        logger.warning(
            "An upload-relay request arrived but UPLOAD_RELAY_SECRET is empty: the "
            "relay is off and every request is rejected with 503. Set it to the "
            "secret the Noah deployment uses. Logged once per process."
        )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="The upload relay is not configured",
    )


def _verify_noah_signature(request: Request) -> None:
    """Gate a Noah-server endpoint on the shared-secret request signature."""
    _require_configured()
    if not relay.request_signature_valid(
        request.method,
        request.url.path,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing, stale or invalid request signature",
        )


def _ticket_or_http_error(token: object) -> relay.RelayTicket:
    try:
        return relay.verify_ticket(token)
    except relay.TicketError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _stored_count(db: AsyncSession, tid: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(UploadRelayFile)
                .where(UploadRelayFile.ticket_id == tid)
            )
        ).scalar_one()
    )


# ── Browser: what the ticket allows ─────────────────────────────────────────


@router.post("/ticket")
async def describe_ticket(
    body: TicketRequest,
    _user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Who the files go to and what may be uploaded, for the page to show."""
    _require_configured()
    ticket = _ticket_or_http_error(body.ticket)
    await relay.purge_expired(db)
    stored = await _stored_count(db, ticket.tid)
    return {
        "user": ticket.user,
        "name": ticket.name,
        "expiresAt": relay.iso_utc(ticket.expires_at),
        # For the page's countdown: this server's clock, not the PC's.
        "expiresInSeconds": max(0, ticket.exp - int(utcnow().timestamp())),
        "accept": list(ticket.accept),
        "maxBytes": ticket.effective_max_bytes,
        "maxFiles": ticket.max_files,
        "remainingFiles": max(0, ticket.max_files - stored),
    }


# ── Browser: one file ───────────────────────────────────────────────────────


async def _limited_body(request: Request, limit: int):
    """The request body, cut off once it passes ``limit`` bytes.

    Counted as it arrives: a declared Content-Length is the client's word.
    """
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise _BodyTooLarge()
        yield chunk


def _verified_upload_ticket(token: object) -> relay.RelayTicket:
    try:
        return relay.verify_ticket(token)
    except relay.TicketError as exc:
        raise _UploadRefused(exc.status_code, exc.detail, tid=exc.tid) from None


async def _read_upload(request: Request) -> tuple[relay.RelayTicket, str, bytes]:
    """Parse and judge an upload: its ticket, its clean file name, its bytes.

    The ticket is verified first whenever it comes in the header, so a caller
    without one is turned away before a byte of the body is read, and a caller
    with one is read only up to what that ticket allows.
    """
    ticket: relay.RelayTicket | None = None
    header_ticket = request.headers.get(TICKET_HEADER)
    if header_ticket is not None:
        ticket = _verified_upload_ticket(header_ticket)

    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise _UploadRefused(400, "The upload must be multipart/form-data", ticket=ticket)
    if ticket is not None:
        file_limit = ticket.effective_max_bytes
        too_large = _UploadRefused(413, "The file is larger than this upload allows", ticket=ticket)
    else:
        file_limit = settings.UPLOAD_RELAY_MAX_FILE_BYTES
        too_large = _UploadRefused(413, "The file is larger than this server accepts")
    body_limit = file_limit + _BODY_OVERHEAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > body_limit:
        raise too_large

    parser = MultiPartParser(
        request.headers,
        _limited_body(request, body_limit),
        max_files=1,
        max_fields=1,
        max_part_size=relay.MAX_TICKET_CHARS + 1024,
    )
    try:
        form = await parser.parse()
    except _BodyTooLarge:
        raise too_large from None
    except MultiPartException as exc:
        raise _UploadRefused(400, f"Malformed upload: {exc.message}", ticket=ticket) from None

    try:
        if ticket is None:
            # No header: the ticket travelled as a form field, so it could only
            # be read with the body.
            ticket = _verified_upload_ticket(form.get("ticket"))

        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise _UploadRefused(400, "The upload carries no file", ticket=ticket)
        name = relay.clean_filename(upload.filename)
        if name is None:
            raise _UploadRefused(400, "The file name is not usable", ticket=ticket)
        if relay.filename_extension(name) not in ticket.accept:
            raise _UploadRefused(
                400, "This type of file is not accepted here", ticket=ticket, filename=name
            )
        content = await upload.read()
    finally:
        await form.close()

    if not content:
        raise _UploadRefused(400, "The file is empty", ticket=ticket, filename=name, size=0)
    if len(content) > ticket.effective_max_bytes:
        raise _UploadRefused(
            413,
            "The file is larger than this upload allows",
            ticket=ticket,
            filename=name,
            size=len(content),
        )
    return ticket, name, content


async def _record_refused_upload(db: AsyncSession, uploader: str, refusal: _UploadRefused) -> None:
    """Leave the trace of a refused upload: an audit row when it came with a
    valid ticket, otherwise a warning in the service log and nothing else."""
    if refusal.ticket is None:
        # Every approved account can send this as often as it likes, holding
        # no ticket at all: it must not be able to fill the admin audit log.
        logger.warning(
            "Upload relay: refused an upload without a valid ticket (%s) uploader=%s ticket=%s",
            refusal.detail,
            uploader,
            refusal.tid or relay.UNKNOWN_TICKET,
        )
        return
    noah_user = refusal.ticket.user
    logger.info(
        "Upload relay: refused upload (%s) file=%r size=%s uploader=%s noah_user=%s ticket=%s",
        refusal.detail,
        refusal.filename,
        refusal.size,
        uploader,
        noah_user,
        refusal.tid,
    )
    what = refusal.filename or "an upload"
    if refusal.size is not None:
        what = f"{what} ({relay.describe_size(refusal.size)})"
    await log_admin_action(
        db,
        actor=uploader,
        action="upload",
        resource_type=relay.AUDIT_RESOURCE_TYPE,
        resource_id=refusal.tid or relay.UNKNOWN_TICKET,
        summary=f"{what} for Noah user {noah_user} was refused",
        after={
            "ticket_id": refusal.tid,
            "noah_user": noah_user,
            "filename": refusal.filename,
            "size": refusal.size,
            "uploader": uploader,
        },
        status="error",
        error_message=refusal.detail,
    )


async def _pending_bytes(db: AsyncSession, noah_user: str | None = None) -> int:
    query = select(func.coalesce(func.sum(UploadRelayFile.size), 0))
    if noah_user is not None:
        query = query.where(UploadRelayFile.noah_user == noah_user)
    return int((await db.execute(query)).scalar_one())


async def _no_room_for(
    db: AsyncSession, ticket: relay.RelayTicket, name: str, size: int
) -> _UploadRefused | None:
    """Why this file cannot be stored right now, if it cannot.

    Call inside ``relay.storing``: the answer holds only while no other upload
    can count or insert.
    """
    about = {"ticket": ticket, "filename": name, "size": size}
    if await _stored_count(db, ticket.tid) >= ticket.max_files:
        return _UploadRefused(409, "This upload already holds as many files as it allows", **about)
    if await _pending_bytes(db, ticket.user) + size > settings.UPLOAD_RELAY_MAX_PENDING_BYTES_PER_USER:
        return _UploadRefused(
            429,
            "Too much is already waiting for this Noah user; "
            "try again once Noah has collected the earlier files",
            **about,
        )
    if await _pending_bytes(db) + size > settings.UPLOAD_RELAY_MAX_PENDING_BYTES:
        return _UploadRefused(503, "The upload relay is full; try again in a few minutes", **about)
    return None


@router.post(
    "/files",
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "parameters": [
            {
                "name": TICKET_HEADER,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": "The upload ticket; verified before the body is read.",
            }
        ],
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "ticket": {
                                "type": "string",
                                "description": "Only when the header is absent.",
                            },
                            "file": {"type": "string", "format": "binary"},
                        },
                    }
                }
            },
        },
    },
)
async def upload_file(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Take one file under a ticket and hold it for the Noah server."""
    _require_configured()
    uploader = user.username
    try:
        ticket, name, content = await _read_upload(request)
    except _UploadRefused as refusal:
        await _record_refused_upload(db, uploader, refusal)
        raise HTTPException(status_code=refusal.status_code, detail=refusal.detail) from None

    await relay.purge_expired(db)
    now = utcnow()
    row = UploadRelayFile(
        id=uuid.uuid4().hex,
        ticket_id=ticket.tid,
        noah_user=ticket.user,
        noah_name=ticket.name,
        uploader=uploader,
        filename=name,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.UPLOAD_RELAY_FILE_TTL_SECONDS),
    )
    snapshot = relay.file_snapshot(row)
    file_id, sha256 = row.id, row.sha256
    async with relay.storing(db):
        refusal = await _no_room_for(db, ticket, name, len(content))
        if refusal is None:
            db.add(row)
            await db.commit()
        else:
            # Ends the transaction, and with it the lock other uploads wait on.
            await db.rollback()
    if refusal is not None:
        await _record_refused_upload(db, uploader, refusal)
        raise HTTPException(status_code=refusal.status_code, detail=refusal.detail)

    logger.info(
        "Upload relay: stored file=%r size=%s sha256=%s uploader=%s noah_user=%s ticket=%s",
        name,
        len(content),
        sha256,
        uploader,
        ticket.user,
        ticket.tid,
    )
    await log_admin_action(
        db,
        actor=uploader,
        action="upload",
        resource_type=relay.AUDIT_RESOURCE_TYPE,
        resource_id=file_id,
        summary=(
            f"{name} ({relay.describe_size(len(content))}) uploaded for Noah user {ticket.user}"
        ),
        after=snapshot,
    )
    return {"id": file_id, "name": name, "size": len(content), "sha256": sha256}


# ── Noah server: collect ────────────────────────────────────────────────────


@router.get("/tickets/{tid}/files")
async def list_ticket_files(tid: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The files waiting under a ticket, oldest first."""
    _verify_noah_signature(request)
    await relay.purge_expired(db)
    rows = (
        await db.execute(
            select(*relay.FILE_COLUMNS)
            .where(UploadRelayFile.ticket_id == tid, UploadRelayFile.expires_at > utcnow())
            .order_by(UploadRelayFile.created_at, UploadRelayFile.id)
        )
    ).all()
    return {
        "files": [
            {
                "id": row.id,
                "name": row.filename,
                "size": row.size,
                "sha256": row.sha256,
                "uploader": row.uploader,
                "uploadedAt": relay.iso_utc(row.created_at),
            }
            for row in rows
        ]
    }


@router.get("/files/{file_id}")
async def download_file(file_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """One file's bytes. It stays until Noah acknowledges it (or it expires).

    The first download is recorded (``collected_at`` and a ``collect`` audit
    row): from then on Noah has the bytes, whatever happens to the
    acknowledgement.
    """
    _verify_noah_signature(request)
    await relay.purge_expired(db)
    row = (
        await db.execute(
            select(UploadRelayFile.content, *relay.FILE_COLUMNS).where(
                UploadRelayFile.id == file_id, UploadRelayFile.expires_at > utcnow()
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file (collected or expired)")

    first = await db.execute(
        update(UploadRelayFile)
        .where(UploadRelayFile.id == file_id, UploadRelayFile.collected_at.is_(None))
        .values(collected_at=utcnow())
    )
    await db.commit()
    if first.rowcount:
        logger.info(
            "Upload relay: Noah downloaded file=%r size=%s sha256=%s uploader=%s noah_user=%s "
            "ticket=%s",
            row.filename,
            row.size,
            row.sha256,
            row.uploader,
            row.noah_user,
            row.ticket_id,
        )
        await log_admin_action(
            db,
            actor=f"noah:{row.noah_user}",
            action="collect",
            resource_type=relay.AUDIT_RESOURCE_TYPE,
            resource_id=row.id,
            summary=(
                f"{row.filename} ({relay.describe_size(row.size)}) from {row.uploader} was "
                f"downloaded by Noah for user {row.noah_user}"
            ),
            after=relay.file_snapshot(row),
        )
    return Response(
        content=row.content,
        media_type="application/octet-stream",
        headers={**_NO_STORE, "X-Relay-Sha256": row.sha256},
    )


@router.post("/files/{file_id}/ack", status_code=status.HTTP_204_NO_CONTENT)
async def acknowledge_file(
    file_id: str,
    body: AckRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Noah delivered the file (or turned it away): delete it and record which.

    The row is looked up and deleted BEFORE the expiry purge runs. Noah
    acknowledges once the user's browser has the bytes, a few seconds after
    the download, and retries a failed acknowledgement: one that arrives just
    past ``expires_at`` must still be recorded as what it is, not lose to this
    same request's purge and leave an "expired" row for a delivered file.
    """
    _verify_noah_signature(request)
    row = (
        await db.execute(select(*relay.FILE_COLUMNS).where(UploadRelayFile.id == file_id))
    ).first()
    removed = 0
    if row is not None:
        result = await db.execute(sa_delete(UploadRelayFile).where(UploadRelayFile.id == file_id))
        await db.commit()
        # 0: another request (or a purge) removed it between the two statements.
        removed = result.rowcount or 0
    await relay.purge_expired(db)
    if row is None or not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file (collected or expired)")

    delivered = body.outcome == "delivered"
    reason = (body.reason or "").strip() or None
    logger.info(
        "Upload relay: Noah %s file=%r size=%s sha256=%s uploader=%s noah_user=%s ticket=%s%s",
        "delivered" if delivered else "refused",
        row.filename,
        row.size,
        row.sha256,
        row.uploader,
        row.noah_user,
        row.ticket_id,
        f" reason={reason!r}" if reason else "",
    )
    await log_admin_action(
        db,
        actor=f"noah:{row.noah_user}",
        action="deliver" if delivered else "refuse",
        resource_type=relay.AUDIT_RESOURCE_TYPE,
        resource_id=row.id,
        summary=(
            f"{row.filename} ({relay.describe_size(row.size)}) from {row.uploader} was "
            f"{'delivered by' if delivered else 'refused by'} Noah for user {row.noah_user}"
        ),
        after=relay.file_snapshot(row),
        status="success" if delivered else "error",
        error_message=None if delivered else (reason or "Noah refused the file"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
