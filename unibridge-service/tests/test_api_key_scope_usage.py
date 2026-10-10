"""Tests for the API key page's issuer scoping (``?scope=mine``), the boot-time
issuer backfill, and the per-key 7/30-day usage counts (``/admin/api-keys/usage``)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import CurrentUser, create_token, get_current_user, invalidate_permission_cache
from app.models import AdminAuditLog, ApiKeyAccess, Role, RolePermission
from app.routers.api_keys import backfill_api_key_issuers
from tests.conftest import auth_header


async def _create_key(client, token: str | None, name: str) -> dict:
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})
        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": name,
                "api_key": f"{name}-secret",
                "allowed_databases": [],
                "allowed_routes": [],
            },
            headers=auth_header(token) if token else {},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _list_keys(client, headers: dict | None = None, **params) -> dict[str, dict]:
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        resp = await client.get("/admin/api-keys", params=params, headers=headers or {})
    assert resp.status_code == 200, resp.text
    return {key["name"]: key for key in resp.json()}


def _as_user(app, *, username: str, sub: str, role: str = "admin") -> None:
    async def _fake_current_user() -> CurrentUser:
        return CurrentUser(username=username, role=role, sub=sub)

    app.dependency_overrides[get_current_user] = _fake_current_user


@pytest.mark.asyncio
async def test_mine_scope_lists_only_the_keys_the_caller_issued(client, admin_token):
    other_token = create_token("otheradmin", "admin")
    created = await _create_key(client, admin_token, "mine-app")
    assert created["created_by"] == "testadmin"
    await _create_key(client, other_token, "their-app")

    mine = await _list_keys(client, auth_header(admin_token), scope="mine")
    assert set(mine) == {"mine-app"}
    assert mine["mine-app"]["created_by"] == "testadmin"

    everything = await _list_keys(client, auth_header(admin_token), scope="all")
    assert set(everything) == {"mine-app", "their-app"}
    assert everything["their-app"]["created_by"] == "otheradmin"

    # No scope keeps listing every key: the monitoring pages' key filters rely on it.
    assert set(await _list_keys(client, auth_header(admin_token))) == {"mine-app", "their-app"}


@pytest.mark.asyncio
async def test_admin_keys_are_matched_by_username_not_sub(app, client):
    # Under Keycloak the sub is a UUID while the audit log (and so the backfill)
    # names the username: the issuer has to be the username for both to agree.
    _as_user(app, username="alice", sub="0b7e4c52-8d4e-4a52-9a61-6f1e0c3d2b19")
    created = await _create_key(client, None, "alice-app")
    assert created["created_by"] == "alice"
    assert set(await _list_keys(client, scope="mine")) == {"alice-app"}


@pytest.mark.asyncio
async def test_mine_scope_includes_the_callers_own_self_service_key(app, client, seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add_all([
            # Self-service keys issued before created_by existed carry only owner (the sub).
            ApiKeyAccess(consumer_name="self_alice", owner="alice-sub"),
            ApiKeyAccess(consumer_name="self_bob", owner="bob-sub"),
            # An admin key older than the audit trail: issuer unknown.
            ApiKeyAccess(consumer_name="legacy-app"),
        ])
        await db.commit()

    _as_user(app, username="alice", sub="alice-sub")
    assert set(await _list_keys(client, scope="mine")) == {"self_alice"}
    assert set(await _list_keys(client, scope="all")) == {"self_alice", "self_bob", "legacy-app"}


@pytest.mark.asyncio
async def test_self_service_key_records_its_owner_as_issuer(app, client):
    _as_user(app, username="alice", sub="alice-sub")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})
        resp = await client.post("/admin/api-keys/me")
    assert resp.status_code == 201, resp.text
    assert resp.json()["created_by"] == "alice"


@pytest.mark.asyncio
async def test_backfill_names_each_keys_creator_from_the_audit_log(seeded_db):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add_all([
            ApiKeyAccess(consumer_name="recreated"),
            ApiKeyAccess(consumer_name="audited"),
            ApiKeyAccess(consumer_name="lost-audit"),
            ApiKeyAccess(consumer_name="unaudited"),
            ApiKeyAccess(consumer_name="self_x", owner="x-sub"),
            ApiKeyAccess(consumer_name="kept", created_by="keeper"),
        ])
        for actor, action, resource_type, resource_id, outcome in [
            # Deleted and re-created: the key belongs to whoever created it last.
            ("alice", "create", "api_key", "recreated", "success"),
            ("alice", "delete", "api_key", "recreated", "success"),
            ("bob", "create", "api_key", "recreated", "success"),
            ("carol", "create", "api_key", "audited", "success"),
            ("mallory", "create", "api_key", "audited", "error"),
            # Re-created by someone whose audit write was lost: no longer alice's.
            ("alice", "create", "api_key", "lost-audit", "success"),
            ("alice", "delete", "api_key", "lost-audit", "success"),
            # Another resource type sharing the name is not this key's history.
            ("dave", "create", "route", "unaudited", "success"),
            # Self-service keys are matched by owner, and a recorded issuer stays.
            ("erin", "create", "api_key", "self_x", "success"),
            ("frank", "create", "api_key", "kept", "success"),
        ]:
            db.add(AdminAuditLog(
                actor=actor, action=action, resource_type=resource_type,
                resource_id=resource_id, status=outcome,
            ))
            await db.flush()  # ids follow the order above
        await db.commit()

        assert await backfill_api_key_issuers(db) == 2
        assert await backfill_api_key_issuers(db) == 0  # every boot runs it again

        issuers = dict((await db.execute(
            select(ApiKeyAccess.consumer_name, ApiKeyAccess.created_by)
        )).all())
    assert issuers == {
        "recreated": "bob",
        "audited": "carol",
        "lost-audit": None,
        "unaudited": None,
        "self_x": None,
        "kept": "keeper",
    }


@pytest.mark.asyncio
async def test_list_rejects_an_unknown_scope(client, admin_token):
    resp = await client.get(
        "/admin/api-keys", params={"scope": "team"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_usage_counts_each_keys_requests_over_7_and_30_days(client, admin_token):
    await _create_key(client, admin_token, "busy-app")
    await _create_key(client, admin_token, "idle-app")

    async def instant_query(query, eval_time=None):
        if "[7d]" in query:
            return [
                {"metric": {"consumer": "busy-app"}, "value": [eval_time, "41.6"]},
                # Traffic of a key that no longer exists stays out of the response.
                {"metric": {"consumer": "deleted-app"}, "value": [eval_time, "9"]},
            ]
        assert "[30d]" in query
        return [{"metric": {"consumer": "busy-app"}, "value": [eval_time, "120.2"]}]

    mock = AsyncMock(side_effect=instant_query)
    with patch("app.routers.api_keys.prometheus_client.instant_query", mock):
        resp = await client.get("/admin/api-keys/usage", headers=auth_header(admin_token))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "keys": {
            "busy-app": {"requests_7d": 42, "requests_30d": 120},
            "idle-app": {"requests_7d": 0, "requests_30d": 0},
        }
    }
    queries = [call.args[0] for call in mock.call_args_list]
    assert len(queries) == 2
    # Every route counts, LLM ones included: nothing narrows the selector by route.
    assert all("route" not in query for query in queries)
    assert all('consumer!=""' in query for query in queries)
    # The first request of each series counts too (increase() alone drops it).
    assert any("min_over_time" in q and "offset 7d" in q for q in queries)
    assert any("min_over_time" in q and "offset 30d" in q for q in queries)
    # Both windows end at the same instant.
    assert len({call.kwargs["eval_time"] for call in mock.call_args_list}) == 1


@pytest.mark.asyncio
async def test_usage_30d_never_reads_below_7d(client, admin_token):
    await _create_key(client, admin_token, "edge-app")

    async def instant_query(query, eval_time=None):
        value = "10" if "[7d]" in query else "9.4"
        return [{"metric": {"consumer": "edge-app"}, "value": [eval_time, value]}]

    with patch(
        "app.routers.api_keys.prometheus_client.instant_query",
        AsyncMock(side_effect=instant_query),
    ):
        resp = await client.get("/admin/api-keys/usage", headers=auth_header(admin_token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["keys"]["edge-app"] == {"requests_7d": 10, "requests_30d": 10}


@pytest.mark.asyncio
async def test_usage_reports_502_when_prometheus_fails(client, admin_token):
    with patch(
        "app.routers.api_keys.prometheus_client.instant_query",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        resp = await client.get("/admin/api-keys/usage", headers=auth_header(admin_token))
    assert resp.status_code == 502
    assert "Prometheus error" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_usage_requires_apikeys_read(client, user_token):
    resp = await client.get("/admin/api-keys/usage", headers=auth_header(user_token))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_usage_also_requires_gateway_monitoring_read(client, seeded_db):
    # A role that may list keys but not see gateway traffic gets the list only.
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        role = Role(name="keyviewer", description="Test key viewer", is_system=False)
        db.add(role)
        await db.flush()
        db.add(RolePermission(role_id=role.id, permission="apikeys.read"))
        await db.commit()
    await invalidate_permission_cache()
    headers = auth_header(create_token("testkeyviewer", "keyviewer"))

    mock = AsyncMock(return_value=[])
    with patch("app.routers.api_keys.prometheus_client.instant_query", mock):
        resp = await client.get("/admin/api-keys/usage", headers=headers)
    assert resp.status_code == 403
    assert "gateway.monitoring.read" in resp.json()["detail"]
    mock.assert_not_called()
    assert await _list_keys(client, headers) == {}
