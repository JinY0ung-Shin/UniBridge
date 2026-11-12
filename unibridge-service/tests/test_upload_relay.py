"""Upload relay (app/routers/upload_relay.py, app/services/upload_relay.py).

The ticket and request-signature helpers below are written from the wire
contract, not imported from the app: the Noah deployment signs with its own
code, so these tests fail if the app's reading of the contract drifts.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import config as app_config
from app.config import settings
from app.db_types import utcnow
from app.models import AdminAuditLog, UploadRelayFile
from app.routers import upload_relay as relay_router
from app.services import upload_relay as relay
from tests.conftest import auth_header

SECRET = "upload-relay-test-secret-0123456789abcdef"
TID = "tid_0123456789abcdef"
PDF = b"%PDF-1.7 hello relay"

# Fixed vectors, computed once outside the app from the contract text.
VECTOR_TICKET = (
    "v1.eyJ0aWQiOiJ0aWRfMDEyMzQ1Njc4OWFiY2RlZiIsInVzZXIiOiJqaW55b3VuZyIsIm5hbWUiOiLsp4TsmIEiLCJleHAi"
    "OjQxMDI0NDQ4MDAsImFjY2VwdCI6WyIucGRmIiwiLnBuZyJdLCJtYXhCeXRlcyI6ODM4ODYwOCwibWF4RmlsZXMiOjV9"
    ".xGzRmUSq-tlGDDkwl7DaXxpf1lkM0zSSEGUJfgtfsgU"
)
VECTOR_REQUEST_SIGNATURE = "783d878dfbe1b9f994e90f80843c44f5d12548c77e8f71abc922efbd5ca4f9ad"


# ── Contract helpers (what the Noah server does) ────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_ticket(secret: str = SECRET, **overrides) -> str:
    payload = {
        "tid": TID,
        "user": "jinyoung",
        "name": "진영",
        "exp": int(time.time()) + 600,
        "accept": [".pdf", ".png"],
        "maxBytes": 8 * 1024 * 1024,
        "maxFiles": 5,
    }
    payload.update(overrides)
    payload = {key: value for key, value in payload.items() if value is not ...}
    part = _b64url(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(
        secret.encode("utf-8"), f"noah-upload-relay-ticket:v1.{part}".encode("utf-8"), hashlib.sha256
    ).digest()
    return f"v1.{part}.{_b64url(mac)}"


def noah_headers(method: str, path: str, *, secret: str = SECRET, at: float | None = None) -> dict[str, str]:
    timestamp = str(int(at if at is not None else time.time()))
    message = f"noah-upload-relay-request:v1\n{method}\n{path}\n{timestamp}"
    signature = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"X-Noah-Relay-Timestamp": timestamp, "X-Noah-Relay-Signature": signature}


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_RELAY_SECRET", SECRET)
    monkeypatch.setattr(relay_router, "_disabled_warning_logged", False)


@pytest.fixture
def db_factory(seeded_db):
    return async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def pending_token():
    """A signed-in account that no admin has approved yet (no app role)."""
    return jwt.encode(
        {"sub": "newcomer", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


async def _describe(client, token: str | None, ticket: str):
    headers = auth_header(token) if token else {}
    return await client.post("/upload-relay/ticket", json={"ticket": ticket}, headers=headers)


async def _upload(
    client,
    token: str | None,
    ticket: str,
    name: str = "report.pdf",
    content: bytes = PDF,
    *,
    via: str = "header",
):
    """Upload as the relay page does (ticket in the header), or with the
    ticket as a form field (``via="field"``, the fallback)."""
    headers = auth_header(token) if token else {}
    data = None
    if via == "header":
        headers["X-Upload-Relay-Ticket"] = ticket
    else:
        data = {"ticket": ticket}
    return await client.post(
        "/upload-relay/files",
        data=data,
        files={"file": (name, content, "application/octet-stream")},
        headers=headers,
    )


async def _noah(client, method: str, path: str, **kwargs):
    return await client.request(method, path, headers=noah_headers(method, path), **kwargs)


async def _audit(db_factory) -> list[AdminAuditLog]:
    async with db_factory() as db:
        rows = (
            await db.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.resource_type == "upload_relay")
                .order_by(AdminAuditLog.id)
            )
        ).scalars().all()
    return list(rows)


async def _stored(db_factory) -> list:
    async with db_factory() as db:
        return list((await db.execute(select(*relay.FILE_COLUMNS))).all())


# ── The contract itself ─────────────────────────────────────────────────────


def test_the_fixed_ticket_vector_verifies():
    ticket = relay.verify_ticket(VECTOR_TICKET)
    assert ticket == relay.RelayTicket(
        tid=TID,
        user="jinyoung",
        name="진영",
        exp=4102444800,
        accept=(".pdf", ".png"),
        max_bytes=8388608,
        max_files=5,
    )
    assert mint_ticket(exp=4102444800) == VECTOR_TICKET


def test_the_fixed_request_signature_vector_verifies():
    path = f"/upload-relay/tickets/{TID}/files"
    assert relay.request_signature("GET", path, "1700000000") == VECTOR_REQUEST_SIGNATURE
    assert relay.request_signature_valid(
        "GET", path, "1700000000", VECTOR_REQUEST_SIGNATURE, now=1700000000 + 300
    )
    assert not relay.request_signature_valid(
        "GET", path, "1700000000", VECTOR_REQUEST_SIGNATURE, now=1700000000 + 301
    )
    assert not relay.request_signature_valid(
        "POST", path, "1700000000", VECTOR_REQUEST_SIGNATURE, now=1700000000
    )


# ── Ticket verification ─────────────────────────────────────────────────────


async def test_a_good_ticket_is_described(client, user_token):
    resp = await _describe(client, user_token, mint_ticket(exp=4102444800))
    assert resp.status_code == 200
    body = resp.json()
    # Counted on this server's clock, for a page whose PC clock may be off.
    remaining = body.pop("expiresInSeconds")
    assert 0 <= 4102444800 - int(time.time()) - remaining <= 2
    assert body == {
        "user": "jinyoung",
        "name": "진영",
        "expiresAt": "2100-01-01T00:00:00Z",
        "accept": [".pdf", ".png"],
        "maxBytes": 8 * 1024 * 1024,
        "maxFiles": 5,
        "remainingFiles": 5,
    }


async def test_the_countdown_is_whole_seconds_left_on_the_ticket(client, user_token):
    resp = await _describe(client, user_token, mint_ticket(exp=int(time.time()) + 90))
    assert 87 <= resp.json()["expiresInSeconds"] <= 90


async def test_the_server_cap_bounds_what_a_ticket_allows(client, user_token, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_RELAY_MAX_FILE_BYTES", 1024)
    resp = await _describe(client, user_token, mint_ticket(name=...))
    assert resp.json()["maxBytes"] == 1024
    assert resp.json()["name"] is None


async def test_only_an_approved_account_may_use_the_relay(
    client, user_token, admin_token, pending_token, db_factory, caplog
):
    """Signing in is not enough: the account needs an app role, like every
    other UniBridge endpoint. Which role does not matter."""
    ticket = mint_ticket()
    assert (await _describe(client, None, ticket)).status_code == 401
    assert (await _upload(client, None, ticket)).status_code == 401
    assert (await _describe(client, "not-a-jwt", ticket)).status_code == 401

    # An account still waiting for approval: the gate's usual 401, on both
    # endpoints and both ways of sending the ticket. Nothing is stored, and
    # nothing is audited or even logged by the relay (the gate turned it away).
    with caplog.at_level(logging.INFO, logger=relay_router.__name__):
        assert (await _describe(client, pending_token, ticket)).status_code == 401
        for via in ("header", "field"):
            refused = await _upload(client, pending_token, ticket, via=via)
            assert refused.status_code == 401
            assert refused.json() == {"detail": "Invalid token: missing subject or role"}
    assert await _stored(db_factory) == []
    assert await _audit(db_factory) == []
    assert [r for r in caplog.records if r.name == relay_router.__name__] == []

    # Any app role will do.
    for token, username in ((user_token, "testuser"), (admin_token, "testadmin")):
        assert (await _describe(client, token, ticket)).status_code == 200
        assert (await _upload(client, token, ticket, name=f"{username}.pdf")).status_code == 201
    assert sorted(row.uploader for row in await _stored(db_factory)) == ["testadmin", "testuser"]
    assert [log.actor for log in await _audit(db_factory)] == ["testuser", "testadmin"]


async def test_the_login_is_checked_before_the_ticket_and_the_body(client, pending_token):
    """Auth first: without an approved login even a perfectly good ticket is
    not looked at, and the body is never read."""
    body, headers = _multipart(None, b"x" * 200_000)
    for auth in ({}, auth_header(pending_token)):
        stream = _CountedBody(body)
        resp = await client.post(
            "/upload-relay/files",
            content=stream,
            headers={**headers, **auth, "X-Upload-Relay-Ticket": mint_ticket()},
        )
        assert resp.status_code == 401
        assert stream.taken == 0


@pytest.mark.parametrize(
    "ticket",
    [
        "",
        "v1.only-two",
        "v2." + mint_ticket().split(".", 1)[1],
        mint_ticket(secret="another-secret-another-secret-12345"),
        mint_ticket()[:-2] + "AA",
        mint_ticket(tid="short"),
        mint_ticket(tid="has space 0123456789"),
        mint_ticket(user=""),
        mint_ticket(user="u" * 201),
        mint_ticket(name=7),
        mint_ticket(exp="soon"),
        mint_ticket(exp=True),
        mint_ticket(exp=10**12),
        mint_ticket(user="nul\x00user"),
        mint_ticket(name="line\nbreak"),
        mint_ticket(accept=[]),
        mint_ticket(accept=[".PDF"]),
        mint_ticket(accept=["pdf"]),
        mint_ticket(accept=[".pdf"] * 65),
        mint_ticket(maxBytes=0),
        mint_ticket(maxFiles=0),
        mint_ticket(maxFiles=21),
        mint_ticket(tid=...),
        "x" * 9000,
    ],
)
async def test_malformed_or_wrongly_signed_tickets_are_400(client, user_token, ticket):
    resp = await _describe(client, user_token, ticket)
    assert resp.status_code in (400, 422)  # 422: longer than the field allows
    if resp.status_code == 400:
        assert resp.json() == {"detail": "Invalid upload ticket"}
    with pytest.raises(relay.TicketError) as raised:
        relay.verify_ticket(ticket)
    assert raised.value.status_code == 400
    assert raised.value.tid is None


def _signed(raw_payload: bytes) -> str:
    """A correctly signed ticket around any payload bytes (a faulty Noah)."""
    part = _b64url(raw_payload)
    mac = hmac.new(SECRET.encode(), f"noah-upload-relay-ticket:v1.{part}".encode(), hashlib.sha256).digest()
    return f"v1.{part}.{_b64url(mac)}"


@pytest.mark.parametrize("raw_payload", [b"not json", b"\xff\xfe", b'["a", "list"]', b'"text"'])
def test_a_correctly_signed_ticket_with_a_faulty_payload_is_still_refused(raw_payload):
    with pytest.raises(relay.TicketError) as raised:
        relay.verify_ticket(_signed(raw_payload))
    assert (raised.value.status_code, raised.value.tid) == (400, None)


@pytest.mark.parametrize("field", ["user", "name"])
async def test_half_a_surrogate_pair_in_a_name_is_400_not_500(client, user_token, field):
    """A display name cut in the middle of an emoji: JSON can carry the lone
    half, UTF-8 cannot, so it used to fail while the answer was written."""
    payload = {"tid": TID, "user": "jin", "name": "Jin", "exp": int(time.time()) + 600,
               "accept": [".pdf"], "maxBytes": 1024, "maxFiles": 1}
    payload[field] = "진영 \ud83d"
    ticket = _signed(json.dumps(payload, ensure_ascii=True).encode("ascii"))

    described = await _describe(client, user_token, ticket)
    assert described.status_code == 400
    assert described.json() == {"detail": "Invalid upload ticket"}
    assert (await _upload(client, user_token, ticket)).status_code == 400
    assert (await _upload(client, user_token, ticket, via="field")).status_code == 400

    # The whole pair is a perfectly good name.
    payload[field] = "진영 \U0001f600"
    whole = _signed(json.dumps(payload, ensure_ascii=True).encode("ascii"))
    assert (await _describe(client, user_token, whole)).status_code == 200


async def test_a_payload_swapped_under_a_valid_signature_is_refused(client, user_token):
    good = mint_ticket()
    other = mint_ticket(user="someone-else")
    forged = ".".join(["v1", other.split(".")[1], good.split(".")[2]])
    assert (await _describe(client, user_token, forged)).status_code == 400


async def test_an_expired_ticket_is_410(client, user_token):
    resp = await _describe(client, user_token, mint_ticket(exp=int(time.time()) - 1))
    assert resp.status_code == 410
    with pytest.raises(relay.TicketError) as raised:
        relay.verify_ticket(mint_ticket(exp=1000), now=1000)
    assert (raised.value.status_code, raised.value.tid) == (410, TID)


# ── Upload ──────────────────────────────────────────────────────────────────


async def test_an_upload_is_stored_and_audited(client, user_token, db_factory, caplog):
    ticket = mint_ticket()
    with caplog.at_level(logging.INFO):
        resp = await _upload(client, user_token, ticket, name="분기 보고서.PDF")
    assert resp.status_code == 201
    body = resp.json()
    assert body == {
        "id": body["id"],
        "name": "분기 보고서.PDF",
        "size": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }

    [row] = await _stored(db_factory)
    assert (row.id, row.ticket_id, row.noah_user, row.noah_name) == (body["id"], TID, "jinyoung", "진영")
    assert (row.uploader, row.filename, row.size) == ("testuser", "분기 보고서.PDF", len(PDF))
    ttl = (row.expires_at - row.created_at).total_seconds()
    assert ttl == settings.UPLOAD_RELAY_FILE_TTL_SECONDS == 600

    [log] = await _audit(db_factory)
    assert (log.actor, log.action, log.resource_id, log.status) == ("testuser", "upload", body["id"], "success")
    assert "분기 보고서.PDF" in log.summary and "jinyoung" in log.summary and str(len(PDF)) in log.summary
    assert json.loads(log.after) == {
        "ticket_id": TID,
        "noah_user": "jinyoung",
        "filename": "분기 보고서.PDF",
        "size": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
        "uploader": "testuser",
    }

    described = await _describe(client, user_token, ticket)
    assert described.json()["remainingFiles"] == 4

    # The service log names the file, never the ticket or the secret.
    text = caplog.text
    assert "분기 보고서.PDF" in text and hashlib.sha256(PDF).hexdigest() in text
    assert ticket not in text and ticket.split(".")[2] not in text and SECRET not in text


async def test_only_the_last_path_component_of_a_name_is_kept(client, user_token):
    resp = await _upload(client, user_token, mint_ticket(), name="C:\\Users\\me\\..\\secret/plan.pdf")
    assert resp.status_code == 201
    assert resp.json()["name"] == "plan.pdf"


@pytest.mark.parametrize(
    ("name", "content", "ticket_overrides", "status", "detail"),
    [
        ("notes.txt", PDF, {}, 400, "This type of file is not accepted here"),
        ("report", PDF, {}, 400, "This type of file is not accepted here"),
        ("report.pdf", b"", {}, 400, "The file is empty"),
        ("re\u202eport.pdf", PDF, {}, 400, "The file name is not usable"),
        ("a" * 252 + ".pdf", PDF, {}, 400, "The file name is not usable"),
        ("report.pdf", PDF, {"maxBytes": len(PDF) - 1}, 413, "The file is larger than this upload allows"),
    ],
)
async def test_refused_uploads_store_nothing_and_are_audited(
    client, user_token, db_factory, name, content, ticket_overrides, status, detail
):
    resp = await _upload(client, user_token, mint_ticket(**ticket_overrides), name=name, content=content)
    assert resp.status_code == status
    assert resp.json() == {"detail": detail}
    assert await _stored(db_factory) == []

    [log] = await _audit(db_factory)
    assert (log.actor, log.action, log.resource_id, log.status) == ("testuser", "upload", TID, "error")
    assert log.error_message == detail
    assert json.loads(log.after)["noah_user"] == "jinyoung"


@pytest.mark.parametrize("via", ["header", "field"])
async def test_an_upload_without_a_valid_ticket_is_logged_but_never_audited(
    client, user_token, db_factory, caplog, via
):
    """Every approved account can send these as often as it likes, holding no
    ticket at all: they leave a warning in the service log and no audit row."""
    forged = mint_ticket(secret="another-secret-another-secret-12345", user="victim")
    expired = mint_ticket(exp=int(time.time()) - 5)
    with caplog.at_level(logging.WARNING, logger=relay_router.__name__):
        assert (await _upload(client, user_token, forged, via=via)).status_code == 400
        assert (await _upload(client, user_token, expired, via=via)).status_code == 410
        for _ in range(5):
            flood = await client.post(
                "/upload-relay/files",
                content=b"x",
                headers={**auth_header(user_token), "content-type": "text/plain"},
            )
            assert flood.status_code == 400
        no_ticket = await client.post(
            "/upload-relay/files",
            files={"file": ("a.pdf", PDF, "application/pdf")},
            headers=auth_header(user_token),
        )
        assert no_ticket.status_code == 400

    assert await _stored(db_factory) == []
    assert await _audit(db_factory) == []
    warnings = [r.getMessage() for r in caplog.records if "without a valid ticket" in r.getMessage()]
    assert len(warnings) == 8
    assert all("uploader=testuser" in line for line in warnings)
    # An expired ticket was Noah's once: its id is worth a log line. A forged
    # one is not trusted even for that.
    assert f"ticket={TID}" in warnings[1]
    assert "victim" not in caplog.text and forged not in caplog.text


class _CountedBody:
    """A request body handed over in chunks, counting how many were taken."""

    def __init__(self, body: bytes, chunk: int = 4096) -> None:
        self.chunks = [body[start : start + chunk] for start in range(0, len(body), chunk)]
        self.taken = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.taken += 1
            yield chunk


async def test_a_bad_header_ticket_is_refused_before_the_body_is_read(client, user_token, db_factory):
    body, headers = _multipart(None, b"x" * 200_000)
    for ticket, status in (
        (mint_ticket(secret="another-secret-another-secret-12345"), 400),
        (mint_ticket(exp=int(time.time()) - 5), 410),
        ("garbage", 400),
    ):
        stream = _CountedBody(body)
        resp = await client.post(
            "/upload-relay/files",
            content=stream,
            headers={**headers, **auth_header(user_token), "X-Upload-Relay-Ticket": ticket},
        )
        assert resp.status_code == status
        assert stream.taken == 0, "the body was read before the ticket was verified"
    assert await _audit(db_factory) == []


async def test_a_header_ticket_bounds_how_much_of_the_body_is_read(client, user_token, db_factory):
    """With the ticket known up front, the read stops at what THAT ticket
    allows, not at the server's 32 MiB."""
    ticket = mint_ticket(maxBytes=1024)
    body, headers = _multipart(None, b"x" * 2_000_000)
    stream = _CountedBody(body)
    resp = await client.post(
        "/upload-relay/files",
        content=stream,
        headers={**headers, **auth_header(user_token), "X-Upload-Relay-Ticket": ticket},
    )
    assert resp.status_code == 413
    assert resp.json() == {"detail": "The file is larger than this upload allows"}
    allowed_chunks = (1024 + relay_router._BODY_OVERHEAD_BYTES) // 4096 + 2
    assert stream.taken <= allowed_chunks < len(stream.chunks)

    # A refusal under a valid ticket IS audited, under that ticket.
    [log] = await _audit(db_factory)
    assert (log.actor, log.resource_id, log.status) == ("testuser", TID, "error")

    # Declared honestly, it is refused without reading anything.
    declared = await client.post(
        "/upload-relay/files",
        content=body,
        headers={**headers, **auth_header(user_token), "X-Upload-Relay-Ticket": ticket},
    )
    assert declared.status_code == 413


