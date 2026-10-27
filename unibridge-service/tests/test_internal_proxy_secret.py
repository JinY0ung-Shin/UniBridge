"""APISIX_INTERNAL_PROXY_SECRET: fail-fast config and the boot header reconcile.

The secret used to fall back to APISIX_ADMIN_KEY, which published the
highest-privilege gateway credential into etcd route configs and onto every
proxied request. It is now a required, dedicated value — which only works if a
rotated value can still reach APISIX on a color that boots with
APISIX_PROVISION_ON_START=false.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from app.main import (
    APISIX_INTERNAL_PROXY_HEADER,
    INTERNAL_PROXY_ROUTE_IDS,
    lifespan,
)
from tests.test_main import _DummyTask, _fake_get_db, _keyed_get_resource


# ── validate_settings ──────────────────────────────────────────────────────


def test_validate_settings_requires_internal_proxy_secret(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "APISIX_INTERNAL_PROXY_SECRET", "")

    with pytest.raises(RuntimeError, match="APISIX_INTERNAL_PROXY_SECRET"):
        cfg.validate_settings()


def test_validate_settings_warns_when_internal_proxy_secret_equals_admin_key(
    monkeypatch, caplog
):
    """Reusing the admin key is warned about, not refused.

    Deployments that relied on the old fallback start out in exactly this state,
    and refusing to boot would take them down on the deploy that upgrades them.
    """
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "APISIX_ADMIN_KEY", "shared-secret")
    monkeypatch.setattr(cfg.settings, "APISIX_INTERNAL_PROXY_SECRET", "shared-secret")

    with caplog.at_level(logging.WARNING, logger="app.config"):
        cfg.validate_settings()

    assert "APISIX_INTERNAL_PROXY_SECRET equals APISIX_ADMIN_KEY" in caplog.text


def test_validate_settings_silent_when_internal_proxy_secret_is_dedicated(
    monkeypatch, caplog
):
    from app import config as cfg

    monkeypatch.setattr(cfg.settings, "APISIX_ADMIN_KEY", "admin-secret")
    monkeypatch.setattr(cfg.settings, "APISIX_INTERNAL_PROXY_SECRET", "proxy-secret")

    with caplog.at_level(logging.WARNING, logger="app.config"):
        cfg.validate_settings()

    assert "APISIX_INTERNAL_PROXY_SECRET" not in caplog.text


# ── Boot header reconcile ──────────────────────────────────────────────────


def _lifespan_patches(settings_ns, get_resource, put_resource, sleep=None):
    """Patches that let ``lifespan`` run with no real APISIX, DB, or network."""
    patches = [
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", settings_ns),
        patch("app.services.apisix_client.get_resource", get_resource),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch(
            "app.services.apisix_client.list_resources",
            new=AsyncMock(return_value={"items": []}),
        ),
        patch(
            "app.main.api_keys.sync_all_consumer_route_restrictions", new=AsyncMock()
        ),
        patch(
            "app.services.alert_checker.start_checker",
            new=AsyncMock(return_value=_DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ]
    if sleep is not None:
        patches.append(patch("asyncio.sleep", sleep))
    return patches


def _steady_state_settings(secret: str) -> SimpleNamespace:
    """A promoted blue/green color: provisioning off, secret configured."""
    return SimpleNamespace(
        APISIX_PROVISION_ON_START=False,
        APISIX_INTERNAL_PROXY_SECRET=secret,
        APISIX_ADMIN_KEY="admin-secret",
    )


def _system_route(route_id: str, header_value: str | None) -> dict:
    """An existing APISIX route as the admin API returns it."""
    plugins: dict[str, object] = {
        "key-auth": {},
        "consumer-restriction": {"whitelist": [f"{route_id}-consumer"]},
    }
    if header_value is not None:
        plugins["proxy-rewrite"] = {
            "regex_uri": ["^/api/x(.*)", "/x$1"],
            "use_real_request_uri_unsafe": True,
            "headers": {"set": {APISIX_INTERNAL_PROXY_HEADER: header_value}},
        }
    return {
        "id": route_id,
        "name": route_id,
        "uri": f"/api/{route_id}/*",
        "upstream_id": "unibridge-service",
        "plugins": plugins,
        "status": 1,
        "create_time": 1700000000,
        "update_time": 1700000001,
    }


def _route_puts(put_resource: AsyncMock) -> dict[str, dict]:
    return {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    }


async def _run_lifespan(settings_ns, get_resource, put_resource, sleep=None) -> None:
    app = FastAPI()
    with ExitStack() as stack:
        for p in _lifespan_patches(settings_ns, get_resource, put_resource, sleep):
            stack.enter_context(p)
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_boot_rewrites_stale_internal_proxy_header_without_provisioning():
    """The rotation path: old value in etcd, new value in env, provisioning off."""
    put_resource = AsyncMock()
    get_resource = _keyed_get_resource(
        {
            ("routes", route_id): _system_route(route_id, "old-secret")
            for route_id in INTERNAL_PROXY_ROUTE_IDS
        }
    )

    await _run_lifespan(
        _steady_state_settings("new-secret"), get_resource, put_resource
    )

    route_puts = _route_puts(put_resource)
    assert sorted(route_puts) == sorted(INTERNAL_PROXY_ROUTE_IDS)
    for route_id, body in route_puts.items():
        proxy_rewrite = body["plugins"]["proxy-rewrite"]
        assert proxy_rewrite["headers"]["set"][APISIX_INTERNAL_PROXY_HEADER] == (
            "new-secret"
        )
        # GET-mutate-PUT: only the header value changes, so per-key access rules
        # and the rewrite config survive a rotation.
        assert body["plugins"]["consumer-restriction"] == {
            "whitelist": [f"{route_id}-consumer"]
        }
        assert proxy_rewrite["regex_uri"] == ["^/api/x(.*)", "/x$1"]
        assert proxy_rewrite["use_real_request_uri_unsafe"] is True
        assert body["upstream_id"] == "unibridge-service"
        # Server-managed metadata is not part of a config PUT.
        assert "id" not in body
        assert "create_time" not in body
        assert "update_time" not in body


@pytest.mark.asyncio
async def test_boot_leaves_matching_internal_proxy_header_untouched():
    put_resource = AsyncMock()
    get_resource = _keyed_get_resource(
        {
            ("routes", route_id): _system_route(route_id, "proxy-secret")
            for route_id in INTERNAL_PROXY_ROUTE_IDS
        }
    )

    await _run_lifespan(
        _steady_state_settings("proxy-secret"), get_resource, put_resource
    )

    assert _route_puts(put_resource) == {}
    assert get_resource.await_count == len(INTERNAL_PROXY_ROUTE_IDS)


@pytest.mark.asyncio
async def test_boot_adds_internal_proxy_header_to_route_that_lacks_it():
    """A route provisioned before the header existed is repaired, not skipped.

    Without the header APISIX proxies requests the app then rejects as untrusted,
    so leaving it alone would keep the route permanently broken.
    """
    put_resource = AsyncMock()
    get_resource = _keyed_get_resource(
        {("routes", "s3-api"): _system_route("s3-api", None)}
    )

    await _run_lifespan(
        _steady_state_settings("proxy-secret"), get_resource, put_resource
    )

    route_puts = _route_puts(put_resource)
    assert list(route_puts) == ["s3-api"]
    plugins = route_puts["s3-api"]["plugins"]
    assert plugins["proxy-rewrite"]["headers"]["set"] == {
        APISIX_INTERNAL_PROXY_HEADER: "proxy-secret"
    }
    assert plugins["consumer-restriction"] == {"whitelist": ["s3-api-consumer"]}


@pytest.mark.asyncio
async def test_boot_skips_internal_proxy_routes_that_do_not_exist_yet():
    put_resource = AsyncMock()
    get_resource = _keyed_get_resource({})

    await _run_lifespan(
        _steady_state_settings("proxy-secret"), get_resource, put_resource
    )

    assert _route_puts(put_resource) == {}
    # Every route was looked up — "no PUTs" here means "nothing to update", not
    # "the reconcile never ran".
    assert get_resource.await_count == len(INTERNAL_PROXY_ROUTE_IDS)


@pytest.mark.asyncio
async def test_boot_fails_when_internal_proxy_reconcile_cannot_reach_apisix():
    """An unreachable admin API may be hiding a rotation, so fail startup."""
    put_resource = AsyncMock()
    get_resource = AsyncMock(side_effect=RuntimeError("APISIX admin unavailable"))
    sleep = AsyncMock()

    with pytest.raises(RuntimeError, match="APISIX admin unavailable"):
        await _run_lifespan(
            _steady_state_settings("proxy-secret"),
            get_resource,
            put_resource,
            sleep=sleep,
        )

    assert get_resource.await_count == 10
    assert sleep.await_args_list == [
        ((2,),), ((4,),), ((8,),), ((15,),), ((15,),),
        ((15,),), ((15,),), ((15,),), ((15,),),
    ]
    assert _route_puts(put_resource) == {}


@pytest.mark.asyncio
async def test_internal_proxy_route_ids_match_the_provisioned_routes():
    """Drift guard for INTERNAL_PROXY_ROUTE_IDS.

    A route provisioned with the header but missing from the list would never be
    rotated; one in the list that does not carry it would be rewritten for no
    reason.
    """
    put_resource = AsyncMock()

    await _run_lifespan(
        SimpleNamespace(
            BIFROST_TEST_VK="sk-bf-" + "v" * 32,
            APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
            APISIX_ADMIN_KEY="admin-secret",
        ),
        AsyncMock(side_effect=RuntimeError("404 not found")),
        put_resource,
    )

    header_carrying = {
        route_id
        for route_id, body in _route_puts(put_resource).items()
        if APISIX_INTERNAL_PROXY_HEADER
        in body.get("plugins", {})
        .get("proxy-rewrite", {})
        .get("headers", {})
        .get("set", {})
    }
    assert header_carrying == set(INTERNAL_PROXY_ROUTE_IDS)


# ── Only the active color writes the shared header ──────────────────────────


@pytest.mark.asyncio
async def test_boot_on_the_standby_color_leaves_the_header_alone():
    """A restarted standby or rollback target may hold an older secret; writing
    it would 401 the active color's API-key traffic."""
    put_resource = AsyncMock()
    get_resource = AsyncMock(
        side_effect=lambda kind, rid: _system_route(rid, "old-secret")
    )

    with patch("app.main.is_active_instance", new=AsyncMock(return_value=False)):
        await _run_lifespan(
            _steady_state_settings("new-secret"), get_resource, put_resource
        )

    assert _route_puts(put_resource) == {}


