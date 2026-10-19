"""The /api/llm routes on LiteLLM or Bifrost (app/services/llm_routes.py) and their provisioning.

Pinned here:

1. On Bifrost, /api/llm exposes exactly the inference, converter and metrics
   paths, under the same route ids as on LiteLLM, and injects the gateway
   virtual key; llm-not-found answers every other path.
2. The converter routes are the same on both gateways and carry each configured
   gateway's credential, because a deploy rewrites the routes before it moves
   the converter upstream to the color that reads the new switch.
3. Boot provisioning follows LLM_GATEWAY, keeps API-key grants across a switch
   in both directions, takes LiteLLM's admin route and upstream out on Bifrost
   (LiteLLM does not run there), and leaves everything alone on Bifrost without
   a usable virtual key.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.main import _provision_llm_routes
from app.routers.gateway import (
    _SYSTEM_INJECTED_HEADERS,
    _SYSTEM_ROUTE_URIS,
    _health_path_for_route,
    _shadowed_system_uri,
)
from app.services import llm_routes
from app.services.openapi_export import build_openapi_spec
from app.services.apisix_system_resources import (
    LLM_NOT_FOUND_ROUTE_ID,
    PROTECTED_ROUTE_IDS,
    PROTECTED_UPSTREAM_IDS,
)
from app.services.consumer_restrictions import DENY_ALL_CONSUMER, IMPLIED_ROUTES
from tests.conftest import auth_header

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


def _status_error(status_code: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("DELETE", "http://apisix:9180/apisix/admin/x")
    response = httpx.Response(status_code, text=text, request=request)
    return httpx.HTTPStatusError(f"{status_code}", request=request, response=response)


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
    assert headers["remove"] == [h for h in llm_routes.REMOVED_HEADERS if h != "Authorization"]
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


# Shared with test_deployment_security.py, which holds the deploy script's rule to it.
VIRTUAL_KEY_CASES = [
    (VK, True),
    ("sk-bf-" + "x" * 16, True),
    ("sk-bf-Az09_-" + "x" * 10, True),
    ("sk-bf-" + "x" * 15, False),
    ("v" * 40, False),
    ("SK-BF-" + "x" * 32, False),
    ("", False),
    # Bifrost would take these, but the deploy script finds the key in
    # whitespace-stripped, JSON-escaped route bodies, where they never match.
    ("sk-bf-" + "x" * 16 + " y", False),
    ("sk-bf-" + "x" * 16 + '"y', False),
    ("sk-bf-" + "x" * 16 + "\\y", False),
    ("sk-bf-" + "x" * 16 + "+y", False),
    ("sk-bf-" + "x" * 16 + "é", False),
]


@pytest.mark.parametrize("value,usable", VIRTUAL_KEY_CASES)
def test_usable_virtual_key_takes_only_url_safe_keys(value: str, usable: bool) -> None:
    assert llm_routes.usable_virtual_key(value) is usable


def test_bifrost_upstream_is_protected_and_plain_http() -> None:
    # A protected upstream cannot be re-pointed to harvest the injected key.
    assert "bifrost" in PROTECTED_UPSTREAM_IDS
    assert llm_routes.BIFROST_UPSTREAM["nodes"] == {"bifrost:8080": 1}
    assert llm_routes.BIFROST_UPSTREAM["scheme"] == "http"


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


class _FakeApisix:
    """Admin API stand-in recording every write in order.

    ``existing`` maps ``(resource, id)`` to what a GET returns; a DELETE of
    anything else is a 404. ``delete_errors`` / ``put_errors`` make one DELETE
    or PUT raise instead.
    """

    def __init__(self, existing=None, delete_errors=None, put_errors=None):
        self.existing = dict(existing or {})
        self.delete_errors = dict(delete_errors or {})
        self.put_errors = dict(put_errors or {})
        self.writes: list[tuple[str, str, str]] = []
        self.bodies: dict[tuple[str, str], dict] = {}

    async def get_resource(self, resource, resource_id):
        try:
            return deepcopy(self.existing[(resource, resource_id)])
        except KeyError:
            raise _status_error(404) from None

    async def put_resource(self, resource, resource_id, body):
        self.writes.append(("PUT", resource, resource_id))
        error = self.put_errors.get((resource, resource_id))
        if error is not None:
            raise error
        self.bodies[(resource, resource_id)] = deepcopy(body)
        return body

    async def delete_resource(self, resource, resource_id):
        self.writes.append(("DELETE", resource, resource_id))
        error = self.delete_errors.get((resource, resource_id))
        if error is not None:
            raise error
        if (resource, resource_id) not in self.existing:
            raise _status_error(404)



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
        ("PUT", "upstreams", "bifrost"),
        ("PUT", "upstreams", "llm-converter"),
        *_converter_writes(),
        ("PUT", "routes", "llm-proxy"),
        ("PUT", "routes", "llm-metrics"),
        ("PUT", "routes", LLM_NOT_FOUND_ROUTE_ID),
        # LiteLLM does not run on Bifrost; nothing may still route to it.
        ("DELETE", "upstreams", "litellm"),
    ]
    for route_id, body in _on_bifrost().items():
        assert fake.bodies[("routes", route_id)] == body, route_id
    assert fake.bodies[("upstreams", "bifrost")] == llm_routes.BIFROST_UPSTREAM
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


def _litellm_routing() -> dict:
    """LiteLLM's own APISIX resources as a boot on LiteLLM leaves them."""
    return {
        ("upstreams", "litellm"): {"id": "litellm", **llm_routes.LITELLM_UPSTREAM},
        ("routes", "llm-admin"): {"id": "llm-admin", **llm_routes.llm_admin_route()},
    }


