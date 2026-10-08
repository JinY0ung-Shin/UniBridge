"""Log out the Bifrost sessions UniBridge handed to browsers, once they are due.

Bifrost OSS keeps an admin session for 30 days and offers no setting to shorten
it, so a session from the sign-in handoff (app/routers/bifrost_sso.py) would
outlive the admin's UniBridge rights by weeks. The handoff records each session
it obtains in ``bifrost_sso_sessions``; this loop logs it out at Bifrost once
``expires_at`` (BIFROST_SSO_SESSION_HOURS after sign-in) has passed and deletes
the row when Bifrost no longer accepts the token. A session that cannot be
confirmed logged out (Bifrost down, an unexpected answer) stays for the next
pass.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db_types import utcnow
from app.models import BifrostSsoSession
from app.services.active_color import is_active_instance
from app.services.connection_manager import decrypt_password, encrypt_password

logger = logging.getLogger(__name__)

REVOKE_INTERVAL_SECONDS = 300
REVOKE_FIRST_DELAY_SECONDS = 60
_BIFROST_TIMEOUT_SECONDS = 10.0


def _bifrost_url(path: str) -> str:
    return f"{settings.BIFROST_URL.rstrip('/')}{path}"


def _session_headers(token: str) -> dict[str, str]:
    # The UI's https origin, as the browser would send it through the UI nginx.
    return {"Cookie": f"token={token}", "X-Forwarded-Proto": "https"}


async def record_session(db: AsyncSession, *, actor: str, token: str) -> datetime:
    """Store a session handed to ``actor``; returns when it is due to be logged out."""
    expires_at = utcnow() + timedelta(hours=settings.BIFROST_SSO_SESSION_HOURS)
    db.add(
        BifrostSsoSession(
            actor=actor, token_encrypted=encrypt_password(token), expires_at=expires_at
        )
    )
    await db.commit()
    return expires_at


async def log_out(token: str) -> bool:
    """Log ``token`` out at Bifrost; True once Bifrost no longer accepts it.

    Bifrost answers 200 to a logout even without a token, so that answer proves
    nothing: the token is checked again afterwards.
    """
    headers = _session_headers(token)
    try:
        async with httpx.AsyncClient(timeout=_BIFROST_TIMEOUT_SECONDS) as client:
            await client.post(_bifrost_url("/api/session/logout"), headers=headers)
            check = await client.get(_bifrost_url("/api/session/is-auth-enabled"), headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Bifrost session logout: Bifrost is unreachable: %s", exc)
        return False
    if check.status_code != 200:
        logger.warning("Bifrost session logout: the session check answered HTTP %s", check.status_code)
        return False
    try:
        return check.json().get("has_valid_token") is False
    except ValueError:
        logger.warning("Bifrost session logout: the session check did not answer JSON")
        return False


async def revoke_due_sessions(db: AsyncSession) -> int:
    """Log out every recorded session past ``expires_at``; returns how many went."""
    due = (
        await db.execute(
            select(BifrostSsoSession)
            .where(BifrostSsoSession.expires_at <= utcnow())
            .order_by(BifrostSsoSession.id)
        )
    ).scalars().all()
    removed = 0
    for session in due:
        try:
            token = decrypt_password(session.token_encrypted)
        except ValueError:
            # Encrypted under another ENCRYPTION_KEY: this service can never log
            # it out, and keeping the row would retry it forever. Bifrost drops
            # it on its own 30 days after the sign-in.
            logger.error(
                "Bifrost session %s for %s cannot be decrypted (ENCRYPTION_KEY changed?); "
                "it stays valid at Bifrost until Bifrost expires it",
                session.id,
                session.actor,
            )
            await db.delete(session)
            removed += 1
            continue
        if await log_out(token):
            await db.delete(session)
            removed += 1
            logger.info("Bifrost session for %s logged out", session.actor)
    await db.commit()
    return removed


async def run_revoke_once() -> int:
    """One pass against the meta database."""
    from app.database import async_session

    async with async_session() as db:
        return await revoke_due_sessions(db)


async def run_revoke_loop(
    *,
    interval_seconds: int = REVOKE_INTERVAL_SECONDS,
    first_delay_seconds: int = REVOKE_FIRST_DELAY_SECONDS,
) -> None:
    """Background loop: a pass shortly after boot, then every 5 minutes.

    Only the active blue/green color runs it: both colors share one meta DB and
    one Bifrost. Every failure is swallowed, so a transient Bifrost or database
    problem only moves a logout to the next pass.
    """
    logger.info("Bifrost session logout loop started")
    await asyncio.sleep(first_delay_seconds)
    while True:
        try:
            if await is_active_instance():
                await run_revoke_once()
        except Exception:
            logger.exception("Bifrost session logout pass failed")
        await asyncio.sleep(interval_seconds)