@pytest.mark.asyncio
async def test_reconcile_loop_writes_only_while_active():
    from app.main import _run_internal_proxy_reconcile_loop

    sleeps = 0

    async def fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 3:
            raise asyncio.CancelledError

    reconcile = AsyncMock(side_effect=[RuntimeError("APISIX down"), None])
    active = AsyncMock(side_effect=[False, True, True])
    with (
        patch("asyncio.sleep", fake_sleep),
        patch("app.main.is_active_instance", active),
        patch("app.main._reconcile_internal_proxy_header", reconcile),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _run_internal_proxy_reconcile_loop(interval_seconds=0)

    # standby cycle skipped; a failed cycle is retried on the next one
    assert active.await_count == 3
    assert reconcile.await_count == 2


@pytest.mark.asyncio
async def test_previous_secret_is_accepted_during_a_rotation(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from app.auth import get_current_user_or_apikey, settings

    monkeypatch.setattr(settings, "ENABLE_DEV_TOKEN_ENDPOINT", False)
    monkeypatch.setattr(settings, "APISIX_INTERNAL_PROXY_SECRET", "new-secret")
    monkeypatch.setattr(settings, "APISIX_INTERNAL_PROXY_SECRET_PREVIOUS", "old-secret")

    access = MagicMock(
        consumer_name="my-app-key",
        allowed_databases="[]",
        allowed_routes="[]",
        allow_insert=False,
        allow_update=False,
        allow_delete=False,
        allowed_tables=None,
        expires_at=None,
    )
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=access))

    for presented in ("new-secret", "old-secret"):
        request = MagicMock()
        request.headers = {
            "x-consumer-username": "my-app-key",
            APISIX_INTERNAL_PROXY_HEADER.lower(): presented,
        }
        user = await get_current_user_or_apikey(request=request, credentials=None, db=db)
        assert user.consumer_name == "my-app-key"

    request = MagicMock()
    request.headers = {
        "x-consumer-username": "my-app-key",
        APISIX_INTERNAL_PROXY_HEADER.lower(): "some-other-secret",
    }
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_apikey(request=request, credentials=None, db=db)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_empty_previous_secret_accepts_nothing_extra(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from app.auth import get_current_user_or_apikey, settings

    monkeypatch.setattr(settings, "ENABLE_DEV_TOKEN_ENDPOINT", False)
    monkeypatch.setattr(settings, "APISIX_INTERNAL_PROXY_SECRET", "new-secret")
    monkeypatch.setattr(settings, "APISIX_INTERNAL_PROXY_SECRET_PREVIOUS", "")

    request = MagicMock()
    request.headers = {"x-consumer-username": "my-app-key", APISIX_INTERNAL_PROXY_HEADER.lower(): ""}
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_apikey(request=request, credentials=None, db=AsyncMock())
    assert exc_info.value.status_code == 401
