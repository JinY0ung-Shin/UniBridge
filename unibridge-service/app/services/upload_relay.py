"""Upload relay: tickets, request signatures, file names and expiry.

The relay lets a Noah user hand a file to the Noah server through UniBridge:
the browser uploads it to UniBridge (app/routers/upload_relay.py), UniBridge
keeps it in ``upload_relay_files`` for a few minutes and the Noah server
collects it. UniBridge never calls Noah.

Two things are signed with ``UPLOAD_RELAY_SECRET`` (HMAC-SHA256), each under
its own prefix so one can never be replayed as the other:

- the **ticket** Noah gives the browser: ``v1.<P>.<S>``, P the base64url
  (unpadded) JSON payload, S the base64url (unpadded) MAC of
  ``noah-upload-relay-ticket:v1.<P>``. It says who the files are for and what
  may be uploaded. The browser can read it but cannot alter it.
- each **request** the Noah server makes: headers ``X-Noah-Relay-Timestamp``
  (unix seconds) and ``X-Noah-Relay-Signature``, the lowercase hex MAC of
  ``noah-upload-relay-request:v1\\n<METHOD>\\n<PATH>\\n<TIMESTAMP>``. PATH is
  the path this service sees (``/upload-relay/...``, without ``/_api``).

This is the contract the Noah deployment is written against: changing a
prefix, an encoding or a field name breaks it.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import unicodedata
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db_types import utcnow
from app.models import UploadRelayFile
from app.services.active_color import is_active_instance
from app.services.audit import log_admin_action

logger = logging.getLogger(__name__)

AUDIT_RESOURCE_TYPE = "upload_relay"
# resource_id of an audit row about an upload whose ticket could not be read.
UNKNOWN_TICKET = "-"

TICKET_MAC_PREFIX = "noah-upload-relay-ticket:v1."
REQUEST_MAC_PREFIX = "noah-upload-relay-request:v1"
# A request signature is accepted this many seconds either side of now.
REQUEST_MAX_SKEW_SECONDS = 300

MAX_TICKET_CHARS = 8192
_TICKET_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_EXTENSION = re.compile(r"^\.[a-z0-9]{1,16}$")
MAX_ACCEPT_ENTRIES = 64
MAX_TICKET_FILES = 20
MAX_NAME_CHARS = 200
MAX_FILENAME_CHARS = 255

# Left-to-right/right-to-left embeddings, overrides and isolates: they make a
# name read differently from what it is ("exe.pdf" shown as "fdp.exe").
_BIDI_CONTROLS = frozenset("‎‏‪‫‬‭‮⁦⁧⁨⁩")
_REFUSED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})

PURGE_INTERVAL_SECONDS = 120
PURGE_FIRST_DELAY_SECONDS = 60


def relay_configured() -> bool:
    return bool(settings.UPLOAD_RELAY_SECRET)


# ── Ticket ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RelayTicket:
    tid: str
    user: str
    name: str | None
    exp: int
    accept: tuple[str, ...]
    max_bytes: int
    max_files: int

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.exp, tz=timezone.utc)

    @property
    def effective_max_bytes(self) -> int:
        """What one file may weigh: the ticket's limit, capped by this server's."""
        return min(self.max_bytes, settings.UPLOAD_RELAY_MAX_FILE_BYTES)


class TicketError(Exception):
    """A ticket this server will not accept.

    ``tid`` is set only when the signature verified (an expired ticket): an
    unverified payload is never trusted, not even for a log line.
    """

    def __init__(self, status_code: int, detail: str, tid: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.tid = tid


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _ticket_mac(payload_part: str) -> str:
    digest = hmac.new(
        settings.UPLOAD_RELAY_SECRET.encode("utf-8"),
        (TICKET_MAC_PREFIX + payload_part).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64url(digest)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def storable_text(value: str) -> bool:
    """Whether ``value`` can be stored, logged and sent back as it is.

    JSON can carry half of a surrogate pair (``"\\ud83d"``, e.g. a name cut in
    the middle of an emoji): Python keeps it, but it cannot be encoded as
    UTF-8, so the response, the log line or the INSERT would fail with a 500.
    Control characters (NUL above all: Postgres text cannot hold it) are out
    for the same reason.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(unicodedata.category(ch) == "Cc" for ch in value)


# ``exp`` beyond this (year 5138) is no timestamp: datetime would overflow.
_MAX_EXP = 10**11