async def test_the_form_field_ticket_still_works_when_there_is_no_header(client, user_token, db_factory):
    resp = await _upload(client, user_token, mint_ticket(), via="field")
    assert resp.status_code == 201
    [row] = await _stored(db_factory)
    assert (row.ticket_id, row.noah_user) == (TID, "jinyoung")

    # With both, the header is the ticket and the field is ignored.
    other = mint_ticket(tid="tid_another_0123456789", user="someone")
    both = await client.post(
        "/upload-relay/files",
        data={"ticket": mint_ticket()},
        files={"file": ("b.pdf", PDF, "application/pdf")},
        headers={**auth_header(user_token), "X-Upload-Relay-Ticket": other},
    )
    assert both.status_code == 201
    assert sorted(row.noah_user for row in await _stored(db_factory)) == ["jinyoung", "someone"]


async def test_a_ticket_holds_only_as_many_files_as_it_allows(client, user_token, db_factory):
    ticket = mint_ticket(maxFiles=2)
    assert (await _upload(client, user_token, ticket, name="a.pdf")).status_code == 201
    assert (await _upload(client, user_token, ticket, name="b.pdf")).status_code == 201
    third = await _upload(client, user_token, ticket, name="c.pdf")
    assert third.status_code == 409
    assert len(await _stored(db_factory)) == 2
    assert (await _describe(client, user_token, ticket)).json()["remainingFiles"] == 0
    assert [log.status for log in await _audit(db_factory)] == ["success", "success", "error"]


