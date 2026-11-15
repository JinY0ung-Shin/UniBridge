"""The /api/llm routes on Bifrost (app/services/llm_routes.py) and their provisioning.

Pinned here:

1. /api/llm exposes exactly the inference, converter and metrics paths, under
   the route ids it had on LiteLLM, and injects the gateway virtual key;
   llm-not-found answers every other path.
2. The converter routes carry the virtual key and nothing of LiteLLM's.
3. Boot provisioning keeps the API-key grants of routes it rewrites and leaves
   everything alone without a usable virtual key.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.main import _provision_llm_routes
from app.routers.gateway import (
    _SYSTEM_INJECTED_HEADERS,
    _shadowed_system_uri,
)
from app.services import llm_routes
from app.services.openapi_export import build_openapi_spec
from app.services.apisix_system_resources import (
    LLM_NOT_FOUND_ROUTE_ID,
    PROTECTED_ROUTE_IDS,
    PROTECTED_UPSTREAM_IDS,
    RETIRED_LITELLM_ROUTE_ID,
    RETIRED_LITELLM_UPSTREAM_ID,
    SYSTEM_ROUTE_URIS,
)
from app.services.consumer_restrictions import DENY_ALL_CONSUMER, IMPLIED_ROUTES
from tests.conftest import auth_header
from tests.helpers import BIFROST_VK, VIRTUAL_KEY_CASES, FakeApisix, http_status_error


ON_BIFROST = [
    ("POST", "/api/llm/v1/chat/completions"),
    ("POST", "/api/llm/v1/completions"),
    ("POST", "/api/llm/v1/embeddings"),
    ("POST", "/api/llm/v1/messages"),
    ("POST", "/api/llm/v1/messages/count_tokens"),
    ("POST", "/api/llm/v1/responses"),
    ("GET", "/api/llm/v1/models"),
    ("GET", "/api/llm/metrics"),
]


def _paths(body: dict) -> list[str]:
    return body.get("uris") or [body["uri"]]


def _on_bifrost(vk: str = BIFROST_VK) -> dict[str, dict]:
    bodies = {
        "llm-proxy": llm_routes.bifrost_proxy_route(vk),
        "llm-metrics": llm_routes.bifrost_metrics_route(),
    }
    for route_id in llm_routes.CONVERTER_ROUTE_IDS:
        bodies[route_id] = llm_routes.converter_route(route_id, vk)
    return bodies


def _headers(body: dict) -> dict:
    return body["plugins"]["proxy-rewrite"].get("headers", {})


# ---------------------------------------------------------------------------
# The route definitions
# ---------------------------------------------------------------------------


def test_bifrost_exposes_only_the_served_paths() -> None:
    bodies = _on_bifrost()

    exposed = sorted(
        (method, path)
        for body in bodies.values()
        for path in _paths(body)
        for method in body["methods"]
        if method != "OPTIONS"
    )
    # Exact paths only: Bifrost answers any other extension-less path with its
    # UI's index.html (200) and serves its management API under /api/*.
    assert exposed == sorted(ON_BIFROST)
    assert all("*" not in path for body in bodies.values() for path in _paths(body))
    assert sorted(llm_routes.served_endpoints()) == sorted(f"{m} {p}" for m, p in ON_BIFROST)

    proxy = bodies["llm-proxy"]
    assert proxy["upstream_id"] == "bifrost"
    assert proxy["timeout"] == {"connect": 60, "send": 600, "read": 600}
    headers = _headers(proxy)
    assert headers["set"]["x-bf-vk"] == BIFROST_VK
    assert headers["set"]["x-bf-dim-consumer"] == "$consumer_name"
    assert headers["set"]["x-bf-lh-consumer"] == "$consumer_name"
    # Non-OpenAI fields reach the backend on the raw proxy.
    assert headers["set"]["x-bf-passthrough-extra-params"] == "true"
    assert headers["remove"] == list(llm_routes.REMOVED_HEADERS)

    metrics = bodies["llm-metrics"]
    assert metrics["upstream_id"] == "bifrost"
    assert metrics["plugins"]["proxy-rewrite"]["regex_uri"] == ["^/api/llm(.*)", "$1"]
    assert "set" not in _headers(metrics)

    # APISIX picks the route on the normalized path; with the raw one,
    # /api/llm/%2e%2e/llm/metrics reached Bifrost unresolved and got its UI.
    for body in (proxy, metrics):
        assert body["plugins"]["proxy-rewrite"]["use_real_request_uri_unsafe"] is False

    for route_id, body in bodies.items():
        # The Prometheus route label carries names (prefer_name), and every
        # selector keyed on the LiteLLM-era ids must keep matching.
        assert body["name"] == route_id
        assert body["plugins"]["key-auth"] == {}
        assert body["plugins"]["consumer-restriction"] == {"whitelist": [DENY_ALL_CONSUMER]}
        assert body["status"] == 1


def test_converter_routes_carry_the_virtual_key() -> None:
    messages = llm_routes.converter_route("llm-messages", BIFROST_VK)
    headers = _headers(messages)
    assert headers["set"] == {
        "x-bf-vk": BIFROST_VK,
        "x-bf-dim-consumer": "$consumer_name",
        "x-bf-lh-consumer": "$consumer_name",
    }
    # The client's own credential headers never reach Bifrost.
    assert headers["remove"] == list(llm_routes.REMOVED_HEADERS)
    # The converter answers count_tokens with its own estimate.
    assert _paths(messages) == ["/api/llm/v1/messages", "/api/llm/v1/messages/count_tokens"]
    assert messages["upstream_id"] == "llm-converter"
    assert messages["timeout"] == {"connect": 60, "send": 600, "read": 600}

    models = llm_routes.converter_route("llm-models", BIFROST_VK)
    assert models["methods"] == ["GET"]
    assert "timeout" not in models
    assert _headers(models) == headers


def test_not_found_route_answers_the_rest_of_the_prefix() -> None:
    body = llm_routes.not_found_route()

    assert body["name"] == LLM_NOT_FOUND_ROUTE_ID
    assert body["uri"] == "/api/llm/*"
    # It proxies nothing and takes no key: no upstream, no key-auth.
    assert "upstream_id" not in body and "upstream" not in body
    assert "key-auth" not in body["plugins"]
    restriction = body["plugins"]["consumer-restriction"]
    assert restriction["type"] == "route_id"
    assert restriction["blacklist"] == [LLM_NOT_FOUND_ROUTE_ID]
    assert restriction["rejected_code"] == 404
    for endpoint in llm_routes.served_endpoints():
        assert endpoint in restriction["rejected_msg"]
    # Below any other /api/llm/* route: should a release from before the switch
    # bring back the catch-all without removing this route, the catch-all wins.
    assert body["priority"] < 0


def test_they_stay_system_routes_with_their_grants() -> None:
    assert LLM_NOT_FOUND_ROUTE_ID in PROTECTED_ROUTE_IDS
    # An llm-proxy grant still implies the converter routes, whatever the gateway.
    assert {"llm-messages", "llm-responses", "llm-models"} <= set(IMPLIED_ROUTES["llm-proxy"])
    for body in _on_bifrost().values():
        for name in _headers(body).get("set", {}):
            assert name.lower() in _SYSTEM_INJECTED_HEADERS, name
        # A custom route cannot take any of the paths.
        for path in _paths(body):
            assert _shadowed_system_uri(path, list(SYSTEM_ROUTE_URIS)), path


# VIRTUAL_KEY_CASES (tests/helpers.py) also holds the deploy script's rule to it in
# test_deployment_security.py.
@pytest.mark.parametrize("value,usable", VIRTUAL_KEY_CASES)
def test_usable_virtual_key_takes_only_url_safe_keys(value: str, usable: bool) -> None:
    assert llm_routes.usable_virtual_key(value) is usable


def test_bifrost_upstream_is_protected_and_plain_http() -> None:
    # A protected upstream cannot be re-pointed to harvest the injected key.
    assert "bifrost" in PROTECTED_UPSTREAM_IDS
    assert llm_routes.BIFROST_UPSTREAM["nodes"] == {"bifrost:8080": 1}
    assert llm_routes.BIFROST_UPSTREAM["scheme"] == "http"


def test_litellm_ids_stay_protected() -> None:
    # No longer deleted at boot, but kept protected until the llm-bi metric
    # exclusions go too (after 2026-12-18).
    assert RETIRED_LITELLM_ROUTE_ID in PROTECTED_ROUTE_IDS
    assert RETIRED_LITELLM_UPSTREAM_ID in PROTECTED_UPSTREAM_IDS


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def _patched(fake: FakeApisix, **settings):
    config = SimpleNamespace(**{"BIFROST_TEST_VK": BIFROST_VK, **settings})
    return (
        patch("app.main.settings", config),
        patch("app.services.apisix_client.get_resource", fake.get_resource),
        patch("app.services.apisix_client.put_resource", fake.put_resource),
    )


async def _provision(fake: FakeApisix, **settings) -> None:
    config, get, put = _patched(fake, **settings)
    with config, get, put:
        await _provision_llm_routes()


def _converter_writes() -> list[tuple[str, str, str]]:
    return [("PUT", "routes", route_id) for route_id in llm_routes.CONVERTER_ROUTE_IDS]


async def test_provisioning_installs_the_exact_paths_then_the_404_route() -> None:
    fake = FakeApisix()

    await _provision(fake)

    assert fake.writes == [
        ("PUT", "upstreams", "bifrost"),
        ("PUT", "upstreams", "llm-converter"),
        *_converter_writes(),
        ("PUT", "routes", "llm-proxy"),
        ("PUT", "routes", "llm-metrics"),
        ("PUT", "routes", LLM_NOT_FOUND_ROUTE_ID),
    ]
    for route_id, body in _on_bifrost().items():
        assert fake.bodies[("routes", route_id)] == body, route_id
    assert fake.bodies[("upstreams", "bifrost")] == llm_routes.BIFROST_UPSTREAM
    assert fake.bodies[("routes", LLM_NOT_FOUND_ROUTE_ID)] == llm_routes.not_found_route()


async def test_reprovisioning_keeps_the_grants() -> None:
    granted = {"whitelist": ["alice", "master-app"]}
    start = _on_bifrost()
    existing = {}
    for route_id, body in start.items():
        route = {"id": route_id, "name": route_id, **deepcopy(body)}
        route["plugins"] = {**route.get("plugins", {}), "consumer-restriction": dict(granted)}
        existing[("routes", route_id)] = route
    fake = FakeApisix(existing)

    await _provision(fake)

    for route_id in start:
        body = fake.bodies[("routes", route_id)]
        assert body["plugins"]["consumer-restriction"] == granted, route_id


@pytest.mark.parametrize("value", ["", "sk-bf-short", "not-a-key"])
async def test_without_a_usable_key_leaves_everything_alone(value, caplog) -> None:
    fake = FakeApisix()

    with caplog.at_level(logging.ERROR, logger="app.main"):
        await _provision(fake, BIFROST_TEST_VK=value)

    assert fake.writes == []
    assert "left as they are" in caplog.text


async def test_apisix_refusing_the_404_route_does_not_block_boot(caplog) -> None:
    fake = FakeApisix(put_errors={("routes", LLM_NOT_FOUND_ROUTE_ID): http_status_error(400, "bad")})

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _provision(fake)

    assert "refused the llm-not-found route" in caplog.text


async def test_a_404_route_outage_still_reaches_the_retry_loop() -> None:
    fake = FakeApisix(put_errors={("routes", LLM_NOT_FOUND_ROUTE_ID): http_status_error(503)})

    with pytest.raises(Exception):
        await _provision(fake)


# ---------------------------------------------------------------------------
# The gateway router keeps the system headers
# ---------------------------------------------------------------------------


def _provisioned_responses_route() -> dict:
    return {
        "id": "llm-responses",
        **llm_routes.converter_route("llm-responses", BIFROST_VK),
    }


def _responses_body(**overrides) -> dict:
    route = _provisioned_responses_route()
    body = {
        key: route[key] for key in ("name", "uri", "methods", "priority", "upstream_id", "status")
    }
    body.update(overrides)
    return body


def _save_mocks(existing: dict):
    return (
        patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value={"items": [deepcopy(existing)], "total": 1},
        ),
        patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(existing),
        ),
    )


async def test_service_key_edit_keeps_the_virtual_key_and_attribution(client, admin_token) -> None:
    listing, put = _save_mocks(_provisioned_responses_route())
    with listing, put as mock_put:
        resp = await client.put(
            "/admin/gateway/routes/llm-responses",
            json=_responses_body(service_keys=[{"header_name": "X-Extra", "header_value": "v"}]),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200, resp.text
    headers = mock_put.call_args[0][2]["plugins"]["proxy-rewrite"]["headers"]
    assert headers["set"] == {
        "X-Extra": "v",
        "x-bf-vk": BIFROST_VK,
        "x-bf-dim-consumer": "$consumer_name",
        "x-bf-lh-consumer": "$consumer_name",
    }
    assert headers["remove"] == list(llm_routes.REMOVED_HEADERS)


@pytest.mark.parametrize("header", ["x-bf-vk", "X-BF-VK", "x-bf-lh-consumer", "x-bf-dim-consumer"])
async def test_a_hand_set_bifrost_header_is_refused(client, admin_token, header) -> None:
    listing, put = _save_mocks(_provisioned_responses_route())
    with listing, put as mock_put:
        resp = await client.put(
            "/admin/gateway/routes/llm-responses",
            json=_responses_body(service_keys=[{"header_name": header, "header_value": "attacker"}]),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400
    assert "startup provisioning" in resp.json()["detail"]
    mock_put.assert_not_awaited()


async def test_curl_sample_uses_the_first_path_of_a_multi_path_route(client, admin_token) -> None:
    route = {"id": "llm-proxy", **llm_routes.bifrost_proxy_route(BIFROST_VK)}
    with patch(
        "app.routers.gateway.apisix_client.get_resource",
        new_callable=AsyncMock,
        return_value=route,
    ):
        resp = await client.get(
            "/admin/gateway/routes/llm-proxy/curl", headers=auth_header(admin_token)
        )
    assert resp.status_code == 200
    curl = resp.json()["curl"]
    assert "-X POST" in curl
    assert "/api/llm/v1/chat/completions'" in curl


def test_openapi_lists_the_endpoints_but_not_the_404_route() -> None:
    routes = [{"id": route_id, **body} for route_id, body in _on_bifrost().items()]
    routes.append({"id": LLM_NOT_FOUND_ROUTE_ID, **llm_routes.not_found_route()})

    spec = build_openapi_spec(routes, [], server_url="https://gateway.example")

    assert set(spec["paths"]) == {path for _method, path in ON_BIFROST}