def verify_ticket(token: object, *, now: float | None = None) -> RelayTicket:
    """The ticket ``token`` carries, or :class:`TicketError` (400 malformed or
    wrongly signed, 410 expired)."""
    malformed = TicketError(400, "Invalid upload ticket")
    if not isinstance(token, str) or not token or len(token) > MAX_TICKET_CHARS:
        raise malformed
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1] or not parts[2]:
        raise malformed
    payload_part, signature = parts[1], parts[2]
    if not hmac.compare_digest(
        _ticket_mac(payload_part).encode("ascii"),
        signature.encode("utf-8", "surrogatepass"),
    ):
        raise malformed

    # From here on the payload is Noah's own: a fault in it is Noah's bug, but
    # it is still refused rather than half-read.
    try:
        padded = payload_part + "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError):
        raise malformed from None
    if not isinstance(payload, dict):
        raise malformed

    tid = payload.get("tid")
    user = payload.get("user")
    name = payload.get("name")
    exp = payload.get("exp")
    accept = payload.get("accept")
    max_bytes = payload.get("maxBytes")
    max_files = payload.get("maxFiles")
    if (
        not isinstance(tid, str)
        or not _TICKET_ID.match(tid)
        or not isinstance(user, str)
        or not 1 <= len(user) <= MAX_NAME_CHARS
        or not storable_text(user)
        or not (
            name is None
            or (isinstance(name, str) and len(name) <= MAX_NAME_CHARS and storable_text(name))
        )
        or not _is_int(exp)
        or not 0 < exp < _MAX_EXP
        or not isinstance(accept, list)
        or not 1 <= len(accept) <= MAX_ACCEPT_ENTRIES
        or not all(isinstance(ext, str) and _EXTENSION.match(ext) for ext in accept)
        or not _is_int(max_bytes)
        or max_bytes <= 0
        or not _is_int(max_files)
        or not 1 <= max_files <= MAX_TICKET_FILES
    ):
        raise malformed

    current = now if now is not None else utcnow().timestamp()
    if exp <= current:
        raise TicketError(410, "This upload ticket has expired", tid=tid)
    return RelayTicket(
        tid=tid,
        user=user,
        name=name or None,
        exp=exp,
        accept=tuple(accept),
        max_bytes=max_bytes,
        max_files=max_files,
    )


# ── Noah's own requests ─────────────────────────────────────────────────────


def request_signature(method: str, path: str, timestamp: str) -> str:
    message = f"{REQUEST_MAC_PREFIX}\n{method.upper()}\n{path}\n{timestamp}"
    return hmac.new(
        settings.UPLOAD_RELAY_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def request_signature_valid(
    method: str,
    path: str,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float | None = None,
) -> bool:
    """Whether the Noah server signed this request, recently."""
    if not timestamp or not signature or not re.fullmatch(r"[0-9]{1,12}", timestamp):
        return False
    current = now if now is not None else utcnow().timestamp()
    if abs(current - int(timestamp)) > REQUEST_MAX_SKEW_SECONDS:
        return False
    return hmac.compare_digest(
        request_signature(method, path, timestamp).encode("ascii"),
        signature.encode("utf-8", "surrogatepass"),
    )


# ── File names ──────────────────────────────────────────────────────────────


def clean_filename(raw: object) -> str | None:
    """The plain file name to store, or None when ``raw`` is not usable.

    Only the last path component is kept (browsers send a bare name, but the
    field is client-written), NFC-normalised and trimmed. Control characters,
    line separators and bidirectional controls are refused, not stripped: the
    name is shown to the recipient and written to the audit log.
    """
    if not isinstance(raw, str):
        return None
    name = unicodedata.normalize("NFC", re.split(r"[\\/]", raw)[-1]).strip()
    if not 1 <= len(name) <= MAX_FILENAME_CHARS or name in {".", ".."}:
        return None
    if any(ch in _BIDI_CONTROLS or unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in name):
        return None
    if not storable_text(name):
        return None
    return name


def filename_extension(name: str) -> str:
    """``.pdf`` for ``Report.PDF``; empty when the name has no extension."""
    return os.path.splitext(name)[1].lower()


# ── Audit ───────────────────────────────────────────────────────────────────


def describe_size(size: int) -> str:
    return f"{size:,} bytes"


# Everything about a stored file except its bytes. Queries select these
# columns (plain rows, no ORM state) so listing, purging and acknowledging
# never load ``content``.
FILE_COLUMNS = (
    UploadRelayFile.id,
    UploadRelayFile.ticket_id,
    UploadRelayFile.noah_user,
    UploadRelayFile.noah_name,
    UploadRelayFile.uploader,
    UploadRelayFile.filename,
    UploadRelayFile.size,
    UploadRelayFile.sha256,
    UploadRelayFile.created_at,
    UploadRelayFile.expires_at,
    UploadRelayFile.collected_at,
)


def file_snapshot(row: Any) -> dict[str, object]:
    """The audit ``after`` snapshot of a stored file (a row of FILE_COLUMNS)."""
    snapshot: dict[str, object] = {
        "ticket_id": row.ticket_id,
        "noah_user": row.noah_user,
        "filename": row.filename,
        "size": row.size,
        "sha256": row.sha256,
        "uploader": row.uploader,
    }
    if row.collected_at is not None:
        snapshot["collected_at"] = iso_utc(row.collected_at)
    return snapshot


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Limits ──────────────────────────────────────────────────────────────────

# pg_advisory_xact_lock key of the store-a-file critical section ("uprelay1").
_STORE_LOCK_KEY = 0x75_70_72_65_6C_61_79_31
# One lock per event loop: the service has a single loop, but an asyncio.Lock
# belongs to the loop that first waits on it (each test runs its own).
_store_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def store_lock() -> asyncio.Lock:
    return _store_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())


