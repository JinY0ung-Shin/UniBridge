"""API key safeguards.

- a typed-in key value has to be long enough, header-safe and not already taken
- a create without a value generates one instead of leaving a keyless consumer
- an expired key stays off the gateway whitelists when an admin edits it
- issuing, regenerating and deleting a self-service key land in the audit log
- a deleted or disabled account cannot issue or extend a key with the token
  it still holds (the Users page side of this is in ``test_users.py``)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import CurrentUser, get_current_user
from app.models import AdminAuditLog, ApiKeyAccess
from tests.conftest import auth_header

TYPED_VALUE = "typed-key-value-0001"
ROUTE = {
    "id": "query-api",
    "uri": "/api/query/*",
    "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["__deny_all__"]}},
}


def _sessions(seeded_db):
    return async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)


def _consumer(name: str, key: str) -> dict:
    return {"username": name, "plugins": {"key-auth": {"key": key}}}


def _apisix(*, consumers: list[dict] | None = None, routes: list[dict] | None = None):
    """Patch the router's APISIX client with separate consumer and route listings."""

    async def list_resources(resource_type):
        if resource_type == "consumers":
            return {"items": consumers or []}
        return {"items": routes or []}

    patcher = patch("app.routers.api_keys.apisix_client")
    mock = patcher.start()
    mock.list_resources = AsyncMock(side_effect=list_resources)
    mock.put_resource = AsyncMock(return_value={})
    mock.get_resource = AsyncMock(side_effect=Exception("not found"))
    mock.delete_resource = AsyncMock()
    return patcher, mock


@pytest.fixture
def apisix():
    patcher, mock = _apisix()
    yield mock
    patcher.stop()


def _puts(mock, resource_type: str) -> list[tuple[str, dict]]:
    return [
        (call.args[1], call.args[2])
        for call in mock.put_resource.await_args_list
        if call.args[0] == resource_type
    ]


# ── key values ──────────────────────────────────────────────────────────────


async def test_create_without_a_value_generates_one(client, admin_token, apisix):
    resp = await client.post(
        "/admin/api-keys", json={"name": "gen-app"}, headers=auth_header(admin_token)
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["key_created"] is True
    assert len(data["api_key"]) >= 32
    # The consumer carries that same value: without key-auth nobody could use it.
    assert _puts(apisix, "consumers") == [
        ("gen-app", {"username": "gen-app", "plugins": {"key-auth": {"key": data["api_key"]}}})
    ]


async def test_generated_values_differ(client, admin_token, apisix):
    first = await client.post(
        "/admin/api-keys", json={"name": "gen-a"}, headers=auth_header(admin_token)
    )
    second = await client.post(
        "/admin/api-keys", json={"name": "gen-b", "api_key": ""}, headers=auth_header(admin_token)
    )
    assert first.json()["api_key"] != second.json()["api_key"]


@pytest.mark.parametrize(
    "value",
    [
        "short-key",
        "x" * 15,
        "x" * 257,
        "has a space 0123456789",
        "tab\tin-the-middle-0123",
        "한글이들어간키값0123456789",
    ],
)
async def test_create_rejects_a_weak_or_unsafe_value(client, admin_token, apisix, value):
    resp = await client.post(
        "/admin/api-keys",
        json={"name": "weak-app", "api_key": value},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422
    apisix.put_resource.assert_not_awaited()


async def test_update_rejects_a_weak_value(client, admin_token, apisix):
    await client.post(
        "/admin/api-keys", json={"name": "rot-app"}, headers=auth_header(admin_token)
    )
    apisix.put_resource.reset_mock()

    resp = await client.put(
        "/admin/api-keys/rot-app", json={"api_key": "tiny"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422
    apisix.put_resource.assert_not_awaited()


async def test_create_refuses_a_value_another_key_has(client, admin_token):
    patcher, mock = _apisix(consumers=[_consumer("other-app", TYPED_VALUE)])
    try:
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "twin-app", "api_key": TYPED_VALUE},
            headers=auth_header(admin_token),
        )
    finally:
        patcher.stop()

    assert resp.status_code == 409
    assert "already used" in resp.json()["detail"]
    # The other key's name is not given away, and nothing was written.
    assert "other-app" not in resp.json()["detail"]
    mock.put_resource.assert_not_awaited()


async def test_update_may_keep_its_own_value_but_not_take_anothers(client, admin_token):
    consumers = [_consumer("mine-app", TYPED_VALUE), _consumer("other-app", "other-key-value-0002")]
    patcher, mock = _apisix(consumers=consumers)
    mock.get_resource = AsyncMock(return_value=_consumer("mine-app", TYPED_VALUE))
    try:
        created = await client.post(
            "/admin/api-keys",
            json={"name": "mine-app", "api_key": "first-key-value-0000"},
            headers=auth_header(admin_token),
        )
        assert created.status_code == 201
        mock.put_resource.reset_mock()

        same = await client.put(
            "/admin/api-keys/mine-app",
            json={"api_key": TYPED_VALUE},
            headers=auth_header(admin_token),
        )
        taken = await client.put(
            "/admin/api-keys/mine-app",
            json={"api_key": "other-key-value-0002"},
            headers=auth_header(admin_token),
        )
    finally:
        patcher.stop()

    assert same.status_code == 200
    assert taken.status_code == 409
    # Only the first update reached the consumer.
    assert [body["plugins"]["key-auth"]["key"] for _, body in _puts(mock, "consumers")] == [
        TYPED_VALUE
    ]


async def test_value_check_fails_closed_when_consumers_cannot_be_listed(client, admin_token):
    patcher, mock = _apisix()
    mock.list_resources = AsyncMock(side_effect=RuntimeError("apisix down"))
    try:
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "blind-app", "api_key": TYPED_VALUE},
            headers=auth_header(admin_token),
        )
    finally:
        patcher.stop()

    assert resp.status_code == 502
    mock.put_resource.assert_not_awaited()


