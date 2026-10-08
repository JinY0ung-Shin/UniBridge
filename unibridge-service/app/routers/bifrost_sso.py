"""Open the Bifrost UI signed in, for UniBridge admins (BIFROST_UI_HOSTNAME only).

Bifrost OSS has a single admin login and no SSO, so UniBridge signs in for the
admin:

1. The LLM Monitoring page's Bifrost button calls ``POST
   /admin/bifrost/sso-handoff`` with the admin's own token. The service signs in
   to Bifrost with BIFROST_ADMIN_USERNAME/PASSWORD, records the session (see
   below) and keeps its token under a random one-time code for
   HANDOFF_TTL_SECONDS.
2. The page opens ``https://<BIFROST_UI_HOSTNAME>/_unibridge/sso?code=…`` in a
   new tab. The UI nginx's Bifrost server (nginx-bifrost-ui.conf) forwards it to
   ``GET /bifrost/sso``, which redeems the code once, sets the session cookie on
   the Bifrost host and sends the browser to /workspace. Bifrost's UI sees a
   valid session and skips its login form.

Bifrost would keep such a session for 30 days, well past the admin's UniBridge
rights, so the handoff records each one and app/services/bifrost_sessions.py
logs it out BIFROST_SSO_SESSION_HOURS after the sign-in; the browser cookie
expires at the same time. Every admin shares the one Bifrost admin account, so
each sign-in is written to the admin audit log. Redeeming answers only requests
for BIFROST_UI_HOSTNAME: the cookie never lands on UniBridge's own host, where
it would also replace the LiteLLM admin UI's cookie of the same name (cookies
are not separated by port). Codes live in this process; the page and the
redeem request reach the same service through the same UI color. Bifrost's own
password login stays available.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, get_current_user
from app.config import settings
from app.database import get_db
from app.db_types import utcnow
from app.services.audit import log_admin_action
from app.services.bifrost_sessions import log_out, record_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Bifrost"])

HANDOFF_TTL_SECONDS = 60
# Unredeemed codes beyond this many are dropped, oldest first.
MAX_PENDING_HANDOFFS = 100
_BIFROST_LOGIN_TIMEOUT_SECONDS = 10.0
_NO_STORE = {"Cache-Control": "no-store"}
_EXPIRED_PAGE = """<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><title>Bifrost sign-in</title></head>
<body>
<p>이 Bifrost 로그인 링크는 만료되었거나 이미 사용되었습니다. UniBridge의 LLM 모니터링에서 Bifrost 관리 버튼을 다시 눌러 주세요.</p>
<p>This Bifrost sign-in link has expired or has already been used. Open Bifrost again from UniBridge: LLM Monitoring, Bifrost Admin.</p>
</body>
</html>
"""


@dataclass(frozen=True)
class _Handoff:
    token: str
    session_expires_at: datetime
    expires_at: float  # the code's own deadline, time.monotonic()


_pending: dict[str, _Handoff] = {}


def sso_configured() -> bool:
    return bool(settings.BIFROST_UI_HOSTNAME and settings.BIFROST_ADMIN_PASSWORD)


def _redeem_code(code: str) -> _Handoff | None:
    """Spend ``code``: its handoff, or None when it is unknown, used or expired."""
    handoff = _pending.pop(code, None)
    if handoff is None or handoff.expires_at <= time.monotonic():
        return None
    return handoff


def _drop_stale(now: float) -> None:
    for code in [code for code, handoff in _pending.items() if handoff.expires_at <= now]:
        del _pending[code]
    while len(_pending) >= MAX_PENDING_HANDOFFS:
        del _pending[next(iter(_pending))]


async def _sign_in_to_bifrost() -> str:
    """Sign in as the Bifrost admin and return the session token."""
    url = f"{settings.BIFROST_URL.rstrip('/')}/api/session/login"
    try:
        async with httpx.AsyncClient(timeout=_BIFROST_LOGIN_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                json={
                    "username": settings.BIFROST_ADMIN_USERNAME,
                    "password": settings.BIFROST_ADMIN_PASSWORD,
                },
                # As the browser reaches it: over https, through the UI nginx.
                headers={"X-Forwarded-Proto": "https"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Bifrost sign-in: %s is unreachable: %s", url, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Bifrost is unreachable") from exc
    if response.status_code != 200:
        logger.warning(
            "Bifrost sign-in: Bifrost refused the admin login (HTTP %s)", response.status_code
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Bifrost refused the admin login; check BIFROST_ADMIN_USERNAME and "
            "BIFROST_ADMIN_PASSWORD",
        )
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        token = rest.split(";", 1)[0].strip()
        if name.strip() == "token" and token:
            return token
    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY, "Bifrost answered the login without a session cookie"
    )


@router.post("/admin/bifrost/sso-handoff")
async def create_bifrost_sso_handoff(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | int]:
    """One-time code that opens the Bifrost UI signed in (admins only)."""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can open the Bifrost UI")
    if not sso_configured():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Bifrost sign-in through UniBridge needs BIFROST_UI_HOSTNAME and "
            "BIFROST_ADMIN_PASSWORD",
        )
    token = await _sign_in_to_bifrost()
    try:
        session_expires_at = await record_session(db, actor=user.username, token=token)
    except Exception as exc:
        # Unrecorded means never logged out by this service: do not hand it out.
        logger.exception("Bifrost sign-in: could not record the session; logging it out")
        await log_out(token)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not record the Bifrost session"
        ) from exc
    now = time.monotonic()
    _drop_stale(now)
    code = secrets.token_urlsafe(32)
    _pending[code] = _Handoff(
        token=token, session_expires_at=session_expires_at, expires_at=now + HANDOFF_TTL_SECONDS
    )
    await log_admin_action(
        db,
        actor=user.username,
        action="create",
        resource_type="bifrost_session",
        resource_id=settings.BIFROST_ADMIN_USERNAME,
        summary=(
            "Signed in to Bifrost as its admin through UniBridge; the session is logged "
            f"out after {settings.BIFROST_SSO_SESSION_HOURS} h"
        ),
    )
    return {"code": code, "expires_in": HANDOFF_TTL_SECONDS}


@router.get("/bifrost/sso", include_in_schema=False)
async def redeem_bifrost_sso_handoff(
    request: Request,
    code: str = Query("", max_length=128),
) -> Response:
    """Trade a one-time code for the Bifrost session cookie, on the Bifrost host only."""
    host = request.headers.get("host", "").rsplit(":", 1)[0].lower()
    if not sso_configured() or host != settings.BIFROST_UI_HOSTNAME.lower():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # The code is the credential here: the browser arrives from the Bifrost host
    # with no UniBridge token (tests/test_route_auth_guard.py HANDLER_AUTH_ROUTES).
    handoff = _redeem_code(code)
    if handoff is None:
        return HTMLResponse(_EXPIRED_PAGE, status_code=status.HTTP_410_GONE, headers=_NO_STORE)
    response = RedirectResponse(
        "/workspace", status_code=status.HTTP_303_SEE_OTHER, headers=_NO_STORE
    )
    # Bifrost's own cookie would last 30 days; this one ends with the session.
    remaining = int((handoff.session_expires_at - utcnow()).total_seconds())
    response.set_cookie(
        "token",
        handoff.token,
        max_age=max(remaining, 1),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response
