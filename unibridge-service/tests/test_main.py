from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from app.main import lifespan


class _DummyTask:
    def cancel(self) -> None:
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class _FakeDb:
    async def execute(self, _query):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )


async def _fake_get_db():
    yield _FakeDb()


@pytest.mark.asyncio
async def test_lifespan_provisions_llm_admin_route_when_master_key_set():
    app = FastAPI()
    put_resource = AsyncMock()

    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", SimpleNamespace(LITELLM_MASTER_KEY="sk-test")),
        patch("app.services.apisix_client.get_resource", AsyncMock(side_effect=RuntimeError("not found"))),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch(
            "app.services.alert_checker.start_checker",
            new=AsyncMock(return_value=_DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(app):
            pass

    upstream_calls = {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == "upstreams"
    }
    route_calls = {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    }

    assert "litellm" in upstream_calls
    assert upstream_calls["litellm"]["scheme"] == "https"
    assert "llm-proxy" in route_calls
    assert "llm-admin" in route_calls
    assert route_calls["llm-admin"]["uri"] == "/api/llm-admin/*"
    assert route_calls["llm-admin"]["plugins"]["proxy-rewrite"]["regex_uri"] == [
        "^/api/llm-admin(.*)",
        "$1",
    ]


@pytest.mark.asyncio
async def test_lifespan_preserves_consumer_restriction_for_protected_routes():
    app = FastAPI()
    put_resource = AsyncMock()
    get_resource = AsyncMock(
        side_effect=[
            {
                "id": "query-api",
                "name": "query-api",
                "uri": "/api/query/*",
                "methods": ["POST", "GET"],
                "upstream_id": "unibridge-service",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["query-consumer"]},
                },
                "status": 1,
            },
            {
                "id": "s3-api",
                "name": "s3-api",
                "uri": "/api/s3/*",
                "methods": ["GET"],
                "upstream_id": "unibridge-service",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["s3-consumer"]},
                },
                "status": 1,
            },
            {
                "id": "llm-proxy",
                "name": "llm-proxy",
                "uri": "/api/llm/*",
                "methods": ["POST", "GET", "PUT", "DELETE", "OPTIONS"],
                "upstream_id": "litellm",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["llm-consumer"]},
                },
                "status": 1,
            },
        ]
    )

    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", SimpleNamespace(LITELLM_MASTER_KEY="sk-test")),
        patch("app.services.apisix_client.get_resource", get_resource),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch(
            "app.services.alert_checker.start_checker",
            new=AsyncMock(return_value=_DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(app):
            pass

    route_calls = {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    }

    assert route_calls["query-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["query-consumer"]
    }
    assert route_calls["s3-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["s3-consumer"]
    }
    assert route_calls["llm-proxy"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["llm-consumer"]
    }


@pytest.mark.asyncio
async def test_lifespan_treats_missing_protected_routes_as_first_boot_creation():
    app = FastAPI()
    put_resource = AsyncMock()
    get_resource = AsyncMock(
        side_effect=[RuntimeError("route not found"), RuntimeError("404 s3 route missing"), RuntimeError("404 route missing")]
    )

    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", SimpleNamespace(LITELLM_MASTER_KEY="sk-test")),
        patch("app.services.apisix_client.get_resource", get_resource),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch(
            "app.main.api_keys.sync_all_consumer_route_restrictions",
            new=AsyncMock(),
        ),
        patch(
            "app.services.alert_checker.start_checker",
            new=AsyncMock(return_value=_DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(app):
            pass

    route_calls = {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    }
    assert "consumer-restriction" not in route_calls["query-api"]["plugins"]
    assert "consumer-restriction" not in route_calls["s3-api"]["plugins"]
    assert "consumer-restriction" not in route_calls["llm-proxy"]["plugins"]


@pytest.mark.asyncio
async def test_lifespan_retries_when_protected_route_state_lookup_fails():
    app = FastAPI()
    put_resource = AsyncMock()
    get_resource = AsyncMock(side_effect=RuntimeError("APISIX unavailable"))
    sleep = AsyncMock()

    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", SimpleNamespace(LITELLM_MASTER_KEY="")),
        patch("app.services.apisix_client.get_resource", get_resource),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch("asyncio.sleep", sleep),
        patch(
            "app.services.alert_checker.start_checker",
            new=AsyncMock(return_value=_DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        with pytest.raises(RuntimeError, match="APISIX unavailable"):
            async with lifespan(app):
                pass

    route_ids = [
        call.args[1]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    ]

    assert get_resource.await_count == 5
    assert sleep.await_args_list == [((2,),), ((4,),), ((8,),), ((16,),)]
    assert "query-api" not in route_ids
    assert "llm-proxy" not in route_ids
    assert "llm-admin" not in route_ids


@pytest.mark.asyncio
async def test_lifespan_skips_litellm_routes_when_master_key_missing():
    app = FastAPI()
    put_resource = AsyncMock()

    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", SimpleNamespace(LITELLM_MASTER_KEY="")),
        patch("app.services.apisix_client.get_resource", AsyncMock(side_effect=RuntimeError("not found"))),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch(
            "app.services.alert_checker.start_checker",
            new=AsyncMock(return_value=_DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(app):
            pass

    route_ids = [
        call.args[1]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    ]

    assert "query-api" in route_ids
    assert "llm-proxy" not in route_ids
    assert "llm-admin" not in route_ids


@pytest.mark.asyncio
async def test_lifespan_replays_api_key_route_restrictions_after_provisioning_with_fresh_db_block():
    app = FastAPI()
    events: list[tuple[str, str]] = []

    class _ConnectionsDb:
        async def execute(self, _query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    class _S3Db:
        async def execute(self, _query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    class _SettingsDb:
        pass

    class _ReplayDb:
        pass

    async def fake_get_db_sequence():
        yield next(db_iter)

    async def put_resource(resource_type, resource_id, body):
        del body
        events.append((resource_type, resource_id))

    replay_route_restrictions = AsyncMock(
        side_effect=lambda db: events.append(("replay", db.__class__.__name__))
    )
    db_iter = iter([_ConnectionsDb(), _S3Db(), _SettingsDb(), _ReplayDb()])

    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: fake_get_db_sequence()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", SimpleNamespace(LITELLM_MASTER_KEY="sk-test")),
        patch("app.services.apisix_client.get_resource", AsyncMock(side_effect=RuntimeError("not found"))),
        patch("app.services.apisix_client.put_resource", new=AsyncMock(side_effect=put_resource)),
        patch(
            "app.main.api_keys.sync_all_consumer_route_restrictions",
            replay_route_restrictions,
        ),
        patch(
            "app.services.alert_checker.start_checker",
            new=AsyncMock(return_value=_DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(app):
            pass

    assert replay_route_restrictions.await_count == 1
    db_arg = replay_route_restrictions.await_args.args[0]
    assert isinstance(db_arg, _ReplayDb)
    assert events[-1] == ("replay", "_ReplayDb")
    assert events.index(("routes", "query-api")) < events.index(("replay", "_ReplayDb"))
    assert events.index(("routes", "s3-api")) < events.index(("replay", "_ReplayDb"))
    assert events.index(("routes", "llm-proxy")) < events.index(("replay", "_ReplayDb"))
    assert events.index(("routes", "llm-admin")) < events.index(("replay", "_ReplayDb"))