async def test_concurrent_uploads_cannot_pass_the_file_limit_together(client, user_token, db_factory):
    """Eight uploads at once on a one-file ticket: counting and inserting is
    one critical section, so exactly one gets in."""
    ticket = mint_ticket(maxFiles=1)
    responses = await asyncio.gather(
        *[_upload(client, user_token, ticket, name=f"f{i}.pdf") for i in range(8)]
    )
    assert sorted(resp.status_code for resp in responses) == [201] + [409] * 7
    assert len(await _stored(db_factory)) == 1
    assert sorted(log.status for log in await _audit(db_factory)) == ["error"] * 7 + ["success"]


async def test_concurrent_uploads_cannot_pass_the_byte_budgets_together(
    client, user_token, db_factory, monkeypatch
):
    # Room for one file in total, tickets of eight different users.
    monkeypatch.setattr(settings, "UPLOAD_RELAY_MAX_PENDING_BYTES", len(PDF) + 5)
    tickets = [mint_ticket(tid=f"tid_user{i}_0123456789ab", user=f"user{i}") for i in range(8)]
    responses = await asyncio.gather(*[_upload(client, user_token, t) for t in tickets])
    assert sorted(resp.status_code for resp in responses) == [201] + [503] * 7
    assert len(await _stored(db_factory)) == 1