async def test_switching_to_bifrost_removes_litellm_once_nothing_routes_to_it() -> None:
    fake = _FakeApisix(_litellm_routing())

    await _provision(fake, LLM_GATEWAY="bifrost")

    route_deleted = fake.writes.index(("DELETE", "routes", "llm-admin"))
    upstream_deleted = fake.writes.index(("DELETE", "upstreams", "litellm"))
    # APISIX refuses to delete an upstream a route still uses.
    assert fake.writes.index(("PUT", "routes", "llm-proxy")) < upstream_deleted
    assert fake.writes.index(("PUT", "routes", "llm-metrics")) < upstream_deleted
    assert route_deleted < upstream_deleted


async def test_switching_back_to_litellm_reinstalls_its_admin_route_and_upstream() -> None:
    fake = _FakeApisix()

    await _provision(fake, LLM_GATEWAY="litellm")

    assert fake.bodies[("upstreams", "litellm")] == llm_routes.LITELLM_UPSTREAM
    assert fake.bodies[("routes", "llm-admin")] == llm_routes.llm_admin_route()


async def test_bifrost_keeps_the_litellm_upstream_another_route_still_uses(caplog) -> None:
    fake = _FakeApisix(
        _litellm_routing(),
        delete_errors={("upstreams", "litellm"): _status_error(400, "still referenced")},
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _provision(fake, LLM_GATEWAY="bifrost")

    assert ("DELETE", "routes", "llm-admin") in fake.writes
    assert "Kept APISIX upstream litellm" in caplog.text


async def test_an_outage_removing_the_litellm_upstream_reaches_the_retry_loop() -> None:
    fake = _FakeApisix(
        _litellm_routing(), delete_errors={("upstreams", "litellm"): _status_error(503)}
    )

    with pytest.raises(Exception):
        await _provision(fake, LLM_GATEWAY="bifrost")


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


# ---------------------------------------------------------------------------
# The gateway router keeps the system headers on Bifrost
# ---------------------------------------------------------------------------


def _provisioned_responses_route() -> dict:
    return {
        "id": "llm-responses",
        **llm_routes.converter_route(
            "llm-responses", master_key="", virtual_key=VK, on_bifrost=True
        ),
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
        "x-bf-vk": VK,
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
    route = {"id": "llm-proxy", **llm_routes.bifrost_proxy_route(VK)}
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
