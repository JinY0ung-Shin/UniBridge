"""The /api/llm-bi routes (app/services/bifrost_routes.py) and their provisioning.

Pinned here:

1. The routes expose exactly six inference paths of Bifrost and inject the
   gateway virtual key; nothing of its UI, management API or MCP is reachable.
2. Boot provisioning follows BIFROST_GATEWAY_ROUTES: on installs the upstreams,
   the four routes (keeping their grants) and the not-found route; off installs
   the "switched off" explanation and removes the rest; an unusable virtual key
   leaves the rest as it is and says so. APISIX refusing the not-found route
   never keeps the service from booting.
3. They are system routes: the gateway router refuses to hand their namespace
   to a custom route, keeps their injected headers through a service-key edit,
   and the OpenAPI export leaves out the not-found route.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.main import _provision_bifrost_routes, lifespan
from app.routers.gateway import (
    _SYSTEM_INJECTED_HEADERS,
    _SYSTEM_ROUTE_URIS,
    _shadowed_system_uri,
)
from app.services import bifrost_routes
from app.services.apisix_system_resources import (
    BIFROST_NOT_FOUND_ROUTE_ID,
    BIFROST_ROUTE_IDS,
    BIFROST_UPSTREAM_IDS,
    PROTECTED_ROUTE_IDS,
    PROTECTED_UPSTREAM_IDS,
)
from app.services.consumer_restrictions import DENY_ALL_CONSUMER, IMPLIED_ROUTES
from app.services.openapi_export import build_openapi_spec
from tests.conftest import auth_header
from tests.test_main import _DummyTask, _fake_get_db, _keyed_get_resource

VK = "sk-bf-" + "v" * 32
EXPOSED = [
    ("POST", "/api/llm-bi/v1/chat/completions"),
    ("POST", "/api/llm-bi/v1/completions"),
    ("POST", "/api/llm-bi/v1/embeddings"),
    ("POST", "/api/llm-bi/v1/messages"),
    ("POST", "/api/llm-bi/v1/responses"),
    ("GET", "/api/llm-bi/v1/models"),
]


def _paths(body: dict) -> list[str]:
    return body.get("uris") or [body["uri"]]


def _status_error(status_code: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("DELETE", "http://apisix:9180/apisix/admin/x")
    response = httpx.Response(status_code, text=text, request=request)
    return httpx.HTTPStatusError(f"{status_code}", request=request, response=response)


# ---------------------------------------------------------------------------
# The route definitions
# ---------------------------------------------------------------------------


def test_routes_expose_only_the_inference_paths() -> None:
    bodies = {route_id: bifrost_routes.route(route_id, VK) for route_id in BIFROST_ROUTE_IDS}

    # Exact paths only: Bifrost answers any other extension-less path with its
    # UI's index.html (HTTP 200) and serves MCP under /v1/mcp/*.
    exposed = sorted(
        (method, path)
        for body in bodies.values()
        for path in _paths(body)
        for method in body["methods"]
        if method != "OPTIONS"
    )
    assert exposed == sorted(EXPOSED)
    assert all("*" not in path for body in bodies.values() for path in _paths(body))
    assert bifrost_routes.served_endpoints() == [f"{method} {path}" for method, path in EXPOSED]

    for route_id, body in bodies.items():
        assert body["name"] == route_id
        assert body["upstream_id"] == ("bifrost" if route_id == "llm-bi-proxy" else "llm-converter-bi")
        assert body["timeout"] == {"connect": 60, "send": 600, "read": 600}
        assert body["status"] == 1
        plugins = body["plugins"]
        assert plugins["key-auth"] == {}
        # Deny-all until the restriction replay grants it.
        assert plugins["consumer-restriction"] == {"whitelist": [DENY_ALL_CONSUMER]}
        rewrite = plugins["proxy-rewrite"]
        assert rewrite["regex_uri"] == ["^/api/llm-bi(.*)", "$1"]
        # Bifrost itself gets the normalized path: APISIX matches the route on
        # it, and the raw one would carry a %2e%2e past the route to Bifrost.
        # The converter routes keep the raw path, as every other route does.
        assert rewrite["use_real_request_uri_unsafe"] is (body["upstream_id"] != "bifrost")
        # Credentials and key selectors a client could aim at Bifrost; key-auth
        # reads the caller's apikey before proxy-rewrite strips these.
        assert rewrite["headers"]["remove"] == [
            "Authorization",
            "x-api-key",
            "api-key",
            "x-goog-api-key",
            "x-bf-api-key",
            "x-bf-api-key-id",
        ]
        injected = rewrite["headers"]["set"]
        assert injected["x-bf-vk"] == VK
        assert injected["x-bf-dim-consumer"] == "$consumer_name"
        assert injected["x-bf-lh-consumer"] == "$consumer_name"
        # Extra params pass on the raw proxy only (the converter attaches
        # LiteLLM-only allowed_openai_params Bifrost must not forward).
        assert ("x-bf-passthrough-extra-params" in injected) is (route_id == "llm-bi-proxy")

    for upstream_id in BIFROST_UPSTREAM_IDS:
        upstream = bifrost_routes.UPSTREAMS[upstream_id]
        assert upstream["name"] == upstream_id
        assert upstream["scheme"] == "http"
    assert bifrost_routes.UPSTREAMS["bifrost"]["nodes"] == {"bifrost:8080": 1}
    assert bifrost_routes.UPSTREAMS["llm-converter-bi"]["nodes"] == {"llm-converter-bi:4001": 1}


def test_route_ids_are_system_resources_and_need_their_own_grant() -> None:
    assert tuple(bifrost_routes._ROUTES) == BIFROST_ROUTE_IDS
    assert set(BIFROST_ROUTE_IDS) | {BIFROST_NOT_FOUND_ROUTE_ID} <= PROTECTED_ROUTE_IDS
    # A protected upstream cannot be re-pointed to harvest the injected key.
    assert set(BIFROST_UPSTREAM_IDS) <= PROTECTED_UPSTREAM_IDS
    # Test access stays an explicit grant for regular keys: no existing grant
    # implies these routes. (Master keys, `*`, are whitelisted on every key-auth
    # route by the consumer-restriction reconciler, these included.)
    assert not set(BIFROST_ROUTE_IDS) & set().union(*IMPLIED_ROUTES.values())
    assert not set(BIFROST_ROUTE_IDS) & set(IMPLIED_ROUTES)


@pytest.mark.parametrize("state", ["on", "off", "no-key"])
def test_not_found_route_answers_everything_itself(state: str) -> None:
    body = bifrost_routes.not_found_route(state)

    assert body["name"] == BIFROST_NOT_FOUND_ROUTE_ID
    assert body["uri"] == "/api/llm-bi/*"
    assert body["status"] == 1
    # Proxies nothing, and stays out of the consumer-restriction reconciler,
    # which manages the whitelist of every key-auth route.
    assert "upstream_id" not in body and "upstream" not in body
    assert set(body["plugins"]) == {"consumer-restriction"}
    restriction = body["plugins"]["consumer-restriction"]
    assert restriction["type"] == "route_id"
    assert restriction["blacklist"] == [BIFROST_NOT_FOUND_ROUTE_ID]
    assert restriction["rejected_code"] == 404
    message = restriction["rejected_msg"]
    if state == "on":
        for endpoint in bifrost_routes.served_endpoints():
            assert endpoint in message
        assert "switched off" not in message
    elif state == "off":
        assert "switched off" in message
        assert "BIFROST_GATEWAY_ROUTES=false" in message
        assert "/api/llm " in message
    else:
        assert "BIFROST_TEST_VK" in message
        assert bifrost_routes.VIRTUAL_KEY_RULE in message


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
    assert bifrost_routes.usable_virtual_key(value) is usable


# ---------------------------------------------------------------------------
# Boot provisioning
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


def _installed() -> dict:
    """Every /api/llm-bi resource as a previous `on` boot left it."""
    existing = {("upstreams", upstream_id): {"id": upstream_id} for upstream_id in BIFROST_UPSTREAM_IDS}
    for route_id in BIFROST_ROUTE_IDS:
        existing[("routes", route_id)] = {"id": route_id, **bifrost_routes.route(route_id, VK)}
    existing[("routes", BIFROST_NOT_FOUND_ROUTE_ID)] = bifrost_routes.not_found_route("on")
    return existing


async def _provision(fake: _FakeApisix, **settings) -> None:
    config = SimpleNamespace(**{"BIFROST_GATEWAY_ROUTES": True, "BIFROST_TEST_VK": VK, **settings})
    with (
        patch("app.main.settings", config),
        patch("app.services.apisix_client.get_resource", fake.get_resource),
        patch("app.services.apisix_client.put_resource", fake.put_resource),
        patch("app.services.apisix_client.delete_resource", fake.delete_resource),
    ):
        await _provision_bifrost_routes()


async def test_on_installs_upstreams_routes_and_the_explanation() -> None:
    fake = _FakeApisix()

    await _provision(fake)

    # Upstreams before the routes that reference them; the explanation last.
    assert fake.writes == [
        ("PUT", "upstreams", "bifrost"),
        ("PUT", "upstreams", "llm-converter-bi"),
        *(("PUT", "routes", route_id) for route_id in BIFROST_ROUTE_IDS),
        ("PUT", "routes", BIFROST_NOT_FOUND_ROUTE_ID),
    ]
    for upstream_id in BIFROST_UPSTREAM_IDS:
        assert fake.bodies[("upstreams", upstream_id)] == bifrost_routes.UPSTREAMS[upstream_id]
    for route_id in BIFROST_ROUTE_IDS:
        assert fake.bodies[("routes", route_id)] == bifrost_routes.route(route_id, VK)
    assert fake.bodies[("routes", BIFROST_NOT_FOUND_ROUTE_ID)] == bifrost_routes.not_found_route("on")


async def test_on_keeps_grants_and_replaces_a_rotated_key() -> None:
    existing = _installed()
    granted = {"whitelist": ["alice", "master-app"]}
    for route_id in BIFROST_ROUTE_IDS:
        route = existing[("routes", route_id)]
        route["plugins"]["consumer-restriction"] = dict(granted)
        route["plugins"]["proxy-rewrite"]["headers"]["set"]["x-bf-vk"] = "sk-bf-" + "o" * 32
    fake = _FakeApisix(existing)

    rotated = "sk-bf-" + "n" * 32
    await _provision(fake, BIFROST_TEST_VK=rotated)

    for route_id in BIFROST_ROUTE_IDS:
        body = fake.bodies[("routes", route_id)]
        assert body["plugins"]["consumer-restriction"] == granted
        assert body["plugins"]["proxy-rewrite"]["headers"]["set"]["x-bf-vk"] == rotated
    assert not [write for write in fake.writes if write[0] == "DELETE"]


async def test_off_explains_first_then_removes_routes_before_upstreams() -> None:
    fake = _FakeApisix(_installed())

    await _provision(fake, BIFROST_GATEWAY_ROUTES=False, BIFROST_TEST_VK="")

    assert fake.writes == [
        ("PUT", "routes", BIFROST_NOT_FOUND_ROUTE_ID),
        *(("DELETE", "routes", route_id) for route_id in BIFROST_ROUTE_IDS),
        ("DELETE", "upstreams", "bifrost"),
        ("DELETE", "upstreams", "llm-converter-bi"),
    ]
    assert fake.bodies[("routes", BIFROST_NOT_FOUND_ROUTE_ID)] == bifrost_routes.not_found_route("off")


async def test_off_on_a_stack_that_never_had_them_installs_only_the_explanation() -> None:
    fake = _FakeApisix()

    await _provision(fake, BIFROST_GATEWAY_ROUTES=False)

    assert [write for write in fake.writes if write[0] == "PUT"] == [
        ("PUT", "routes", BIFROST_NOT_FOUND_ROUTE_ID)
    ]


async def test_off_keeps_an_upstream_another_route_still_uses(caplog) -> None:
    in_use = _status_error(
        400, '{"error_msg":"can not delete this upstream, route [mine] is still using it now"}'
    )
    fake = _FakeApisix(_installed(), delete_errors={("upstreams", "bifrost"): in_use})

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _provision(fake, BIFROST_GATEWAY_ROUTES=False)

    assert ("DELETE", "upstreams", "llm-converter-bi") in fake.writes
    assert "Kept APISIX upstream bifrost" in caplog.text
    assert "route [mine]" in caplog.text


async def test_off_lets_other_admin_errors_reach_the_retry_loop() -> None:
    fake = _FakeApisix(
        _installed(), delete_errors={("routes", "llm-bi-proxy"): _status_error(503)}
    )

    with pytest.raises(httpx.HTTPStatusError):
        await _provision(fake, BIFROST_GATEWAY_ROUTES=False)


@pytest.mark.parametrize("value", ["", "sk-bf-short", "v" * 40, "sk-bf-" + "x" * 20 + " y"])
async def test_on_without_a_usable_virtual_key_only_says_why(value, caplog) -> None:
    fake = _FakeApisix(_installed())

    with caplog.at_level(logging.ERROR, logger="app.main"):
        await _provision(fake, BIFROST_TEST_VK=value)

    # The four routes and the upstreams are left as they are; the rest of
    # /api/llm-bi explains itself instead of answering APISIX's bare 404.
    assert fake.writes == [("PUT", "routes", BIFROST_NOT_FOUND_ROUTE_ID)]
    assert fake.bodies[("routes", BIFROST_NOT_FOUND_ROUTE_ID)] == bifrost_routes.not_found_route(
        "no-key"
    )
    assert "BIFROST_TEST_VK" in caplog.text


@pytest.mark.parametrize("switch", [True, False])
async def test_apisix_refusing_the_not_found_route_does_not_block_boot(switch, caplog) -> None:
    """Else not even switching the routes off could get a stack past it."""
    refused = _status_error(400, '{"error_msg":"failed to check the configuration of plugin"}')
    fake = _FakeApisix(
        _installed(), put_errors={("routes", BIFROST_NOT_FOUND_ROUTE_ID): refused}
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _provision(fake, BIFROST_GATEWAY_ROUTES=switch)

    assert "APISIX refused the llm-bi-not-found route" in caplog.text
    if switch:
        assert ("PUT", "routes", "llm-bi-models") in fake.writes
    else:
        assert ("DELETE", "upstreams", "llm-converter-bi") in fake.writes


async def test_a_not_found_route_outage_still_reaches_the_retry_loop() -> None:
    fake = _FakeApisix(put_errors={("routes", BIFROST_NOT_FOUND_ROUTE_ID): _status_error(503)})

    with pytest.raises(httpx.HTTPStatusError):
        await _provision(fake, BIFROST_GATEWAY_ROUTES=False)


async def test_lifespan_provisions_them_before_the_restriction_replay() -> None:
    """Part of boot provisioning, so the replay grants the new routes right away."""
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
                LITELLM_MASTER_KEY="sk-test",
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

    for route_id in (*BIFROST_ROUTE_IDS, BIFROST_NOT_FOUND_ROUTE_ID):
        assert order.index("llm-models") < order.index(route_id) < order.index("<replay>")


# ---------------------------------------------------------------------------
# The gateway router treats them as system routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri,shadowed",
    [
        ("/api/llm-bi/v1/chat/completions", True),
        ("/api/llm-bi/v1/audio/speech", True),
        ("/api/llm-bi/*", True),
        ("/api/llm-b*", True),
        ("/api/llm-bix/*", False),
        ("/api/llm-bi2/v1/models", False),
    ],
)
def test_custom_routes_cannot_take_the_llm_bi_namespace(uri: str, shadowed: bool) -> None:
    assert (_shadowed_system_uri(uri, list(_SYSTEM_ROUTE_URIS)) is not None) is shadowed


def test_exposed_paths_sit_under_no_other_system_namespace() -> None:
    others = [uri for uri in _SYSTEM_ROUTE_URIS if uri != "/api/llm-bi/*"]
    for _method, path in EXPOSED:
        assert _shadowed_system_uri(path, others) is None, path


def test_injected_headers_are_system_headers() -> None:
    headers = bifrost_routes.route("llm-bi-proxy", VK)["plugins"]["proxy-rewrite"]["headers"]["set"]
    assert {name.lower() for name in headers} <= _SYSTEM_INJECTED_HEADERS


def _provisioned_messages_route() -> dict:
    return {"id": "llm-bi-messages", **bifrost_routes.route("llm-bi-messages", VK)}


def _messages_body(**overrides) -> dict:
    route = _provisioned_messages_route()
    body = {key: route[key] for key in ("name", "uri", "methods", "upstream_id", "status")}
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
    listing, put = _save_mocks(_provisioned_messages_route())
    with listing, put as mock_put:
        resp = await client.put(
            "/admin/gateway/routes/llm-bi-messages",
            json=_messages_body(service_keys=[{"header_name": "X-Extra", "header_value": "v"}]),
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
    assert headers["remove"] == list(bifrost_routes.REMOVED_HEADERS)


@pytest.mark.parametrize("header", ["x-bf-vk", "X-BF-VK", "x-bf-lh-consumer", "x-bf-dim-consumer"])
async def test_a_hand_set_bifrost_header_is_refused(client, admin_token, header) -> None:
    listing, put = _save_mocks(_provisioned_messages_route())
    with listing, put as mock_put:
        resp = await client.put(
            "/admin/gateway/routes/llm-bi-messages",
            json=_messages_body(service_keys=[{"header_name": header, "header_value": "attacker"}]),
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400
    assert "startup provisioning" in resp.json()["detail"]
    mock_put.assert_not_awaited()


async def test_curl_sample_uses_the_first_path_of_a_multi_path_route(client, admin_token) -> None:
    route = {"id": "llm-bi-proxy", **bifrost_routes.route("llm-bi-proxy", VK)}
    with patch(
        "app.routers.gateway.apisix_client.get_resource",
        new_callable=AsyncMock,
        return_value=route,
    ):
        resp = await client.get(
            "/admin/gateway/routes/llm-bi-proxy/curl", headers=auth_header(admin_token)
        )
    assert resp.status_code == 200
    curl = resp.json()["curl"]
    assert "-X POST" in curl
    assert "/api/llm-bi/v1/chat/completions'" in curl


def test_openapi_lists_the_endpoints_but_not_the_not_found_route() -> None:
    routes = [{"id": route_id, **bifrost_routes.route(route_id, VK)} for route_id in BIFROST_ROUTE_IDS]
    routes.append({"id": BIFROST_NOT_FOUND_ROUTE_ID, **bifrost_routes.not_found_route("on")})

    spec = build_openapi_spec(routes, [], server_url="https://gateway.example")

    assert set(spec["paths"]) == {path for _method, path in EXPOSED}
    for method, path in EXPOSED:
        assert method.lower() in spec["paths"][path]
