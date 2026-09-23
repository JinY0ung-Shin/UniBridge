"""Gateway-side API key expiry: reconciling consumer-restriction whitelists.

App-level auth already rejects an expired key, but the LLM gateway routes go
APISIX → llm-converter → LiteLLM without ever reaching the app. The only place
an expired key can be stopped there is the ``consumer-restriction`` whitelist,
which these tests pin down.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKeyAccess
from app.services import consumer_restrictions as cr
from app.services.consumer_restrictions import (
    DENY_ALL_CONSUMER,
    MASTER_ACCESS,
    reconcile_consumer_route_restrictions,
)


def _session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _key_auth_route(route_id: str, whitelist: list[str] | None = None) -> dict:
    route: dict = {"id": route_id, "uri": f"/{route_id}/*", "plugins": {"key-auth": {}}}
    if whitelist is not None:
        route["plugins"]["consumer-restriction"] = {"whitelist": list(whitelist)}
    return route


def _apisix_stub(routes: list[dict], *, fail_on: set[str] | None = None) -> MagicMock:
    """Stub APISIX client backed by an in-memory route table."""
    state = {route["id"]: json.loads(json.dumps(route)) for route in routes}
    failing = fail_on or set()

    async def list_resources(resource_type):
        assert resource_type == "routes"
        return {"items": [json.loads(json.dumps(r)) for r in state.values()]}

    async def put_resource(resource_type, resource_id, body):
        assert resource_type == "routes"
        if resource_id in failing:
            raise RuntimeError(f"apisix rejected {resource_id}")
        state[resource_id] = {"id": resource_id, **body}
        return state[resource_id]

    stub = MagicMock()
    stub.list_resources = AsyncMock(side_effect=list_resources)
    stub.put_resource = AsyncMock(side_effect=put_resource)
    stub.state = state
    return stub


def _whitelist(stub: MagicMock, route_id: str) -> list[str]:
    return stub.state[route_id]["plugins"]["consumer-restriction"]["whitelist"]


async def _add_keys(engine, *keys: ApiKeyAccess) -> None:
    async with _session_factory(engine)() as db:
        for key in keys:
            db.add(key)
        await db.commit()


def _key(
    name: str,
    routes: list[str] | None,
    *,
    expires_in_days: float | None = None,
    raw_routes: str | None = None,
) -> ApiKeyAccess:
    expires_at = (
        None
        if expires_in_days is None
        else datetime.now(timezone.utc) + timedelta(days=expires_in_days)
    )
    return ApiKeyAccess(
        consumer_name=name,
        allowed_routes=raw_routes if raw_routes is not None else json.dumps(routes or []),
        allowed_databases=json.dumps([MASTER_ACCESS] if routes == [MASTER_ACCESS] else []),
        expires_at=expires_at,
    )


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_expand_allowed_routes_implies_the_converter_routes():
    assert cr.expand_allowed_routes(["llm-proxy"]) == {
        "llm-proxy", "llm-messages", "llm-responses", "llm-models",
    }


def test_expand_allowed_routes_is_one_directional():
    """A discovery-only key must not gain the raw proxy."""
    assert cr.expand_allowed_routes(["llm-models"]) == {"llm-models"}


def test_is_expired_reads_a_naive_timestamp_as_utc():
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    assert cr.is_expired(ApiKeyAccess(consumer_name="c", expires_at=past)) is True


def test_is_expired_is_false_without_an_expiry():
    assert cr.is_expired(ApiKeyAccess(consumer_name="c", expires_at=None)) is False


def test_effective_allowed_routes_is_empty_once_expired():
    expired = _key("c", ["query-api"], expires_in_days=-1)
    live = _key("c", ["query-api"], expires_in_days=1)
    assert cr.effective_allowed_routes(expired) == []
    assert cr.effective_allowed_routes(live) == ["query-api"]


def test_effective_allowed_routes_propagates_a_malformed_row():
    with pytest.raises(ValueError):
        cr.effective_allowed_routes(_key("c", None, raw_routes=json.dumps({"a": 1})))


# ── Reconcile ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_consumer_is_removed_from_every_key_auth_route(engine):
    await _add_keys(engine, _key("lapsed", ["query-api", "llm-proxy"], expires_in_days=-1))
    stub = _apisix_stub([
        _key_auth_route("query-api", ["lapsed", "other"]),
        _key_auth_route("llm-proxy", ["lapsed"]),
        _key_auth_route("llm-messages", ["lapsed"]),
    ])

    async with _session_factory(engine)() as db:
        result = await reconcile_consumer_route_restrictions(db, client=stub)

    assert _whitelist(stub, "query-api") == ["other"]
    assert _whitelist(stub, "llm-proxy") == [DENY_ALL_CONSUMER]
    assert _whitelist(stub, "llm-messages") == [DENY_ALL_CONSUMER]
    assert result.revoked_consumers == {"lapsed"}
    assert sorted(result.routes_changed) == ["llm-messages", "llm-proxy", "query-api"]


@pytest.mark.asyncio
async def test_consumers_unknown_to_the_database_are_left_alone(engine):
    """Hand-added or externally provisioned consumers are not ours to prune."""
    await _add_keys(engine, _key("ours", ["query-api"]))
    stub = _apisix_stub([_key_auth_route("query-api", ["stranger"])])

    async with _session_factory(engine)() as db:
        await reconcile_consumer_route_restrictions(db, client=stub)

    assert _whitelist(stub, "query-api") == ["ours", "stranger"]


@pytest.mark.asyncio
async def test_emptied_whitelist_gets_the_deny_all_sentinel(engine):
    """An empty consumer-restriction means 'any consumer' to APISIX."""
    await _add_keys(engine, _key("lapsed", ["query-api"], expires_in_days=-1))
    stub = _apisix_stub([_key_auth_route("query-api", ["lapsed"])])

    async with _session_factory(engine)() as db:
        await reconcile_consumer_route_restrictions(db, client=stub)

    assert _whitelist(stub, "query-api") == [DENY_ALL_CONSUMER]


@pytest.mark.asyncio
async def test_a_live_consumer_missing_from_its_route_is_added_back(engine):
    """The etcd-reset recovery path: routes exist, whitelists lost their names."""
    await _add_keys(engine, _key("live", ["query-api"], expires_in_days=10))
    stub = _apisix_stub([_key_auth_route("query-api", [DENY_ALL_CONSUMER])])

    async with _session_factory(engine)() as db:
        await reconcile_consumer_route_restrictions(db, client=stub)

    assert _whitelist(stub, "query-api") == ["live"]


@pytest.mark.asyncio
async def test_only_routes_whose_whitelist_changed_are_put(engine):
    await _add_keys(engine, _key("live", ["query-api"]))
    stub = _apisix_stub([
        _key_auth_route("query-api", ["live"]),          # already correct
        _key_auth_route("s3-api", [DENY_ALL_CONSUMER]),  # already correct
        _key_auth_route("nas-api", ["live"]),            # must lose "live"
    ])

    async with _session_factory(engine)() as db:
        result = await reconcile_consumer_route_restrictions(db, client=stub)

    assert stub.put_resource.await_count == 1
    assert stub.list_resources.await_count == 1
    assert result.routes_changed == ["nas-api"]
    assert _whitelist(stub, "nas-api") == [DENY_ALL_CONSUMER]


@pytest.mark.asyncio
async def test_master_wildcard_covers_every_key_auth_route(engine):
    await _add_keys(engine, _key("master-app", [MASTER_ACCESS]))
    stub = _apisix_stub([
        _key_auth_route("query-api", []),
        _key_auth_route("nas-api", [DENY_ALL_CONSUMER]),
        _key_auth_route("llm-metrics", []),
    ])

    async with _session_factory(engine)() as db:
        await reconcile_consumer_route_restrictions(db, client=stub)

    for route_id in ("query-api", "nas-api", "llm-metrics"):
        assert _whitelist(stub, route_id) == ["master-app"], route_id


@pytest.mark.asyncio
async def test_llm_proxy_implies_the_converter_routes_but_never_llm_metrics(engine):
    await _add_keys(engine, _key("llm-user", ["llm-proxy"]))
    stub = _apisix_stub([
        _key_auth_route("llm-proxy", []),
        _key_auth_route("llm-messages", []),
        _key_auth_route("llm-responses", []),
        _key_auth_route("llm-models", []),
        _key_auth_route("llm-metrics", []),
        _key_auth_route("query-api", []),
    ])

    async with _session_factory(engine)() as db:
        await reconcile_consumer_route_restrictions(db, client=stub)

    for route_id in ("llm-proxy", "llm-messages", "llm-responses", "llm-models"):
        assert _whitelist(stub, route_id) == ["llm-user"], route_id
    # llm-metrics carries every key's usage — never implied.
    for route_id in ("llm-metrics", "query-api"):
        assert _whitelist(stub, route_id) == [DENY_ALL_CONSUMER], route_id


@pytest.mark.asyncio
async def test_malformed_row_keeps_its_whitelist_and_is_reported(engine, caplog):
    """A storage bug must not revoke a working key."""
    await _add_keys(
        engine,
        _key("bad-app", None, raw_routes="not-json"),
        _key("good-app", ["query-api"]),
    )
    stub = _apisix_stub([_key_auth_route("query-api", ["bad-app"])])

    with caplog.at_level(logging.WARNING, logger="app.services.consumer_restrictions"):
        async with _session_factory(engine)() as db:
            result = await reconcile_consumer_route_restrictions(db, client=stub)

    assert result.skipped_malformed == ["bad-app"]
    assert _whitelist(stub, "query-api") == ["bad-app", "good-app"]
    assert "bad-app" in caplog.text


@pytest.mark.asyncio
async def test_one_failing_put_still_attempts_the_rest_then_raises(engine):
    await _add_keys(engine, _key("lapsed", ["query-api", "s3-api"], expires_in_days=-1))
    stub = _apisix_stub(
        [
            _key_auth_route("query-api", ["lapsed"]),
            _key_auth_route("s3-api", ["lapsed"]),
        ],
        fail_on={"query-api"},
    )

    with pytest.raises(RuntimeError) as exc_info:
        async with _session_factory(engine)() as db:
            await reconcile_consumer_route_restrictions(db, client=stub)

    assert stub.put_resource.await_count == 2
    assert exc_info.value.result.failed_routes == ["query-api"]
    assert exc_info.value.result.routes_changed == ["s3-api"]
    assert "query-api" in str(exc_info.value)
    # The route that could be fixed was fixed.
    assert _whitelist(stub, "s3-api") == [DENY_ALL_CONSUMER]


@pytest.mark.asyncio
async def test_routes_without_key_auth_are_ignored(engine):
    await _add_keys(engine, _key("master-app", [MASTER_ACCESS]))
    stub = _apisix_stub([
        {"id": "public", "uri": "/public/*", "plugins": {}},
        {"id": "jwt-only", "uri": "/jwt/*", "plugins": {"jwt-auth": {}}},
    ])

    async with _session_factory(engine)() as db:
        result = await reconcile_consumer_route_restrictions(db, client=stub)

    stub.put_resource.assert_not_awaited()
    assert result.routes_changed == []
    assert "consumer-restriction" not in stub.state["public"]["plugins"]


@pytest.mark.asyncio
async def test_no_keys_means_no_apisix_call_at_all(engine):
    """The boot replay fails startup on an exception, so an install with no keys
    must not make startup depend on APISIX being reachable."""
    stub = _apisix_stub([_key_auth_route("query-api", ["stranger"])])

    async with _session_factory(engine)() as db:
        result = await reconcile_consumer_route_restrictions(db, client=stub)

    stub.list_resources.assert_not_awaited()
    stub.put_resource.assert_not_awaited()
    assert result == cr.ReconcileResult()


@pytest.mark.asyncio
async def test_a_route_without_an_id_is_skipped(engine):
    await _add_keys(engine, _key("master-app", [MASTER_ACCESS]))
    stub = MagicMock()
    stub.list_resources = AsyncMock(
        return_value={"items": [{"uri": "/no-id", "plugins": {"key-auth": {}}}]}
    )
    stub.put_resource = AsyncMock()

    async with _session_factory(engine)() as db:
        await reconcile_consumer_route_restrictions(db, client=stub)

    stub.put_resource.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_honours_an_injected_now(engine):
    """``now`` in the future expires a key that is still live today."""
    await _add_keys(engine, _key("soon", ["query-api"], expires_in_days=1))
    stub = _apisix_stub([_key_auth_route("query-api", ["soon"])])

    async with _session_factory(engine)() as db:
        result = await reconcile_consumer_route_restrictions(
            db, now=datetime.now(timezone.utc) + timedelta(days=2), client=stub
        )

    assert result.revoked_consumers == {"soon"}
    assert _whitelist(stub, "query-api") == [DENY_ALL_CONSUMER]


@pytest.mark.asyncio
async def test_a_steady_state_writes_nothing_and_logs_nothing(engine, caplog):
    await _add_keys(engine, _key("live", ["query-api"]))
    stub = _apisix_stub([
        _key_auth_route("query-api", ["live"]),
        _key_auth_route("s3-api", [DENY_ALL_CONSUMER]),
    ])

    with caplog.at_level(logging.INFO, logger="app.services.consumer_restrictions"):
        async with _session_factory(engine)() as db:
            result = await reconcile_consumer_route_restrictions(db, client=stub)

    stub.put_resource.assert_not_awaited()
    assert result == cr.ReconcileResult()
    assert caplog.text == ""


# ── Loop ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_waits_before_the_first_run_then_repeats():
    sleeps: list[float] = []
    once = AsyncMock(return_value=cr.ReconcileResult())

    async def _fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    with patch("app.services.consumer_restrictions.asyncio.sleep", _fake_sleep), \
         patch("app.services.consumer_restrictions.is_active_instance",
               new=AsyncMock(return_value=True)), \
         patch("app.services.consumer_restrictions.run_expiry_reconcile_once", once):
        with pytest.raises(asyncio.CancelledError):
            await cr.run_expiry_reconcile_loop(
                interval_seconds=300, first_delay_seconds=30
            )

    assert sleeps[0] == 30
    assert sleeps[1] == 300
    assert once.await_count == 2


@pytest.mark.asyncio
async def test_loop_makes_no_apisix_call_on_the_standby_color(engine):
    """Blue/green share one APISIX — only the active color may rewrite routes."""
    sleeps = 0

    async def _fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    apisix = MagicMock()
    apisix.list_resources = AsyncMock(return_value={"items": []})
    apisix.put_resource = AsyncMock()

    with patch("app.services.consumer_restrictions.asyncio.sleep", _fake_sleep), \
         patch("app.services.consumer_restrictions.apisix_client", apisix), \
         patch("app.database.async_session", _session_factory(engine)), \
         patch("app.services.consumer_restrictions.is_active_instance",
               new=AsyncMock(return_value=False)):
        with pytest.raises(asyncio.CancelledError):
            await cr.run_expiry_reconcile_loop()

    apisix.list_resources.assert_not_awaited()
    apisix.put_resource.assert_not_awaited()
    # The loop stays alive and keeps re-checking, so a promote is picked up.
    assert sleeps == 3


@pytest.mark.asyncio
async def test_loop_reconciles_through_the_module_client_on_the_active_color(engine):
    await _add_keys(engine, _key("lapsed", ["query-api"], expires_in_days=-1))
    stub = _apisix_stub([_key_auth_route("query-api", ["lapsed"])])
    sleeps = 0

    async def _fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            raise asyncio.CancelledError

    with patch("app.services.consumer_restrictions.asyncio.sleep", _fake_sleep), \
         patch("app.services.consumer_restrictions.apisix_client", stub), \
         patch("app.database.async_session", _session_factory(engine)), \
         patch("app.services.consumer_restrictions.is_active_instance",
               new=AsyncMock(return_value=True)):
        with pytest.raises(asyncio.CancelledError):
            await cr.run_expiry_reconcile_loop()

    assert _whitelist(stub, "query-api") == [DENY_ALL_CONSUMER]


@pytest.mark.asyncio
async def test_loop_survives_a_failing_cycle(caplog):
    sleeps = 0

    async def _fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    once = AsyncMock(side_effect=[RuntimeError("apisix down"), cr.ReconcileResult()])

    with patch("app.services.consumer_restrictions.asyncio.sleep", _fake_sleep), \
         patch("app.services.consumer_restrictions.is_active_instance",
               new=AsyncMock(return_value=True)), \
         patch("app.services.consumer_restrictions.run_expiry_reconcile_once", once):
        with pytest.raises(asyncio.CancelledError):
            await cr.run_expiry_reconcile_loop()

    assert once.await_count == 2
    assert "API key expiry reconcile cycle failed" in caplog.text


# ── Master lookup ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_master_consumer_names_excludes_an_expired_master(engine):
    """A new key-auth route must not be born whitelisting a lapsed master key."""
    from app.routers.api_keys import list_master_consumer_names

    await _add_keys(
        engine,
        ApiKeyAccess(
            consumer_name="live-master",
            allowed_databases=json.dumps([MASTER_ACCESS]),
            allowed_routes=json.dumps([MASTER_ACCESS]),
        ),
        ApiKeyAccess(
            consumer_name="lapsed-master",
            allowed_databases=json.dumps([MASTER_ACCESS]),
            allowed_routes=json.dumps([MASTER_ACCESS]),
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        ),
    )

    async with _session_factory(engine)() as db:
        assert await list_master_consumer_names(db) == ["live-master"]
