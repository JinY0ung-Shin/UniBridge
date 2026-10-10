"""The /api/llm routes on LiteLLM or Bifrost (app/services/llm_routes.py) and their provisioning.

Pinned here:

1. On Bifrost, /api/llm exposes exactly the inference, converter and metrics
   paths, under the same route ids as on LiteLLM, and injects the gateway
   virtual key; llm-not-found answers every other path.
2. The converter routes are the same on both gateways and carry each configured
   gateway's credential, because a deploy rewrites the routes before it moves
   the converter upstream to the color that reads the new switch.
3. Boot provisioning follows LLM_GATEWAY, keeps API-key grants across a switch
   in both directions, and leaves everything alone on Bifrost without a usable
   virtual key.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.main import _provision_bifrost_routes, _provision_llm_routes
from app.routers.gateway import (
    _SYSTEM_INJECTED_HEADERS,
    _SYSTEM_ROUTE_URIS,
    _health_path_for_route,
    _shadowed_system_uri,
)
from app.services import bifrost_routes, llm_routes
from app.services.apisix_system_resources import (
    BIFROST_UPSTREAM_IDS,
    LLM_NOT_FOUND_ROUTE_ID,
    PROTECTED_ROUTE_IDS,
)
from app.services.consumer_restrictions import DENY_ALL_CONSUMER, IMPLIED_ROUTES
from tests.test_bifrost_routes import _FakeApisix, _status_error

VK = "sk-bf-" + "v" * 32
MASTER = "sk-litellm-master"

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


def _on_bifrost(vk: str = VK, master_key: str = MASTER) -> dict[str, dict]:
    bodies = {
        "llm-proxy": llm_routes.bifrost_proxy_route(vk),
        "llm-metrics": llm_routes.bifrost_metrics_route(),
    }
    for route_id in llm_routes.CONVERTER_ROUTE_IDS:
        bodies[route_id] = llm_routes.converter_route(
            route_id, master_key=master_key, virtual_key=vk, on_bifrost=True
        )
    return bodies


def _headers(body: dict) -> dict:
    return body["plugins"]["proxy-rewrite"].get("headers", {})


# ---------------------------------------------------------------------------
# The route definitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gateway", "vk", "state"),
    [
        ("litellm", VK, "litellm"),
        (None, VK, "litellm"),
        ("bifrost", VK, "bifrost"),
        (" Bifrost ", VK, "bifrost"),
        ("bifrost", "", "skip"),
        ("bifrost", "sk-bf-short", "skip"),
        ("bifrost", None, "skip"),
        ("litellm", "", "litellm"),
    ],
)
def test_gateway_state(gateway, vk, state) -> None:
    assert llm_routes.gateway_state(gateway, vk) == state


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
    assert headers["set"]["x-bf-vk"] == VK
    assert headers["set"]["x-bf-dim-consumer"] == "$consumer_name"
    assert headers["set"]["x-bf-lh-consumer"] == "$consumer_name"
    # Non-OpenAI fields reach the backend on the raw proxy, as through LiteLLM.
    assert headers["set"]["x-bf-passthrough-extra-params"] == "true"
    assert headers["remove"] == list(bifrost_routes.REMOVED_HEADERS)

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


def test_converter_routes_carry_each_configured_credential() -> None:
    both = llm_routes.converter_route(
        "llm-messages", master_key=MASTER, virtual_key=VK, on_bifrost=True
    )
    headers = _headers(both)
    assert headers["set"] == {
        "Authorization": f"Bearer {MASTER}",
        "x-litellm-end-user-id": "$consumer_name",
        "x-bf-vk": VK,
        "x-bf-dim-consumer": "$consumer_name",
        "x-bf-lh-consumer": "$consumer_name",
    }
    # The client's own credential headers never reach either gateway.
    assert headers["remove"] == [h for h in bifrost_routes.REMOVED_HEADERS if h != "Authorization"]
    assert _paths(both) == ["/api/llm/v1/messages", "/api/llm/v1/messages/count_tokens"]
    assert both["upstream_id"] == "llm-converter"
    # On LiteLLM, count_tokens stays on llm-proxy's catch-all: LiteLLM answers it.
    on_litellm = llm_routes.converter_route(
        "llm-messages", master_key=MASTER, virtual_key=VK, on_bifrost=False
    )
    assert _paths(on_litellm) == ["/api/llm/v1/messages"]
    assert _headers(on_litellm) == headers

    litellm_only = _headers(
        llm_routes.converter_route(
            "llm-responses", master_key=MASTER, virtual_key="sk-bf-short", on_bifrost=False
        )
    )
    assert not [name for name in litellm_only["set"] if name.startswith("x-bf-")]
    bifrost_only = _headers(
        llm_routes.converter_route("llm-models", master_key="", virtual_key=VK, on_bifrost=True)
    )
    assert "Authorization" not in bifrost_only["set"]
    assert "x-litellm-end-user-id" not in bifrost_only["set"]
    assert "Authorization" in bifrost_only["remove"]

    models = llm_routes.converter_route(
        "llm-models", master_key=MASTER, virtual_key=VK, on_bifrost=True
    )
    assert models["methods"] == ["GET"]
    assert "timeout" not in models


def test_litellm_bodies_keep_their_shape() -> None:
    proxy = llm_routes.litellm_proxy_route(MASTER)
    assert proxy["uri"] == "/api/llm/*"
    assert proxy["upstream_id"] == "litellm"
    assert _headers(proxy)["set"] == {
        "Authorization": f"Bearer {MASTER}",
        "x-litellm-end-user-id": "$consumer_name",
    }
    # Its grants come from the consumer-restriction replay, not a deny-all body.
    assert "consumer-restriction" not in proxy["plugins"]
    metrics = llm_routes.litellm_metrics_route(MASTER)
    assert metrics["upstream_id"] == "litellm"
    assert _headers(metrics)["set"] == {"Authorization": f"Bearer {MASTER}"}


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
            assert _shadowed_system_uri(path, list(_SYSTEM_ROUTE_URIS)), path


@pytest.mark.parametrize(
    ("route", "path"),
    [
        ({"id": "llm-proxy", "upstream_id": "litellm"}, "/health/liveliness"),
        ({"id": "llm-proxy", "upstream_id": "bifrost"}, "/health"),
        ({"id": "llm-admin", "upstream_id": "litellm"}, "/health/liveliness"),
        ({"id": "llm-proxy"}, "/health/liveliness"),
        ({"id": "custom", "upstream_id": "litellm"}, "/health/liveliness"),
        ({"id": "custom", "upstream_id": "other"}, "/health"),
    ],
)
def test_route_health_path_follows_the_upstream(route, path) -> None:
    assert _health_path_for_route(route) == path


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


async def _provision(fake: _FakeApisix, **settings) -> None:
    config = SimpleNamespace(
        **{"LITELLM_MASTER_KEY": MASTER, "BIFROST_TEST_VK": VK, "LLM_GATEWAY": "litellm", **settings}
    )
    with (
        patch("app.main.settings", config),
        patch("app.services.apisix_client.get_resource", fake.get_resource),
        patch("app.services.apisix_client.put_resource", fake.put_resource),
        patch("app.services.apisix_client.delete_resource", fake.delete_resource),
    ):
        await _provision_llm_routes()


def _converter_writes() -> list[tuple[str, str, str]]:
    return [("PUT", "routes", route_id) for route_id in llm_routes.CONVERTER_ROUTE_IDS]


async def test_litellm_installs_the_catch_all_and_no_404_route() -> None:
    fake = _FakeApisix()

    await _provision(fake)

    assert fake.writes == [
        ("PUT", "upstreams", "litellm"),
        ("PUT", "routes", "llm-admin"),
        ("PUT", "upstreams", "llm-converter"),
        *_converter_writes(),
        ("PUT", "routes", "llm-proxy"),
        ("PUT", "routes", "llm-metrics"),
    ]
    assert fake.bodies[("routes", "llm-proxy")] == llm_routes.litellm_proxy_route(MASTER)
    assert fake.bodies[("routes", "llm-metrics")] == llm_routes.litellm_metrics_route(MASTER)
    for route_id in llm_routes.CONVERTER_ROUTE_IDS:
        assert fake.bodies[("routes", route_id)] == llm_routes.converter_route(
            route_id, master_key=MASTER, virtual_key=VK, on_bifrost=False
        )


async def test_back_on_litellm_the_404_route_goes_before_the_catch_all() -> None:
    fake = _FakeApisix({("routes", LLM_NOT_FOUND_ROUTE_ID): llm_routes.not_found_route()})

    await _provision(fake)

    deleted = fake.writes.index(("DELETE", "routes", LLM_NOT_FOUND_ROUTE_ID))
    assert deleted < fake.writes.index(("PUT", "routes", "llm-proxy"))


async def test_bifrost_installs_the_exact_paths_then_the_404_route() -> None:
    fake = _FakeApisix()

    await _provision(fake, LLM_GATEWAY="bifrost")

    assert fake.writes == [
        ("PUT", "upstreams", "litellm"),
        ("PUT", "routes", "llm-admin"),
        ("PUT", "upstreams", "bifrost"),
        ("PUT", "upstreams", "llm-converter"),
        *_converter_writes(),
        ("PUT", "routes", "llm-proxy"),
        ("PUT", "routes", "llm-metrics"),
        ("PUT", "routes", LLM_NOT_FOUND_ROUTE_ID),
    ]
    for route_id, body in _on_bifrost().items():
        assert fake.bodies[("routes", route_id)] == body, route_id
    assert fake.bodies[("upstreams", "bifrost")] == bifrost_routes.UPSTREAMS["bifrost"]
    assert fake.bodies[("routes", LLM_NOT_FOUND_ROUTE_ID)] == llm_routes.not_found_route()


async def test_bifrost_without_litellm_leaves_out_its_credential_and_admin_route() -> None:
    fake = _FakeApisix()

    await _provision(fake, LLM_GATEWAY="bifrost", LITELLM_MASTER_KEY="")

    assert ("PUT", "upstreams", "litellm") not in fake.writes
    assert ("PUT", "routes", "llm-admin") not in fake.writes
    for route_id in llm_routes.CONVERTER_ROUTE_IDS:
        headers = _headers(fake.bodies[("routes", route_id)])
        assert "Authorization" not in headers["set"]
        assert headers["set"]["x-bf-vk"] == VK


@pytest.mark.parametrize("target", ["bifrost", "litellm"])
async def test_a_switch_keeps_the_grants(target) -> None:
    granted = {"whitelist": ["alice", "master-app"]}
    start = _on_bifrost() if target == "litellm" else {
        "llm-proxy": llm_routes.litellm_proxy_route(MASTER),
        "llm-metrics": llm_routes.litellm_metrics_route(MASTER),
        **{
            route_id: llm_routes.converter_route(
                route_id, master_key=MASTER, virtual_key=VK, on_bifrost=False
            )
            for route_id in llm_routes.CONVERTER_ROUTE_IDS
        },
    }
    existing = {}
    for route_id, body in start.items():
        route = {"id": route_id, **body}
        route["plugins"] = {**route["plugins"], "consumer-restriction": dict(granted)}
        existing[("routes", route_id)] = route
    fake = _FakeApisix(existing)

    await _provision(fake, LLM_GATEWAY=target)

    for route_id in start:
        body = fake.bodies[("routes", route_id)]
        assert body["plugins"]["consumer-restriction"] == granted, route_id
    expected_upstream = "bifrost" if target == "bifrost" else "litellm"
    assert fake.bodies[("routes", "llm-proxy")]["upstream_id"] == expected_upstream


@pytest.mark.parametrize("value", ["", "sk-bf-short", "not-a-key"])
async def test_bifrost_without_a_usable_key_leaves_everything_alone(value, caplog) -> None:
    fake = _FakeApisix()

    with caplog.at_level(logging.ERROR, logger="app.main"):
        await _provision(fake, LLM_GATEWAY="bifrost", BIFROST_TEST_VK=value)

    assert fake.writes == []
    assert "left as they are" in caplog.text


async def test_litellm_without_a_master_key_leaves_everything_alone() -> None:
    fake = _FakeApisix()

    await _provision(fake, LITELLM_MASTER_KEY="")

    assert fake.writes == []


async def test_apisix_refusing_the_404_route_does_not_block_boot(caplog) -> None:
    fake = _FakeApisix(put_errors={("routes", LLM_NOT_FOUND_ROUTE_ID): _status_error(400, "bad")})

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _provision(fake, LLM_GATEWAY="bifrost")

    assert "refused the llm-not-found route" in caplog.text


async def test_a_404_route_outage_still_reaches_the_retry_loop() -> None:
    fake = _FakeApisix(put_errors={("routes", LLM_NOT_FOUND_ROUTE_ID): _status_error(503)})

    with pytest.raises(Exception):
        await _provision(fake, LLM_GATEWAY="bifrost")


async def test_switching_llm_bi_off_keeps_the_upstream_api_llm_runs_on() -> None:
    fake = _FakeApisix({("upstreams", upstream_id): {"id": upstream_id} for upstream_id in BIFROST_UPSTREAM_IDS})
    config = SimpleNamespace(BIFROST_GATEWAY_ROUTES=False, BIFROST_TEST_VK=VK, LLM_GATEWAY="bifrost")
    with (
        patch("app.main.settings", config),
        patch("app.services.apisix_client.get_resource", fake.get_resource),
        patch("app.services.apisix_client.put_resource", fake.put_resource),
        patch("app.services.apisix_client.delete_resource", fake.delete_resource),
    ):
        await _provision_bifrost_routes()

    assert ("DELETE", "upstreams", "bifrost") not in fake.writes
    assert ("DELETE", "upstreams", "llm-converter-bi") in fake.writes
