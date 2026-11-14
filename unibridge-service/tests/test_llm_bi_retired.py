"""What is left of /api/llm-bi, the Bifrost side-by-side test.

Pinned here:

1. Every provisioning boot installs llm-bi-not-found, a 404 that points to
   /api/llm; APISIX refusing that route never keeps the service from booting.
2. The old ids stay system resources and /api/llm-bi stays a system namespace,
   so no custom route takes them; the OpenAPI export leaves the 404 route out.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.main import _put_not_found_route, lifespan
from app.routers.gateway import _SYSTEM_ROUTE_URIS, _shadowed_system_uri
from app.services import llm_routes
from app.services.apisix_system_resources import (
    LLM_BI_NOT_FOUND_ROUTE_ID,
    PROTECTED_ROUTE_IDS,
    PROTECTED_UPSTREAM_IDS,
    RETIRED_LLM_BI_ROUTE_IDS,
    RETIRED_LLM_BI_UPSTREAM_ID,
)
from app.services.openapi_export import build_openapi_spec
from tests.test_llm_routes import VK, _FakeApisix, _status_error
from tests.test_main import _DummyTask, _fake_get_db, _keyed_get_resource


async def _put_404_route(fake: _FakeApisix) -> None:
    with patch("app.services.apisix_client.put_resource", fake.put_resource):
        await _put_not_found_route(
            LLM_BI_NOT_FOUND_ROUTE_ID, llm_routes.retired_llm_bi_route(), "/api/llm-bi"
        )


def test_the_404_route_points_to_api_llm() -> None:
    body = llm_routes.retired_llm_bi_route()

    assert body["name"] == LLM_BI_NOT_FOUND_ROUTE_ID
    assert body["uri"] == "/api/llm-bi/*"
    # Proxies nothing, and stays out of the consumer-restriction reconciler,
    # which manages the whitelist of every key-auth route.
    assert "upstream_id" not in body and "upstream" not in body
    assert set(body["plugins"]) == {"consumer-restriction"}
    restriction = body["plugins"]["consumer-restriction"]
    assert restriction["type"] == "route_id"
    assert restriction["blacklist"] == [LLM_BI_NOT_FOUND_ROUTE_ID]
    assert restriction["rejected_code"] == 404
    assert "has been removed" in restriction["rejected_msg"]
    assert "/api/llm serves the same endpoints" in restriction["rejected_msg"]


def test_the_old_ids_stay_system_resources() -> None:
    assert set(RETIRED_LLM_BI_ROUTE_IDS) | {LLM_BI_NOT_FOUND_ROUTE_ID} <= PROTECTED_ROUTE_IDS
    assert {"bifrost", RETIRED_LLM_BI_UPSTREAM_ID} <= PROTECTED_UPSTREAM_IDS


@pytest.mark.parametrize(
    "uri,shadowed",
    [
        ("/api/llm-bi/v1/chat/completions", True),
        ("/api/llm-bi/*", True),
        ("/api/llm-b*", True),
        ("/api/llm-bix/*", False),
    ],
)
def test_custom_routes_cannot_take_the_llm_bi_namespace(uri: str, shadowed: bool) -> None:
    assert (_shadowed_system_uri(uri, list(_SYSTEM_ROUTE_URIS)) is not None) is shadowed


async def test_installs_the_404_route() -> None:
    fake = _FakeApisix()

    await _put_404_route(fake)

    assert fake.writes == [("PUT", "routes", LLM_BI_NOT_FOUND_ROUTE_ID)]
    assert fake.bodies[("routes", LLM_BI_NOT_FOUND_ROUTE_ID)] == llm_routes.retired_llm_bi_route()


async def test_apisix_refusing_the_404_route_does_not_block_boot(caplog) -> None:
    refused = _status_error(400, '{"error_msg":"failed to check the configuration of plugin"}')
    fake = _FakeApisix(put_errors={("routes", LLM_BI_NOT_FOUND_ROUTE_ID): refused})

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _put_404_route(fake)

    assert "APISIX refused the llm-bi-not-found route, so /api/llm-bi answers" in caplog.text


async def test_a_404_route_outage_still_reaches_the_retry_loop() -> None:
    fake = _FakeApisix(put_errors={("routes", LLM_BI_NOT_FOUND_ROUTE_ID): _status_error(503)})

    with pytest.raises(httpx.HTTPStatusError):
        await _put_404_route(fake)


async def test_lifespan_installs_it_as_part_of_boot_provisioning() -> None:
    order: list[str] = []
    put_resource = AsyncMock(side_effect=lambda resource, resource_id, body: order.append(resource_id))
    replay = AsyncMock(side_effect=lambda db: order.append("<replay>"))

    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch(
            "app.main.settings",
            SimpleNamespace(
                APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
                APISIX_ADMIN_KEY="admin-secret",
                BIFROST_TEST_VK=VK,
            ),
        ),
        patch("app.services.apisix_client.get_resource", _keyed_get_resource({})),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch("app.services.apisix_client.list_resources", AsyncMock(return_value={"items": []})),
        patch("app.main.api_keys.sync_all_consumer_route_restrictions", replay),
        patch("app.services.alert_checker.start_checker", new=AsyncMock(return_value=_DummyTask())),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(FastAPI()):
            pass

    assert order.index("llm-models") < order.index(LLM_BI_NOT_FOUND_ROUTE_ID) < order.index("<replay>")


def test_openapi_leaves_the_404_route_out() -> None:
    routes = [{"id": LLM_BI_NOT_FOUND_ROUTE_ID, **llm_routes.retired_llm_bi_route()}]

    spec = build_openapi_spec(routes, [], server_url="https://gateway.example")

    assert spec["paths"] == {}
