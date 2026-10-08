"""Bifrost UI sign-in through UniBridge (app/routers/bifrost_sso.py) and the
logout of the sessions it hands out (app/services/bifrost_sessions.py)."""
from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db_types import utcnow
from app.models import AdminAuditLog, BifrostSsoSession
from app.routers import bifrost_sso
from app.services import bifrost_sessions
from app.services.connection_manager import decrypt_password, encrypt_password

HOST = "llm-proxy.example.com"
SESSION_SECONDS = 8 * 3600


@pytest.fixture(autouse=True)
def _no_pending_codes():
    bifrost_sso._pending.clear()
    yield
    bifrost_sso._pending.clear()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "BIFROST_UI_HOSTNAME", HOST)
    monkeypatch.setattr(settings, "BIFROST_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "BIFROST_ADMIN_PASSWORD", "bifrost-admin-pass")
    monkeypatch.setattr(settings, "BIFROST_URL", "http://bifrost.test:8080/")
    monkeypatch.setattr(settings, "BIFROST_SSO_SESSION_HOURS", 8)


class FakeBifrost:
    """Bifrost's session API: login mints tokens, logout revokes the one it gets.

    ``login_response`` replaces the login answer, ``error`` makes every request
    fail, and ``ignore_logout`` keeps tokens valid through a logout.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.valid: set[str] = set()
        self.issued = 0
        self.login_response: httpx.Response | None = None
        self.error: Exception | None = None
        self.ignore_logout = False

    @staticmethod
    def _token(request: httpx.Request) -> str | None:
        match = re.search(r"(?:^|;\s*)token=([^;]+)", request.headers.get("cookie", ""))
        return match.group(1) if match else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        path = request.url.path
        if path == "/api/session/login":
            if self.login_response is not None:
                return self.login_response
            self.issued += 1
            token = f"bifrost-session-{self.issued}"
            self.valid.add(token)
            cookie = (
                f"token={token}; expires=Sat, 07 Nov 2026 07:05:24 GMT; path=/; HttpOnly; "
                "secure; SameSite=Lax"
            )
            return httpx.Response(
                200, json={"message": "Login successful"}, headers=[("set-cookie", cookie)]
            )
        if path == "/api/session/logout":
            if not self.ignore_logout:
                self.valid.discard(self._token(request))
            return httpx.Response(200, json={"message": "Logout successful"})
        if path == "/api/session/is-auth-enabled":
            return httpx.Response(
                200,
                json={
                    "auth_type": "password",
                    "has_valid_token": self._token(request) in self.valid,
                    "is_auth_enabled": True,
                },
            )
        return httpx.Response(404)

    def calls(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]


@pytest.fixture
def bifrost(monkeypatch):
    fake = FakeBifrost()
    real_client = httpx.AsyncClient

    def client_with_fake_bifrost(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(fake.handle), **kwargs)

    monkeypatch.setattr(bifrost_sso.httpx, "AsyncClient", client_with_fake_bifrost)
    return fake


@pytest.fixture
def db_factory(seeded_db):
    return async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)


async def _handoff(client, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.post("/admin/bifrost/sso-handoff", headers=headers)


async def _redeem(client, code: str, host: str = HOST):
    return await client.get("/bifrost/sso", params={"code": code}, headers={"host": host})


async def _sessions(db_factory) -> list[BifrostSsoSession]:
    async with db_factory() as db:
        return list((await db.execute(select(BifrostSsoSession))).scalars().all())


def _cookie_attributes(set_cookie: str) -> dict[str, str]:
    parts = [part.strip() for part in set_cookie.split(";")]
    attributes = {}
    for part in parts[1:]:
        name, _, value = part.partition("=")
        attributes[name.lower()] = value
    return attributes


async def test_an_admin_code_signs_the_browser_in_to_bifrost(
    client, admin_token, configured, bifrost, db_factory
):
    resp = await _handoff(client, admin_token)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expires_in"] == bifrost_sso.HANDOFF_TTL_SECONDS
    assert len(body["code"]) >= 40

    # The service signed in to Bifrost as its admin, as an https request.
    [login] = bifrost.calls("/api/session/login")
    assert login.method == "POST"
    assert str(login.url) == "http://bifrost.test:8080/api/session/login"
    assert json.loads(login.content) == {"username": "admin", "password": "bifrost-admin-pass"}
    assert login.headers["x-forwarded-proto"] == "https"

    redeemed = await _redeem(client, body["code"])
    assert redeemed.status_code == 303
    assert redeemed.headers["location"] == "/workspace"
    assert redeemed.headers["cache-control"] == "no-store"
    [set_cookie] = redeemed.headers.get_list("set-cookie")
    assert set_cookie.startswith("token=bifrost-session-1;")
    # UniBridge's own attributes: the cookie ends with the recorded session,
    # not after Bifrost's 30 days, and stays host-only, Secure and HttpOnly.
    attributes = _cookie_attributes(set_cookie)
    assert SESSION_SECONDS - 60 <= int(attributes["max-age"]) <= SESSION_SECONDS
    assert attributes["path"] == "/"
    assert {"secure", "httponly"} <= set(attributes)
    assert attributes["samesite"].lower() == "lax"
    assert "domain" not in attributes
    assert "Nov 2026" not in set_cookie

    # Recorded for the logout loop, encrypted, due 8 hours after the sign-in.
    [session] = await _sessions(db_factory)
    assert session.actor == "testadmin"
    assert "bifrost-session-1" not in session.token_encrypted
    assert decrypt_password(session.token_encrypted) == "bifrost-session-1"
    due_in = (session.expires_at - utcnow()).total_seconds()
    assert SESSION_SECONDS - 60 <= due_in <= SESSION_SECONDS

    async with db_factory() as db:
        logs = (await db.execute(select(AdminAuditLog))).scalars().all()
    [log] = [log for log in logs if log.resource_type == "bifrost_session"]
    assert (log.actor, log.action, log.resource_id) == ("testadmin", "create", "admin")
    assert "logged out after 8 h" in log.summary


async def test_a_code_works_once(client, admin_token, configured, bifrost):
    code = (await _handoff(client, admin_token)).json()["code"]

    assert (await _redeem(client, code)).status_code == 303
    again = await _redeem(client, code)
    assert again.status_code == 410
    assert "set-cookie" not in again.headers
    assert "expired or has already been used" in again.text


async def test_an_expired_code_is_refused(client, admin_token, configured, bifrost, monkeypatch):
    code = (await _handoff(client, admin_token)).json()["code"]
    deadline = bifrost_sso._pending[code].expires_at
    monkeypatch.setattr(bifrost_sso.time, "monotonic", lambda: deadline)

    resp = await _redeem(client, code)
    assert resp.status_code == 410
    assert "set-cookie" not in resp.headers


@pytest.mark.parametrize("host", ["10.0.0.5", "unibridge.example.com", "test"])
async def test_codes_are_redeemed_only_on_the_bifrost_host(
    client, admin_token, configured, bifrost, host
):
    # Anywhere else (e.g. /_api/bifrost/sso on UniBridge's own host) the cookie
    # would land on the wrong host, next to the LiteLLM admin UI's.
    code = (await _handoff(client, admin_token)).json()["code"]

    wrong = await _redeem(client, code, host=host)
    assert wrong.status_code == 404
    assert "set-cookie" not in wrong.headers
    # The code survives a request for another host.
    assert (await _redeem(client, code, host=f"{HOST.upper()}:3000")).status_code == 303


async def test_only_admins_get_a_code(client, user_token, configured, bifrost, db_factory):
    assert (await _handoff(client, user_token)).status_code == 403
    assert (await _handoff(client, None)).status_code == 401
    assert bifrost.requests == []
    assert bifrost_sso._pending == {}
    assert await _sessions(db_factory) == []


@pytest.mark.parametrize("missing", ["BIFROST_UI_HOSTNAME", "BIFROST_ADMIN_PASSWORD"])
async def test_without_hostname_or_password_it_is_off(
    client, admin_token, configured, bifrost, monkeypatch, missing
):
    monkeypatch.setattr(settings, missing, "")

    resp = await _handoff(client, admin_token)
    assert resp.status_code == 404
    assert missing in resp.json()["detail"]
    assert bifrost.requests == []
    assert (await _redeem(client, "anything")).status_code == 404


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx.Response(401, json={"error": "invalid credentials"}), "BIFROST_ADMIN_PASSWORD"),
        (httpx.Response(200, json={"message": "Login successful"}), "without a session cookie"),
        (
            httpx.Response(200, headers=[("set-cookie", "other=1; path=/")]),
            "without a session cookie",
        ),
        (httpx.Response(200, headers=[("set-cookie", "token=; path=/")]), "without a session cookie"),
        (httpx.ConnectError("connection refused"), "unreachable"),
    ],
)
async def test_a_failed_bifrost_login_is_a_502_without_a_code(
    client, admin_token, configured, bifrost, db_factory, answer, detail
):
    if isinstance(answer, Exception):
        bifrost.error = answer
    else:
        bifrost.login_response = answer

    resp = await _handoff(client, admin_token)
    assert resp.status_code == 502
    assert detail in resp.json()["detail"]
    assert bifrost_sso._pending == {}
    assert await _sessions(db_factory) == []


async def test_a_session_it_cannot_record_is_logged_out_not_handed_out(
    client, admin_token, configured, bifrost, monkeypatch
):
    async def broken_record(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bifrost_sso, "record_session", broken_record)

    resp = await _handoff(client, admin_token)
    assert resp.status_code == 503
    assert bifrost_sso._pending == {}
    # The token Bifrost issued was logged out again right away.
    [logout] = bifrost.calls("/api/session/logout")
    assert "token=bifrost-session-1" in logout.headers["cookie"]
    assert bifrost.valid == set()


async def test_unredeemed_codes_are_bounded(client, admin_token, configured, bifrost, monkeypatch):
    monkeypatch.setattr(bifrost_sso, "MAX_PENDING_HANDOFFS", 3)
    codes = [(await _handoff(client, admin_token)).json()["code"] for _ in range(5)]

    assert len(bifrost_sso._pending) <= 3
    assert (await _redeem(client, codes[0])).status_code == 410
    assert (await _redeem(client, codes[-1])).status_code == 303


# ── Logging the handed-out sessions out (app/services/bifrost_sessions.py) ──


async def _add_session(db_factory, token: str, *, due_in: timedelta, actor: str = "admin1"):
    async with db_factory() as db:
        db.add(
            BifrostSsoSession(
                actor=actor,
                token_encrypted=encrypt_password(token),
                expires_at=utcnow() + due_in,
            )
        )
        await db.commit()


async def _revoke(db_factory) -> int:
    async with db_factory() as db:
        return await bifrost_sessions.revoke_due_sessions(db)


async def test_due_sessions_are_logged_out_and_forgotten(configured, bifrost, db_factory):
    bifrost.valid |= {"due-token", "fresh-token"}
    await _add_session(db_factory, "due-token", due_in=timedelta(seconds=-5))
    await _add_session(db_factory, "fresh-token", due_in=timedelta(hours=1))

    assert await _revoke(db_factory) == 1

    assert bifrost.valid == {"fresh-token"}
    [logout] = bifrost.calls("/api/session/logout")
    assert logout.method == "POST"
    assert str(logout.url) == "http://bifrost.test:8080/api/session/logout"
    assert logout.headers["cookie"] == "token=due-token"
    assert [decrypt_password(s.token_encrypted) for s in await _sessions(db_factory)] == [
        "fresh-token"
    ]


async def test_a_session_bifrost_still_accepts_stays_for_the_next_pass(
    configured, bifrost, db_factory
):
    # Bifrost answers 200 to any logout, even one without a token, so only the
    # check afterwards counts.
    bifrost.valid.add("due-token")
    bifrost.ignore_logout = True
    await _add_session(db_factory, "due-token", due_in=timedelta(seconds=-5))

    assert await _revoke(db_factory) == 0
    assert len(await _sessions(db_factory)) == 1

    bifrost.ignore_logout = False
    assert await _revoke(db_factory) == 1
    assert await _sessions(db_factory) == []


async def test_an_unreachable_bifrost_keeps_due_sessions(configured, bifrost, db_factory):
    await _add_session(db_factory, "due-token", due_in=timedelta(seconds=-5))
    bifrost.error = httpx.ConnectError("connection refused")

    assert await _revoke(db_factory) == 0
    assert len(await _sessions(db_factory)) == 1


async def test_a_session_that_cannot_be_decrypted_is_dropped(
    configured, bifrost, db_factory, caplog
):
    async with db_factory() as db:
        db.add(
            BifrostSsoSession(
                actor="admin1", token_encrypted="not-a-fernet-token", expires_at=utcnow()
            )
        )
        await db.commit()

    assert await _revoke(db_factory) == 1
    assert await _sessions(db_factory) == []
    assert bifrost.requests == []
    assert "cannot be decrypted" in caplog.text


async def test_a_redeemed_session_is_logged_out_when_due(
    client, admin_token, configured, bifrost, db_factory
):
    code = (await _handoff(client, admin_token)).json()["code"]
    assert (await _redeem(client, code)).status_code == 303
    assert bifrost.valid == {"bifrost-session-1"}

    async with db_factory() as db:
        [session] = (await db.execute(select(BifrostSsoSession))).scalars().all()
        session.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()

    assert await _revoke(db_factory) == 1
    assert bifrost.valid == set()