@asynccontextmanager
async def storing(db: AsyncSession):
    """One upload at a time between "is there room?" and the INSERT.

    The limits (files held per ticket, bytes waiting per Noah user and in
    total) are counted and then acted on; two uploads counting at once would
    both see room. Inside this block no other upload counts or inserts: an
    asyncio lock within this process, and on Postgres a transaction-level
    advisory lock across processes (the other blue/green color, should it be
    serving). The advisory lock goes with the transaction, so the block must
    end in a commit or a rollback; the caller does that. SQLite, the legacy
    single-process store, has only the in-process lock.
    """
    async with store_lock():
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STORE_LOCK_KEY})
        yield


# ── Expiry ──────────────────────────────────────────────────────────────────


async def purge_expired(db: AsyncSession) -> int:
    """Delete every file past ``expires_at`` that Noah never acknowledged.

    Each one gets an ``expire`` audit row that says whether Noah ever
    downloaded it (``collected_at``): "never collected" and "collected, not
    acknowledged" are different findings for whoever reads the trail. The
    DELETE is per row and only the caller whose statement removed it writes
    that row, so two passes at once (a request and the background loop, or
    both colors) audit a file once.
    """
    expired = (
        await db.execute(
            select(*FILE_COLUMNS)
            .where(UploadRelayFile.expires_at <= utcnow())
            .order_by(UploadRelayFile.created_at)
        )
    ).all()
    removed = 0
    for row in expired:
        snapshot = file_snapshot(row)
        result = await db.execute(sa_delete(UploadRelayFile).where(UploadRelayFile.id == row.id))
        await db.commit()
        if not result.rowcount:
            continue
        removed += 1
        if row.collected_at is None:
            outcome = "Noah never collected it"
        else:
            outcome = (
                f"Noah collected it at {iso_utc(row.collected_at)} but did not acknowledge it"
            )
        logger.info(
            "Upload relay: expired (%s) file=%r size=%s sha256=%s uploader=%s "
            "noah_user=%s ticket=%s",
            outcome,
            snapshot["filename"],
            snapshot["size"],
            snapshot["sha256"],
            snapshot["uploader"],
            snapshot["noah_user"],
            snapshot["ticket_id"],
        )
        await log_admin_action(
            db,
            actor="system",
            action="expire",
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=row.id,
            summary=(
                f"{snapshot['filename']} ({describe_size(int(snapshot['size']))}) for Noah user "
                f"{snapshot['noah_user']} was deleted when it expired: {outcome}"
            ),
            after=snapshot,
        )
    return removed


async def run_purge_once() -> int:
    """One pass against the meta database."""
    from app.database import async_session

    async with async_session() as db:
        return await purge_expired(db)


async def run_purge_loop(
    *,
    interval_seconds: int = PURGE_INTERVAL_SECONDS,
    first_delay_seconds: int = PURGE_FIRST_DELAY_SECONDS,
) -> None:
    """Background loop: delete unacknowledged files soon after they expire.

    Requests purge too, but a relay nobody is using would otherwise keep its
    last files until the next upload. Only the active blue/green color runs it
    (both share the table). It keeps running while the relay is off, so files
    left from before it was switched off still go. Every failure is swallowed:
    the next pass picks the file up.
    """
    logger.info("Upload relay purge loop started")
    await asyncio.sleep(first_delay_seconds)
    while True:
        try:
            if await is_active_instance():
                await run_purge_once()
        except Exception:
            logger.exception("Upload relay purge pass failed")
        await asyncio.sleep(interval_seconds)
