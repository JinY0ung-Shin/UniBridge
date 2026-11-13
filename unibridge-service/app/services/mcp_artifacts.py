"""Staged binary artifacts for the MCP surface.

A tool result is JSON text capped at ``max_result_bytes``, so a binary file
never travels in one. Instead it is staged: copied into ``mcp_artifacts`` and
named by an opaque id.

- ``download_artifact`` (app/services/mcp_tools.py) fetches a source file
  through the gateway as the caller, stages it and answers with the id, size,
  SHA-256 and MIME type. The MCP host then fetches the bytes with
  ``GET /api/mcp/artifacts/<id>`` and saves them where its agent can read them.
  A small file can instead come back in the result itself as a standard MCP
  embedded resource (``return_mode=resource``, up to ``Limits.inline_bytes``);
  that one is not staged at all.
- The other way round, the host stages a workspace file with
  ``POST /api/mcp/artifacts`` and ``upload_staged_artifacts`` sends staged
  artifacts on to a gateway API as multipart/form-data.

What holds for every artifact:

- It belongs to the API key that staged it (``consumer``). Another key gets
  the same "not found" as for an id that never existed, and so does a
  malformed or expired id: an answer never tells what is stored.
- It is bounded: one artifact's size, the bytes one key holds, the bytes all
  keys hold and its lifetime come from the MCP settings (``Limits``), and only
  as many transfers run at a time as fit a fixed memory budget
  (``transfer_slot``), as each one holds its file in memory.
- Its size and SHA-256 are taken from the bytes actually received, stored
  with them, and checked again whenever the bytes are read back.
- The same content staged again by the same key is the same artifact: the row
  is kept, its lifetime starts over and the id is returned again.

Rows live in the meta database so both blue/green colors serve the same
artifacts, and are deleted once expired (``purge_expired``).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete as sa_delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_types import utcnow
from app.models import McpArtifact
from app.schemas import McpSettings
from app.services import upload_relay
from app.services.active_color import is_active_instance

logger = logging.getLogger(__name__)

# Where clients reach the staged bytes (the mcp-api route, app/main.py).
PUBLIC_PATH = "/api/mcp/artifacts"
MIB = 1024 * 1024
DEFAULT_CONTENT_TYPE = "application/octet-stream"
FALLBACK_FILENAME = "artifact"

_ARTIFACT_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")

# Transfers in flight in this process each hold one file in memory, and for a
# moment a second copy of it (joining the chunks, the driver's send buffer).
# So their number follows the size limit: as many as keep limit x transfers
# within this budget, which is sized against the service container's memory
# limit (512m in the compose files), and never more than MAX_TRANSFERS.
TRANSFER_BUDGET_BYTES = 96 * MIB
MAX_TRANSFERS = 4
MAX_TRANSFERS_PER_KEY = 2
# Hash in a worker thread from this size on, so a large file does not stall
# the event loop.
_HASH_INLINE_BYTES = 256 * 1024

PURGE_INTERVAL_SECONDS = 120
PURGE_FIRST_DELAY_SECONDS = 60


class ArtifactError(Exception):
    """A refusal with the HTTP status it maps to and a text safe to show."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def not_found() -> ArtifactError:
    """One answer for a malformed, unknown, expired or someone else's id."""
    return ArtifactError(404, "Artifact not found")