# ── an expired key stays off the gateway ────────────────────────────────────


async def _store_key(seeded_db, name: str, expires_at: datetime | None) -> None:
    async with _sessions(seeded_db)() as db:
        db.add(ApiKeyAccess(
            consumer_name=name,
            description="",
            owner="owner-sub-1",
            allowed_databases=json.dumps(["*"]),
            allowed_routes=json.dumps(["query-api"]),
            expires_at=expires_at,
        ))
        await db.commit()


@pytest.mark.parametrize(
    ("expires_in", "whitelisted"),
    [(timedelta(days=-1), False), (timedelta(days=1), True)],
)
async def test_editing_a_key_whitelists_it_only_while_it_is_valid(
    client, admin_token, seeded_db, expires_in, whitelisted
):
    await _store_key(seeded_db, "self_owner1", datetime.now(timezone.utc) + expires_in)
    patcher, mock = _apisix(routes=[ROUTE])
    try:
        # What the UI sends for any edit: the description plus the unchanged grants.
        resp = await client.put(
            "/admin/api-keys/self_owner1",
            json={"description": "edited", "allowed_routes": ["query-api"]},
            headers=auth_header(admin_token),
        )
    finally:
        patcher.stop()

    assert resp.status_code == 200
    # The stored grants are kept either way: Renew brings them back.
    assert resp.json()["allowed_routes"] == ["query-api"]
    on_gateway = any(
        "self_owner1" in body["plugins"]["consumer-restriction"]["whitelist"]
        for _, body in _puts(mock, "routes")
    )
    assert on_gateway is whitelisted


# ── self-service audit trail ────────────────────────────────────────────────


