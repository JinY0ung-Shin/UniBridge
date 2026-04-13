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