async def test_one_noah_user_cannot_fill_the_relay_with_many_tickets(
    client, user_token, db_factory, monkeypatch
):
    """A ticket is free to mint, so the per-ticket file limit does not bound a
    user. Their own byte budget does, and it leaves everyone else's alone."""
    monkeypatch.setattr(settings, "UPLOAD_RELAY_MAX_PENDING_BYTES_PER_USER", 2 * len(PDF) + 5)
    tickets = [mint_ticket(tid=f"tid_greedy{i}_0123456789", user="greedy") for i in range(4)]
    responses = await asyncio.gather(*[_upload(client, user_token, t) for t in tickets])
    assert sorted(resp.status_code for resp in responses) == [201, 201, 429, 429]
    refused = next(resp for resp in responses if resp.status_code == 429)
    assert refused.json() == {
        "detail": "Too much is already waiting for this Noah user; "
        "try again once Noah has collected the earlier files"
    }

    # Another Noah user is not affected, and the greedy one is back in as soon
    # as Noah has collected a file.
    assert (await _upload(client, user_token, mint_ticket(user="modest"))).status_code == 201
    [first, *_] = [row for row in await _stored(db_factory) if row.noah_user == "greedy"]
    ack = await _noah(client, "POST", f"/upload-relay/files/{first.id}/ack", json={"outcome": "delivered"})
    assert ack.status_code == 204
    assert (await _upload(client, user_token, tickets[3])).status_code == 201
    assert [log.error_message for log in await _audit(db_factory) if log.status == "error"] == [
        refused.json()["detail"]
    ] * 2