@dataclass(frozen=True)
class Limits:
    max_bytes: int
    ttl_seconds: int
    key_bytes: int
    total_bytes: int
    # Largest file returned inline as an embedded resource; 0 = that mode is off.
    inline_bytes: int

    @classmethod
    def of(cls, cfg: McpSettings) -> "Limits":
        # The settings are saved independently of each other. A key cannot hold
        # more than everything, nor one artifact be larger than a key may hold:
        # the smaller setting wins, so a refusal names the limit that applies.
        total_bytes = cfg.artifact_total_mb * MIB
        key_bytes = min(cfg.artifact_key_mb * MIB, total_bytes)
        max_bytes = min(cfg.artifact_max_mb * MIB, key_bytes)
        return cls(
            max_bytes=max_bytes,
            ttl_seconds=cfg.artifact_ttl_seconds,
            key_bytes=key_bytes,
            total_bytes=total_bytes,
            inline_bytes=min(cfg.artifact_resource_max_mb * MIB, max_bytes),
        )

    @property
    def transfers(self) -> int:
        """Transfers this process runs at a time (see TRANSFER_BUDGET_BYTES)."""
        return max(1, min(MAX_TRANSFERS, TRANSFER_BUDGET_BYTES // self.max_bytes))

    @property
    def transfers_per_key(self) -> int:
        """One key's share: it never takes every slot while there are several."""
        return max(1, min(MAX_TRANSFERS_PER_KEY, self.transfers - 1))


@dataclass(frozen=True)
class Artifact:
    """A staged artifact without its bytes."""

    id: str
    consumer: str
    source: str
    filename: str
    content_type: str
    size: int
    sha256: str
    expires_at: datetime

    def describe(self) -> dict[str, Any]:
        """What a client is told: nothing about where or how it is stored."""
        return {
            "artifact_id": self.id,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "mime": self.content_type,
            "expires_at": _iso_utc(self.expires_at),
            "download": {"method": "GET", "path": f"{PUBLIC_PATH}/{self.id}"},
        }


# Everything about an artifact except its bytes.
_INFO_COLUMNS = (
    McpArtifact.id,
    McpArtifact.consumer,
    McpArtifact.source,
    McpArtifact.filename,
    McpArtifact.content_type,
    McpArtifact.size,
    McpArtifact.sha256,
    McpArtifact.expires_at,
)


def _artifact(row: Any, *, expires_at: datetime | None = None) -> Artifact:
    return Artifact(
        id=row.id,
        consumer=row.consumer,
        source=row.source,
        filename=row.filename,
        content_type=row.content_type,
        size=row.size,
        sha256=row.sha256,
        expires_at=expires_at or row.expires_at,
    )


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Names and types ─────────────────────────────────────────────────────────


def clean_filename(raw: object) -> str | None:
    """The plain file name to store (last path component), or None if unusable."""
    return upload_relay.clean_filename(raw)


def clean_content_type(raw: object) -> str:
    """The bare media type, or application/octet-stream when ``raw`` is not one."""
    media = raw.split(";", 1)[0].strip().lower() if isinstance(raw, str) else ""
    return media if _MEDIA_TYPE.fullmatch(media) else DEFAULT_CONTENT_TYPE


async def sha256_hex(content: bytes) -> str:
    if len(content) < _HASH_INLINE_BYTES:
        return hashlib.sha256(content).hexdigest()
    return await asyncio.to_thread(lambda: hashlib.sha256(content).hexdigest())


async def base64_text(content: bytes) -> str:
    if len(content) < _HASH_INLINE_BYTES:
        return base64.b64encode(content).decode("ascii")
    return await asyncio.to_thread(lambda: base64.b64encode(content).decode("ascii"))


async def collect(chunks: AsyncIterator[bytes], limit: int) -> bytes | None:
    """All of ``chunks`` as one bytes object, or None once they pass ``limit``.

    Counted as they arrive: an announced length is the sender's word. The
    chunks are kept as they come and joined once, so the file is copied a
    single time (a growing buffer would be copied again and again).
    """
    parts: list[bytes] = []
    size = 0
    async for chunk in chunks:
        size += len(chunk)
        if size > limit:
            return None
        parts.append(chunk)
    return b"".join(parts)


# ── Concurrency ─────────────────────────────────────────────────────────────

_in_flight: dict[str, int] = {}


@contextmanager
def transfer_slot(consumer: str, limits: Limits):
    """Hold one of the few transfer slots, or refuse with 429.

    A transfer keeps its whole file in memory, so the count bounds what the
    artifacts can take from the process (``limits.transfers``: fewer as the
    size limit grows), and the per-key share keeps one key from taking every
    slot.
    """
    if (
        sum(_in_flight.values()) >= limits.transfers
        or _in_flight.get(consumer, 0) >= limits.transfers_per_key
    ):
        raise ArtifactError(429, "Too many artifact transfers are in flight; try again shortly")
    _in_flight[consumer] = _in_flight.get(consumer, 0) + 1
    try:
        yield
    finally:
        left = _in_flight[consumer] - 1
        if left:
            _in_flight[consumer] = left
        else:
            del _in_flight[consumer]


# pg_advisory_xact_lock key of the stage-an-artifact critical section ("mcpartf1").
_STORE_LOCK_KEY = 0x6D_63_70_61_72_74_66_31
# One lock per event loop: an asyncio.Lock belongs to the loop that first
# waits on it (each test runs its own).
_store_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


@asynccontextmanager
async def _storing(db: AsyncSession):
    """One staging at a time between "is there room?" and the INSERT.

    An asyncio lock within this process and, on Postgres, a transaction-level
    advisory lock across processes (the other blue/green color). The advisory
    lock goes with the transaction, so the block must end in a commit or a
    rollback.
    """
    async with _store_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock()):
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STORE_LOCK_KEY})
        yield


# ── Staging ─────────────────────────────────────────────────────────────────


async def _held_bytes(db: AsyncSession, now: datetime, consumer: str | None = None) -> int:
    query = select(func.coalesce(func.sum(McpArtifact.size), 0)).where(McpArtifact.expires_at > now)
    if consumer is not None:
        query = query.where(McpArtifact.consumer == consumer)
    return int((await db.execute(query)).scalar_one())