async def test_self_service_key_lifecycle_is_audited(app, client, seeded_db, apisix):
    async def _alice() -> CurrentUser:
        return CurrentUser(username="alice", role="user", sub="alice-sub-9")

    app.dependency_overrides[get_current_user] = _alice

    created = await client.post("/admin/api-keys/me")
    apisix.get_resource = AsyncMock(
        return_value=_consumer("self_alicesub9", created.json()["api_key"])
    )
    regenerated = await client.post("/admin/api-keys/me/regenerate")
    deleted = await client.delete("/admin/api-keys/me")
    assert (created.status_code, regenerated.status_code, deleted.status_code) == (201, 200, 204)

    async with _sessions(seeded_db)() as db:
        rows = (
            await db.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.resource_type == "api_key")
                .order_by(AdminAuditLog.id)
            )
        ).scalars().all()

    assert [(row.actor, row.action, row.resource_id) for row in rows] == [
        ("alice", "create", "self_alicesub9"),
        ("alice", "update", "self_alicesub9"),
        ("alice", "delete", "self_alicesub9"),
    ]
    assert all(row.status == "success" for row in rows)
    # Neither key value is written down in full.
    recorded = " ".join((row.before or "") + (row.after or "") for row in rows)
    assert created.json()["api_key"] not in recorded
    assert regenerated.json()["api_key"] not in recorded
    assert json.loads(rows[2].before)["owner"] == "alice-sub-9"


# ── a stale token cannot bring a key back ───────────────────────────────────


def _keycloak(*, enabled: bool = True, missing: bool = False):
    from fastapi import HTTPException

    kc = AsyncMock()
    if missing:
        kc.get_user.side_effect = HTTPException(status_code=404, detail="User not found")
    else:
        kc.get_user.return_value = {"id": "alice-sub-9", "username": "alice", "enabled": enabled}
    return kc


def _as_alice(app) -> None:
    async def _alice() -> CurrentUser:
        return CurrentUser(username="alice", role="user", sub="alice-sub-9")

    app.dependency_overrides[get_current_user] = _alice


@pytest.mark.parametrize(
    ("account", "detail"),
    [
        ({"enabled": False}, "disabled"),
        ({"missing": True}, "no longer exists"),
    ],
)
@pytest.mark.parametrize("path", ["", "/regenerate", "/renew"])
async def test_stale_token_cannot_issue_or_extend_a_key(
    app, client, seeded_db, apisix, monkeypatch, account, detail, path
):
    from app.config import settings

    _as_alice(app)
    if path:
        # An existing key, already expired by the Users page.
        async with _sessions(seeded_db)() as db:
            db.add(ApiKeyAccess(
                consumer_name="self_alicesub9",
                owner="alice-sub-9",
                allowed_routes=json.dumps(["query-api"]),
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            ))
            await db.commit()

    monkeypatch.setattr(settings, "KEYCLOAK_JWT_AUDIENCE", "apihub")
    with patch("app.routers.users._get_kc_admin", return_value=_keycloak(**account)):
        resp = await client.post(f"/admin/api-keys/me{path}")

    assert resp.status_code == 403
    assert detail in resp.json()["detail"]
    apisix.put_resource.assert_not_awaited()
    async with _sessions(seeded_db)() as db:
        stored = (await db.execute(select(ApiKeyAccess))).scalars().all()
    # Nothing issued, and the expired key is still expired.
    assert all(row.expires_at < datetime.now(timezone.utc) for row in stored)
    assert len(stored) == (1 if path else 0)


async def test_live_account_issues_and_renews_as_before(app, client, apisix, monkeypatch):
    from app.config import settings

    _as_alice(app)
    kc = _keycloak()
    monkeypatch.setattr(settings, "KEYCLOAK_JWT_AUDIENCE", "apihub")
    with patch("app.routers.users._get_kc_admin", return_value=kc):
        created = await client.post("/admin/api-keys/me")
        renewed = await client.post("/admin/api-keys/me/renew")

    assert (created.status_code, renewed.status_code) == (201, 200)
    kc.get_user.assert_awaited_with("alice-sub-9")


async def test_keycloak_outage_refuses_rather_than_guessing(app, client, apisix, monkeypatch):
    from app.config import settings

    _as_alice(app)
    kc = _keycloak()
    kc.get_user.side_effect = RuntimeError("keycloak down")
    monkeypatch.setattr(settings, "KEYCLOAK_JWT_AUDIENCE", "apihub")
    with patch("app.routers.users._get_kc_admin", return_value=kc):
        resp = await client.post("/admin/api-keys/me")

    assert resp.status_code == 502
    apisix.put_resource.assert_not_awaited()