async def test_on_postgres_the_critical_section_takes_an_advisory_lock():
    """Across processes (the other blue/green color) an asyncio lock is not
    enough: on Postgres the block also holds a transaction-level advisory lock."""
    executed = []

    class FakeSession:
        def __init__(self, dialect: str) -> None:
            self.dialect = dialect

        def get_bind(self):
            return type("Bind", (), {"dialect": type("Dialect", (), {"name": self.dialect})()})()

        async def execute(self, statement, params=None):
            executed.append((str(statement), params))

    async with relay.storing(FakeSession("postgresql")):
        assert relay.store_lock().locked()
    assert executed == [("SELECT pg_advisory_xact_lock(:key)", {"key": relay._STORE_LOCK_KEY})]
    assert -(2**63) <= relay._STORE_LOCK_KEY < 2**63  # a Postgres bigint

    executed.clear()
    async with relay.storing(FakeSession("sqlite")):
        assert relay.store_lock().locked()
    assert executed == []
    assert not relay.store_lock().locked()


async def test_the_relay_refuses_uploads_while_it_is_full(client, user_token, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_RELAY_MAX_PENDING_BYTES", len(PDF) + 5)
    assert (await _upload(client, user_token, mint_ticket(), name="a.pdf")).status_code == 201
    full = await _upload(client, user_token, mint_ticket(tid="tid_another_0123456789"), name="b.pdf")
    assert full.status_code == 503


def _multipart(
    ticket: str | None, content: bytes, filename: str = "big.pdf", boundary: str = "relayboundary"
) -> tuple[bytes, dict[str, str]]:
    """A raw multipart body; ``ticket=None`` leaves the ticket field out (it
    travels in the header instead)."""
    field = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="ticket"\r\n\r\n{ticket}\r\n'
        if ticket is not None
        else ""
    )
    body = (
        field
        + f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


async def test_an_oversized_body_is_cut_off_while_it_is_read(client, user_token, db_factory, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_RELAY_MAX_FILE_BYTES", 1024)
    body, headers = _multipart(mint_ticket(), b"x" * (1024 + relay_router._BODY_OVERHEAD_BYTES + 1))

    # Declared honestly: refused before a byte is read.
    declared = await client.post(
        "/upload-relay/files", content=body, headers={**headers, **auth_header(user_token)}
    )
    assert declared.status_code == 413

    # Chunked, with no Content-Length to check: counted as it arrives.
    read = 0

    async def chunks():
        nonlocal read
        for start in range(0, len(body), 4096):
            read = start + 4096
            yield body[start : start + 4096]

    streamed = await client.post(
        "/upload-relay/files", content=chunks(), headers={**headers, **auth_header(user_token)}
    )
    assert streamed.status_code == 413
    assert streamed.json() == {"detail": "The file is larger than this server accepts"}
    assert await _stored(db_factory) == []
    # The ticket was a form field inside the body that was cut off: nothing
    # verified it, so nothing is audited.
    assert await _audit(db_factory) == []

    # A file inside the server's cap but over it by less than the framing
    # allowance is still refused, by its own size.
    body, headers = _multipart(mint_ticket(), b"x" * 1025)
    resp = await client.post(
        "/upload-relay/files", content=body, headers={**headers, **auth_header(user_token)}
    )
    assert resp.status_code == 413
    assert resp.json() == {"detail": "The file is larger than this upload allows"}


async def test_a_control_character_in_a_name_is_refused(client, user_token, db_factory):
    # Sent raw, as a browser does (httpx would percent-encode it in the header).
    body, headers = _multipart(mint_ticket(), PDF, filename="re\x07port.pdf")
    resp = await client.post(
        "/upload-relay/files", content=body, headers={**headers, **auth_header(user_token)}
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "The file name is not usable"}
    assert await _stored(db_factory) == []


async def test_an_upload_must_be_one_multipart_file(client, user_token):
    headers = auth_header(user_token)
    not_multipart = await client.post("/upload-relay/files", json={"ticket": mint_ticket()}, headers=headers)
    assert not_multipart.status_code == 400

    no_file = await client.post(
        "/upload-relay/files", files={"ticket": (None, mint_ticket())}, headers=headers
    )
    assert no_file.status_code == 400
    assert no_file.json() == {"detail": "The upload carries no file"}

    two_files = await client.post(
        "/upload-relay/files",
        data={"ticket": mint_ticket()},
        files=[("file", ("a.pdf", PDF)), ("file", ("b.pdf", PDF))],
        headers=headers,
    )
    assert two_files.status_code == 400


# ── The Noah server's requests ──────────────────────────────────────────────


async def test_noah_requests_need_a_fresh_valid_signature(client, user_token):
    path = f"/upload-relay/tickets/{TID}/files"
    assert (await client.get(path, headers=noah_headers("GET", path))).status_code == 200

    refused = [
        {},
        auth_header(user_token),  # a Keycloak login is not what these take
        noah_headers("GET", path, secret="another-secret-another-secret-12345"),
        noah_headers("POST", path),
        noah_headers("GET", path + "x"),
        noah_headers("GET", path, at=time.time() - 301),
        noah_headers("GET", path, at=time.time() + 301),
        {**noah_headers("GET", path), "X-Noah-Relay-Timestamp": "now"},
        {"X-Noah-Relay-Timestamp": str(int(time.time()))},
    ]
    for headers in refused:
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 401, headers

    # A signature for one path does not open another.
    other = "/upload-relay/files/0123456789abcdef0123456789abcdef"
    assert (await client.get(other, headers=noah_headers("GET", path))).status_code == 401
    assert (await client.post(other + "/ack", json={"outcome": "delivered"}, headers=noah_headers("GET", other))).status_code == 401


async def test_collecting_a_file_lists_downloads_and_acknowledges_it(client, user_token, db_factory):
    ticket = mint_ticket()
    first = (await _upload(client, user_token, ticket, name="a.pdf")).json()
    second = (await _upload(client, user_token, ticket, name="b.png", content=b"\x89PNG data")).json()
    await _upload(client, user_token, mint_ticket(tid="tid_another_0123456789"), name="other.pdf")

    listing = await _noah(client, "GET", f"/upload-relay/tickets/{TID}/files")
    assert listing.status_code == 200
    files = listing.json()["files"]
    assert [(f["id"], f["name"], f["size"], f["sha256"], f["uploader"]) for f in files] == [
        (first["id"], "a.pdf", len(PDF), first["sha256"], "testuser"),
        (second["id"], "b.png", 9, second["sha256"], "testuser"),
    ]
    assert all(f["uploadedAt"].endswith("Z") for f in files)

    path = f"/upload-relay/files/{first['id']}"
    for _ in range(2):  # downloading does not delete
        download = await _noah(client, "GET", path)
        assert download.status_code == 200
        assert download.content == PDF
        assert download.headers["content-type"] == "application/octet-stream"
        assert download.headers["cache-control"] == "no-store"
        assert download.headers["x-relay-sha256"] == first["sha256"]

    ack = await _noah(client, "POST", f"{path}/ack", json={"outcome": "delivered"})
    assert ack.status_code == 204
    assert (await _noah(client, "GET", path)).status_code == 404
    assert (await _noah(client, "POST", f"{path}/ack", json={"outcome": "delivered"})).status_code == 404

    refused = await _noah(
        client,
        "POST",
        f"/upload-relay/files/{second['id']}/ack",
        json={"outcome": "refused", "reason": "BAD_FORMAT"},
    )
    assert refused.status_code == 204
    assert (await _noah(client, "GET", f"/upload-relay/tickets/{TID}/files")).json() == {"files": []}
    assert [row.filename for row in await _stored(db_factory)] == ["other.pdf"]

    logs = [log for log in await _audit(db_factory) if log.action != "upload"]
    collected, delivered, turned_away = logs
    # Two downloads, one collect row: the first is the one that matters.
    assert (collected.actor, collected.action, collected.resource_id, collected.status) == (
        "noah:jinyoung", "collect", first["id"], "success",
    )
    assert "a.pdf" in collected.summary and "downloaded by Noah" in collected.summary
    assert (delivered.actor, delivered.action, delivered.resource_id, delivered.status) == (
        "noah:jinyoung", "deliver", first["id"], "success",
    )
    assert "a.pdf" in delivered.summary and "testuser" in delivered.summary
    assert json.loads(delivered.after)["collected_at"].endswith("Z")
    # The second file was refused without ever being downloaded.
    assert "collected_at" not in json.loads(turned_away.after)
    assert (turned_away.action, turned_away.status, turned_away.error_message) == ("refuse", "error", "BAD_FORMAT")
    assert json.loads(turned_away.after)["sha256"] == second["sha256"]


async def test_an_acknowledgement_must_name_its_outcome(client, user_token):
    stored = (await _upload(client, user_token, mint_ticket())).json()
    path = f"/upload-relay/files/{stored['id']}/ack"
    assert (await _noah(client, "POST", path, json={"outcome": "lost"})).status_code == 422
    assert (await _noah(client, "POST", path, json={"outcome": "refused", "reason": "r" * 201})).status_code == 422
    assert (await _noah(client, "GET", f"/upload-relay/files/{stored['id']}")).status_code == 200


# ── Expiry ──────────────────────────────────────────────────────────────────


async def test_an_uncollected_file_expires_and_is_audited(client, user_token, db_factory):
    kept = (await _upload(client, user_token, mint_ticket(), name="kept.pdf")).json()
    gone = (await _upload(client, user_token, mint_ticket(), name="gone.pdf")).json()
    async with db_factory() as db:
        await db.execute(
            update(UploadRelayFile)
            .where(UploadRelayFile.id == gone["id"])
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db.commit()

    # Any relay request purges first: the expired file is neither listed nor served.
    listing = await _noah(client, "GET", f"/upload-relay/tickets/{TID}/files")
    assert [f["id"] for f in listing.json()["files"]] == [kept["id"]]
    assert (await _noah(client, "GET", f"/upload-relay/files/{gone['id']}")).status_code == 404
    assert [row.id for row in await _stored(db_factory)] == [kept["id"]]

    [expired] = [log for log in await _audit(db_factory) if log.action == "expire"]
    assert (expired.actor, expired.resource_id, expired.status) == ("system", gone["id"], "success")
    assert "gone.pdf" in expired.summary and "jinyoung" in expired.summary

    async with db_factory() as db:
        assert await relay.purge_expired(db) == 0


async def _expire_everything(db_factory, seconds_ago: int = 1) -> None:
    async with db_factory() as db:
        await db.execute(
            update(UploadRelayFile).values(expires_at=utcnow() - timedelta(seconds=seconds_ago))
        )
        await db.commit()


async def test_expiry_tells_never_collected_from_collected_but_unacknowledged(
    client, user_token, db_factory, caplog
):
    """Noah downloading a file and never acknowledging it is not "Noah did
    not collect it": Noah HAS those bytes, and the trail has to say so."""
    untouched = (await _upload(client, user_token, mint_ticket(), name="untouched.pdf")).json()
    taken = (await _upload(client, user_token, mint_ticket(), name="taken.pdf")).json()
    assert (await _noah(client, "GET", f"/upload-relay/files/{taken['id']}")).content == PDF

    # Downloaded, but still there until it expires: Noah acknowledges only
    # after the user's browser has the bytes, and may retry.
    listing = await _noah(client, "GET", f"/upload-relay/tickets/{TID}/files")
    assert [f["id"] for f in listing.json()["files"]] == [untouched["id"], taken["id"]]
    [row] = [row for row in await _stored(db_factory) if row.id == taken["id"]]
    assert row.collected_at is not None

    await _expire_everything(db_factory)
    with caplog.at_level(logging.INFO, logger=relay.__name__):
        async with db_factory() as db:
            assert await relay.purge_expired(db) == 2

    expired = {log.resource_id: log for log in await _audit(db_factory) if log.action == "expire"}
    assert "Noah never collected it" in expired[untouched["id"]].summary
    assert "collected_at" not in json.loads(expired[untouched["id"]].after)
    summary = expired[taken["id"]].summary
    assert "Noah collected it at " in summary and "did not acknowledge" in summary
    assert "never collected" not in summary
    assert json.loads(expired[taken["id"]].after)["collected_at"] in summary
    assert "expired (Noah collected it at" in caplog.text


async def test_an_acknowledgement_just_past_expiry_still_wins(client, user_token, db_factory):
    """The ack handler deletes its row before it purges: a delivered file must
    not be recorded as expired because its ack arrived a moment late."""
    stored = (await _upload(client, user_token, mint_ticket(), name="late.pdf")).json()
    other = (await _upload(client, user_token, mint_ticket(), name="abandoned.pdf")).json()
    assert (await _noah(client, "GET", f"/upload-relay/files/{stored['id']}")).status_code == 200
    await _expire_everything(db_factory)

    ack = await _noah(
        client, "POST", f"/upload-relay/files/{stored['id']}/ack", json={"outcome": "delivered"}
    )
    assert ack.status_code == 204

    by_file = {}
    for log in await _audit(db_factory):
        by_file.setdefault(log.resource_id, []).append(log.action)
    assert by_file[stored["id"]] == ["upload", "collect", "deliver"]
    # The same request's purge still clears what really was abandoned.
    assert by_file[other["id"]] == ["upload", "expire"]
    assert await _stored(db_factory) == []

    # A retried acknowledgement of the same file is a plain 404, nothing more.
    again = await _noah(
        client, "POST", f"/upload-relay/files/{stored['id']}/ack", json={"outcome": "delivered"}
    )
    assert again.status_code == 404
    assert len(await _audit(db_factory)) == 5


async def test_the_purge_loop_runs_only_on_the_active_color(db_factory, monkeypatch):
    async with db_factory() as db:
        db.add(
            UploadRelayFile(
                id="f" * 32, ticket_id=TID, noah_user="jinyoung", uploader="testuser",
                filename="old.pdf", size=3, sha256="0" * 64, content=b"old",
                expires_at=utcnow() - timedelta(minutes=1),
            )
        )
        await db.commit()

    import app.database as database

    monkeypatch.setattr(database, "async_session", db_factory)
    active = {"value": False}
    sleeps = []

    async def is_active():
        return active["value"]

    class Stop(Exception):
        pass

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            active["value"] = True
        if len(sleeps) == 4:
            raise Stop

    monkeypatch.setattr(relay, "is_active_instance", is_active)
    monkeypatch.setattr(relay.asyncio, "sleep", sleep)
    with pytest.raises(Stop):
        await relay.run_purge_loop(interval_seconds=7, first_delay_seconds=3)

    assert sleeps == [3, 7, 7, 7]
    # Pass 1 ran as the standby (nothing deleted); pass 2 as the active color.
    assert await _stored(db_factory) == []
    assert [log.action for log in await _audit(db_factory)] == ["expire"]


async def test_a_failing_purge_pass_does_not_end_the_loop(monkeypatch, caplog):
    passes = []

    async def failing_pass():
        passes.append(1)
        raise RuntimeError("database is away")

    class Stop(Exception):
        pass

    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    async def is_active():
        return True

    monkeypatch.setattr(relay, "run_purge_once", failing_pass)
    monkeypatch.setattr(relay, "is_active_instance", is_active)
    monkeypatch.setattr(relay.asyncio, "sleep", sleep)
    with caplog.at_level(logging.ERROR, logger=relay.__name__), pytest.raises(Stop):
        await relay.run_purge_loop()

    assert len(passes) == 2
    assert "Upload relay purge pass failed" in caplog.text


async def test_a_file_removed_by_another_pass_is_not_audited_twice(client, user_token, db_factory, monkeypatch):
    """Two purges (or an acknowledgement and a purge) can reach the same row:
    only the one whose DELETE removed it records it."""
    stored = (await _upload(client, user_token, mint_ticket())).json()
    async with db_factory() as db:
        await db.execute(update(UploadRelayFile).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()

    real_delete = relay.sa_delete

    def delete_nothing(model):
        # The other pass got there first: this DELETE matches no row.
        return real_delete(model).where(UploadRelayFile.id == "gone")

    monkeypatch.setattr(relay, "sa_delete", delete_nothing)
    async with db_factory() as db:
        assert await relay.purge_expired(db) == 0
    assert [log.action for log in await _audit(db_factory)] == ["upload"]

    # Same race on an acknowledgement: 404, and no deliver row.
    monkeypatch.setattr(relay, "sa_delete", real_delete)
    async with db_factory() as db:
        await db.execute(update(UploadRelayFile).values(expires_at=utcnow() + timedelta(minutes=5)))
        await db.commit()
    monkeypatch.setattr(relay_router, "sa_delete", delete_nothing)
    ack = await _noah(client, "POST", f"/upload-relay/files/{stored['id']}/ack", json={"outcome": "delivered"})
    assert ack.status_code == 404
    assert [log.action for log in await _audit(db_factory)] == ["upload"]


# ── Switched off ────────────────────────────────────────────────────────────


async def test_without_a_secret_every_endpoint_is_503(client, user_token, monkeypatch, caplog):
    ticket = mint_ticket()
    monkeypatch.setattr(settings, "UPLOAD_RELAY_SECRET", "")
    file_path = "/upload-relay/files/0123456789abcdef0123456789abcdef"
    with caplog.at_level(logging.WARNING, logger=relay_router.__name__):
        responses = [
            await _describe(client, user_token, ticket),
            await _upload(client, user_token, ticket),
            await client.get(f"/upload-relay/tickets/{TID}/files", headers=noah_headers("GET", f"/upload-relay/tickets/{TID}/files")),
            await client.get(file_path, headers=noah_headers("GET", file_path)),
            await client.post(f"{file_path}/ack", json={"outcome": "delivered"}, headers=noah_headers("POST", f"{file_path}/ack")),
        ]
    assert [resp.status_code for resp in responses] == [503] * 5
    assert responses[0].json() == {"detail": "The upload relay is not configured"}
    warnings = [r for r in caplog.records if "UPLOAD_RELAY_SECRET is empty" in r.getMessage()]
    assert len(warnings) == 1

    # Signing in still comes first for the browser endpoints.
    assert (await _describe(client, None, ticket)).status_code == 401


def test_a_short_secret_fails_startup(monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_RELAY_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="UPLOAD_RELAY_SECRET"):
        app_config.validate_settings()
    monkeypatch.setattr(settings, "UPLOAD_RELAY_SECRET", "")
    app_config.validate_settings()
    monkeypatch.setattr(settings, "UPLOAD_RELAY_SECRET", SECRET)
    app_config.validate_settings()


def test_the_secret_is_stripped_of_surrounding_whitespace(monkeypatch):
    """Noah trims its copy; a newline or space left in this .env must not
    make every signature differ."""
    loaded = app_config.Settings(UPLOAD_RELAY_SECRET=f"  {SECRET}\n")
    assert loaded.UPLOAD_RELAY_SECRET == SECRET
    assert app_config.Settings(UPLOAD_RELAY_SECRET="   ").UPLOAD_RELAY_SECRET == ""

    # The length rule looks at what is left after stripping.
    monkeypatch.setattr(app_config, "settings", app_config.Settings(UPLOAD_RELAY_SECRET=" short-secret" + " " * 40))
    with pytest.raises(RuntimeError, match="UPLOAD_RELAY_SECRET"):
        app_config.validate_settings()


# ── File names ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("  report.pdf  ", "report.pdf"),
        ("dir/sub\\report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("보고서.pdf", "보고서.pdf"),  # decomposed Hangul in, composed out
        ("", None),
        ("dir/", None),
        ("..", None),
        (".", None),
        (None, None),
        ("line\nbreak.pdf", None),
        ("sep\u2028arator.pdf", None),
        ("bidi\u2066.pdf", None),
        ("a" * 256, None),
        ("half\ud83d.pdf", None),  # a lone surrogate cannot be stored or logged
    ],
)
def test_clean_filename(raw, expected):
    assert relay.clean_filename(raw) == expected


def test_filename_extension_is_lowercased():
    assert relay.filename_extension("Report.PDF") == ".pdf"
    assert relay.filename_extension("archive.tar.gz") == ".gz"
    assert relay.filename_extension("noext") == ""
    assert relay.filename_extension(".pdf") == ""