async def stage(
    db: AsyncSession,
    *,
    consumer: str,
    source: str,
    filename: str,
    content_type: str,
    content: bytes,
    limits: Limits,
) -> tuple[Artifact, bool]:
    """Keep ``content`` for ``consumer``; the artifact and whether it was already staged.

    The size and checksum are those of ``content`` as it is here. A key that
    stages bytes it already holds under the same name and type gets that
    artifact back with a fresh lifetime instead of a second copy.
    """
    size = len(content)
    if size > limits.max_bytes:
        raise too_large(limits)
    digest = await sha256_hex(content)
    await purge_expired(db)
    now = utcnow()
    expires_at = now + timedelta(seconds=limits.ttl_seconds)
    refusal: ArtifactError | None = None
    async with _storing(db):
        try:
            held = (
                await db.execute(
                    select(*_INFO_COLUMNS)
                    .where(
                        McpArtifact.consumer == consumer,
                        McpArtifact.sha256 == digest,
                        McpArtifact.size == size,
                        McpArtifact.filename == filename,
                        McpArtifact.content_type == content_type,
                        McpArtifact.expires_at > now,
                    )
                    .order_by(McpArtifact.created_at)
                    .limit(1)
                )
            ).first()
            if held is not None:
                # Deleting and purging do not wait for the lock: the row counts
                # only if it is still there to be renewed.
                renewed = await db.execute(
                    update(McpArtifact)
                    .where(McpArtifact.id == held.id, McpArtifact.expires_at > now)
                    .values(expires_at=expires_at)
                )
                if renewed.rowcount:
                    await db.commit()
                    return _artifact(held, expires_at=expires_at), True
            if await _held_bytes(db, now, consumer) + size > limits.key_bytes:
                refusal = ArtifactError(
                    429,
                    "This API key already holds as many staged artifact bytes as it may; "
                    "delete some or wait for them to expire",
                )
            elif await _held_bytes(db, now) + size > limits.total_bytes:
                refusal = ArtifactError(
                    503, "The artifact staging space is full; try again in a few minutes"
                )
            else:
                row = McpArtifact(
                    id=uuid.uuid4().hex,
                    consumer=consumer,
                    source=source,
                    filename=filename,
                    content_type=content_type,
                    size=size,
                    sha256=digest,
                    content=content,
                    created_at=now,
                    expires_at=expires_at,
                )
                artifact = _artifact(row)
                db.add(row)
                await db.commit()
        finally:
            # Ends the transaction on every path that did not commit, and with
            # it the advisory lock other stagings wait on.
            await db.rollback()
    if refusal is not None:
        raise refusal
    logger.info(
        "MCP artifact staged: id=%s consumer=%s source=%s size=%s sha256=%s",
        artifact.id,
        consumer,
        source,
        size,
        digest,
    )
    return artifact, False


def too_large(limits: Limits) -> ArtifactError:
    return ArtifactError(
        413, f"Larger than one artifact may be ({limits.max_bytes // MIB} MiB)"
    )


# ── Reading back ────────────────────────────────────────────────────────────


async def read(db: AsyncSession, consumer: str, artifact_id: object) -> tuple[Artifact, bytes]:
    """``consumer``'s artifact and its bytes, checked against the stored size and checksum."""
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
        raise not_found()
    row = (
        await db.execute(
            select(*_INFO_COLUMNS, McpArtifact.content).where(
                McpArtifact.id == artifact_id,
                McpArtifact.consumer == consumer,
                McpArtifact.expires_at > utcnow(),
            )
        )
    ).first()
    if row is None:
        raise not_found()
    content = bytes(row.content)
    if len(content) != row.size or await sha256_hex(content) != row.sha256:
        logger.error("MCP artifact %s no longer matches its stored size and checksum", row.id)
        raise ArtifactError(500, "The artifact failed its integrity check")
    return _artifact(row), content


async def delete(db: AsyncSession, consumer: str, artifact_id: object) -> bool:
    """Drop ``consumer``'s artifact before it expires; False when there is none."""
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
        return False
    result = await db.execute(
        sa_delete(McpArtifact).where(
            McpArtifact.id == artifact_id,
            McpArtifact.consumer == consumer,
            McpArtifact.expires_at > utcnow(),
        )
    )
    await db.commit()
    return bool(result.rowcount)


# ── Expiry ──────────────────────────────────────────────────────────────────


async def purge_expired(db: AsyncSession) -> int:
    result = await db.execute(sa_delete(McpArtifact).where(McpArtifact.expires_at <= utcnow()))
    await db.commit()
    return result.rowcount or 0


async def run_purge_loop(
    *,
    interval_seconds: int = PURGE_INTERVAL_SECONDS,
    first_delay_seconds: int = PURGE_FIRST_DELAY_SECONDS,
) -> None:
    """Background loop: delete artifacts soon after they expire.

    Staging purges too, but a surface nobody is using would otherwise keep its
    last artifacts until the next one. Only the active blue/green color runs
    it (both share the table), also while the tools are switched off. Every
    failure is swallowed: the next pass picks the rows up.
    """
    from app.database import async_session

    logger.info("MCP artifact purge loop started")
    await asyncio.sleep(first_delay_seconds)
    while True:
        try:
            if await is_active_instance():
                async with async_session() as db:
                    removed = await purge_expired(db)
                if removed:
                    logger.info("MCP artifacts: %d expired artifact(s) deleted", removed)
        except Exception:
            logger.exception("MCP artifact purge pass failed")
        await asyncio.sleep(interval_seconds)
