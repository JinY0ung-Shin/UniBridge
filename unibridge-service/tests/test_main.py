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


def _keyed_get_resource(resources: dict[tuple[str, str], object]) -> AsyncMock:
    """apisix_client.get_resource fake keyed by (resource, resource_id).

    Order-independent on purpose: with a positional ``side_effect`` list, adding
    a lookup anywhere in the lifespan silently shifts which response every later
    call receives. Unmapped keys raise the "missing resource" error the lifespan
    treats as first-boot creation, so an empty mapping means "nothing exists
    yet". Map a key to an exception to make that one lookup fail.
    """

    def _get(resource, resource_id):
        try:
            value = resources[(resource, resource_id)]
        except KeyError:
            raise RuntimeError(f"404 {resource}/{resource_id} not found") from None
        if isinstance(value, Exception):
            raise value
        return value

    return AsyncMock(side_effect=_get)


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
        patch(
            "app.main.settings",
            SimpleNamespace(
                LITELLM_MASTER_KEY="sk-test",
                APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
                APISIX_ADMIN_KEY="admin-secret",
            ),
        ),
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

    # The prometheus global rule must pin prefer_name — it is what makes the
    # Prometheus `route` label carry route *names* instead of ids (Grafana
    # Route picker); reverting the body to {} silently regresses to id labels.
    global_rule_calls = {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == "global_rules"
    }
    assert global_rule_calls["prometheus"] == {
        "plugins": {"prometheus": {"prefer_name": True}}
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
    for route_id in (
        "query-api", "query-template-write-api", "s3-api", "llm-proxy", "llm-admin"
    ):
        assert (
            route_calls[route_id]["plugins"]["proxy-rewrite"][
                "use_real_request_uri_unsafe"
            ]
            is True
        )


@pytest.mark.asyncio
async def test_lifespan_uses_configured_apisix_upstream_nodes():
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
        patch(
            "app.main.settings",
            SimpleNamespace(
                LITELLM_MASTER_KEY="sk-test",
                APISIX_PROVISION_ON_START=True,
                APISIX_UNIBRIDGE_SERVICE_NODE="unibridge-service-green:8000",
                APISIX_LLM_CONVERTER_NODE="llm-converter-green:4001",
                APISIX_PROMETHEUS_NODE="prometheus-alt:9090",
            ),
        ),
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

    assert upstream_calls["unibridge-service"]["nodes"] == {
        "unibridge-service-green:8000": 1
    }
    assert upstream_calls["llm-converter"]["nodes"] == {
        "llm-converter-green:4001": 1
    }
    assert upstream_calls["prometheus"]["nodes"] == {"prometheus-alt:9090": 1}


@pytest.mark.asyncio
async def test_lifespan_provisions_prometheus_query_route():
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
        patch(
            "app.main.settings",
            SimpleNamespace(
                LITELLM_MASTER_KEY="sk-test",
                APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
                APISIX_ADMIN_KEY="admin-secret",
            ),
        ),
        patch("app.services.apisix_client.get_resource", _keyed_get_resource({})),
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

    assert upstream_calls["prometheus"]["scheme"] == "http"
    assert upstream_calls["prometheus"]["nodes"] == {"prometheus:9090": 1}

    route = route_calls["prometheus-api"]
    assert route["uri"] == "/api/prometheus/*"
    assert route["upstream_id"] == "prometheus"
    # POST is needed for form-encoded /api/v1/query bodies.
    assert route["methods"] == ["GET", "POST"]
    assert route["plugins"]["key-auth"] == {}
    # Ships deny-all so no arbitrary key can read metrics in the window before
    # the consumer-restriction replay installs the real whitelist.
    assert route["plugins"]["consumer-restriction"] == {"whitelist": ["__deny_all__"]}
    assert route["plugins"]["proxy-rewrite"]["regex_uri"] == [
        "^/api/prometheus(.*)",
        "$1",
    ]
    assert route["plugins"]["proxy-rewrite"]["use_real_request_uri_unsafe"] is True
    # The upstream is Prometheus, not this app, so the internal-proxy header
    # must not be injected — it would leak the shared secret to a third party.
    assert "headers" not in route["plugins"]["proxy-rewrite"]


@pytest.mark.asyncio
async def test_lifespan_provisions_litellm_metrics_route():
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
        patch(
            "app.main.settings",
            SimpleNamespace(
                LITELLM_MASTER_KEY="sk-test",
                APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
                APISIX_ADMIN_KEY="admin-secret",
            ),
        ),
        patch("app.services.apisix_client.get_resource", _keyed_get_resource({})),
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

    route = {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    }["llm-metrics"]

    assert route["uri"] == "/api/llm/metrics"
    assert route["upstream_id"] == "litellm"
    # Scrape-only: no other verb reaches LiteLLM through this route.
    assert route["methods"] == ["GET"]
    # Beats the /api/llm/* catch-all, which is what carves the metrics endpoint
    # out of the llm-proxy grant into its own.
    assert route["priority"] == 10
    assert route["plugins"]["key-auth"] == {}
    assert route["plugins"]["consumer-restriction"] == {"whitelist": ["__deny_all__"]}
    assert route["plugins"]["proxy-rewrite"]["regex_uri"] == ["^/api/llm(.*)", "$1"]
    assert route["plugins"]["proxy-rewrite"]["use_real_request_uri_unsafe"] is True

    headers = route["plugins"]["proxy-rewrite"]["headers"]["set"]
    # The master key rides along so the route keeps working on LiteLLM releases
    # that gate /metrics behind bearer auth.
    assert headers["Authorization"] == "Bearer sk-test"
    # A scrape has no consumer semantics — tagging it as an end user would
    # invent LiteLLM spend/usage rows for the scraper.
    assert "x-litellm-end-user-id" not in headers


@pytest.mark.asyncio
async def test_lifespan_provisions_llm_models_route():
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
        patch(
            "app.main.settings",
            SimpleNamespace(
                LITELLM_MASTER_KEY="sk-test",
                APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
                APISIX_ADMIN_KEY="admin-secret",
            ),
        ),
        patch("app.services.apisix_client.get_resource", _keyed_get_resource({})),
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

    route = {
        call.args[1]: call.args[2]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    }["llm-models"]

    assert route["uri"] == "/api/llm/v1/models"
    # The converter serves the listing (it appends the claude/ aliases), not
    # LiteLLM directly.
    assert route["upstream_id"] == "llm-converter"
    assert route["methods"] == ["GET"]
    # Beats the /api/llm/* catch-all, which is what gives this path its own grant.
    assert route["priority"] == 10
    assert route["plugins"]["key-auth"] == {}
    assert route["plugins"]["consumer-restriction"] == {"whitelist": ["__deny_all__"]}
    assert route["plugins"]["proxy-rewrite"]["regex_uri"] == ["^/api/llm(.*)", "$1"]
    assert route["plugins"]["proxy-rewrite"]["use_real_request_uri_unsafe"] is True

    headers = route["plugins"]["proxy-rewrite"]["headers"]["set"]
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["x-litellm-end-user-id"] == "$consumer_name"


@pytest.mark.asyncio
async def test_lifespan_can_skip_apisix_route_provisioning():
    app = FastAPI()
    put_resource = AsyncMock()
    get_resource = AsyncMock()
    list_resources = AsyncMock(return_value={"items": []})
    replay = AsyncMock()
    start_checker = AsyncMock(return_value=_DummyTask())

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
                APISIX_PROVISION_ON_START=False,
            ),
        ),
        patch("app.services.apisix_client.get_resource", get_resource),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch("app.services.apisix_client.list_resources", list_resources),
        patch("app.main.api_keys.sync_all_consumer_route_restrictions", replay),
        patch("app.services.alert_checker.start_checker", start_checker),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(app):
            pass

    # Route/upstream provisioning is skipped entirely when the flag is false —
    # but the prometheus global rule is still reconciled on every boot (it is
    # the only place prefer_name lives; see main.py), so exactly that one PUT
    # goes out and it must pin prefer_name. The internal-proxy header reconcile
    # also runs on every boot but has no secret configured here, so it touches
    # nothing; test_internal_proxy_secret.py covers it with one set.
    assert [call.args[:2] for call in put_resource.await_args_list] == [
        ("global_rules", "prometheus")
    ]
    assert put_resource.await_args_list[0].args[2] == {
        "plugins": {"prometheus": {"prefer_name": True}}
    }
    get_resource.assert_not_awaited()
    assert list_resources.await_count == 2
    # …but the stored API-key restriction replay still runs on every boot
    # (database is the source of truth — see main.py), and the alert checker
    # must still start regardless of the provisioning flag.
    replay.assert_awaited_once()
    start_checker.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_loads_persisted_alert_state_before_starting_checker():
    app = FastAPI()
    put_resource = AsyncMock()
    load_alert_state = AsyncMock()
    start_checker = AsyncMock(return_value=_DummyTask())

    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch("app.main.settings", SimpleNamespace(LITELLM_MASTER_KEY=None)),
        patch("app.services.apisix_client.get_resource", AsyncMock(side_effect=RuntimeError("not found"))),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch("app.services.alert_state.load_alert_state_from_db", load_alert_state),
        patch("app.services.alert_checker.start_checker", start_checker),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(app):
            pass

    load_alert_state.assert_awaited_once()
    start_checker.assert_awaited_once()
    assert load_alert_state.await_args.args[1] is start_checker.await_args.args[0]


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_prometheus_text(client):
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert b"# HELP" in response.content


@pytest.mark.asyncio
async def test_lifespan_preserves_consumer_restriction_for_protected_routes():
    app = FastAPI()
    put_resource = AsyncMock()
    get_resource = _keyed_get_resource(
        {
            # Both color-pinned upstreams already point where this instance
            # would write them, so the claim gate refreshes them in place.
            ("upstreams", "unibridge-service"): {
                "nodes": {"unibridge-service:8000": 1}
            },
            ("upstreams", "llm-converter"): {"nodes": {"llm-converter:4001": 1}},
            # The internal-proxy routes already carry the configured secret, so
            # the boot header reconcile no-ops and every route PUT asserted below
            # comes from provisioning. Rotation is covered in
            # test_internal_proxy_secret.py.
            ("routes", "query-api"): {
                "id": "query-api",
                "name": "query-api",
                "uri": "/api/query/*",
                "methods": ["POST", "GET"],
                "upstream_id": "unibridge-service",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["query-consumer"]},
                    "proxy-rewrite": {
                        "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}
                    },
                },
                "status": 1,
            },
            ("routes", "query-template-write-api"): {
                "id": "query-template-write-api",
                "name": "query-template-write-api",
                "uri": "/api/query/templates/*",
                "methods": ["PUT", "PATCH", "DELETE"],
                "upstream_id": "unibridge-service",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["template-editor"]},
                    "proxy-rewrite": {
                        "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}
                    },
                },
                "status": 1,
            },
            ("routes", "s3-api"): {
                "id": "s3-api",
                "name": "s3-api",
                "uri": "/api/s3/*",
                "methods": ["GET"],
                "upstream_id": "unibridge-service",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["s3-consumer"]},
                    "proxy-rewrite": {
                        "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}
                    },
                },
                "status": 1,
            },
            ("routes", "nas-api"): {
                "id": "nas-api",
                "name": "nas-api",
                "uri": "/api/nas/*",
                "methods": ["GET"],
                "upstream_id": "unibridge-service",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["nas-consumer"]},
                    "proxy-rewrite": {
                        "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}
                    },
                },
                "status": 1,
            },
            ("routes", "usages-api"): {
                "id": "usages-api",
                "name": "usages-api",
                "uri": "/api/usages",
                "methods": ["GET"],
                "upstream_id": "unibridge-service",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["usages-consumer"]},
                    "proxy-rewrite": {
                        "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}
                    },
                },
                "status": 1,
            },
            ("routes", "prometheus-api"): {
                "id": "prometheus-api",
                "name": "prometheus-api",
                "uri": "/api/prometheus/*",
                "methods": ["GET", "POST"],
                "upstream_id": "prometheus",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["prometheus-consumer"]},
                },
                "status": 1,
            },
            ("routes", "llm-proxy"): {
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
            ("routes", "llm-metrics"): {
                "id": "llm-metrics",
                "name": "llm-metrics",
                "uri": "/api/llm/metrics",
                "methods": ["GET"],
                "priority": 10,
                "upstream_id": "litellm",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["scraper-consumer"]},
                },
                "status": 1,
            },
            ("routes", "llm-models"): {
                "id": "llm-models",
                "name": "llm-models",
                "uri": "/api/llm/v1/models",
                "methods": ["GET"],
                "priority": 10,
                "upstream_id": "llm-converter",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["discovery-consumer"]},
                },
                "status": 1,
            },
            ("routes", "llm-messages"): {
                "id": "llm-messages",
                "name": "llm-messages",
                "uri": "/api/llm/v1/messages",
                "methods": ["POST", "OPTIONS"],
                "priority": 10,
                "upstream_id": "llm-converter",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["msgs-consumer"]},
                },
                "status": 1,
            },
            ("routes", "llm-responses"): {
                "id": "llm-responses",
                "name": "llm-responses",
                "uri": "/api/llm/v1/responses",
                "methods": ["POST", "OPTIONS"],
                "priority": 10,
                "upstream_id": "llm-converter",
                "plugins": {
                    "key-auth": {},
                    "consumer-restriction": {"whitelist": ["resp-consumer"]},
                },
                "status": 1,
            },
        }
    )

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
            ),
        ),
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
    upstream_ids = [
        call.args[1]
        for call in put_resource.await_args_list
        if call.args[0] == "upstreams"
    ]

    # An upstream already on this instance's own nodes is refreshed, not skipped.
    assert "unibridge-service" in upstream_ids
    assert "llm-converter" in upstream_ids

    assert route_calls["query-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["query-consumer"]
    }
    assert route_calls["query-template-write-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["template-editor"]
    }
    assert route_calls["query-template-write-api"]["methods"] == [
        "PUT", "PATCH", "DELETE"
    ]
    assert route_calls["s3-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["s3-consumer"]
    }
    assert route_calls["nas-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["nas-consumer"]
    }
    assert route_calls["usages-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["usages-consumer"]
    }
    assert route_calls["prometheus-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["prometheus-consumer"]
    }
    assert route_calls["llm-proxy"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["llm-consumer"]
    }
    assert route_calls["llm-metrics"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["scraper-consumer"]
    }
    assert route_calls["llm-models"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["discovery-consumer"]
    }
    llm_headers = route_calls["llm-proxy"]["plugins"]["proxy-rewrite"]["headers"]["set"]
    assert llm_headers["Authorization"] == "Bearer sk-test"
    assert llm_headers["x-litellm-end-user-id"] == "$consumer_name"
    for route_id in (
        "query-api", "query-template-write-api", "s3-api", "nas-api", "usages-api"
    ):
        headers = route_calls[route_id]["plugins"]["proxy-rewrite"]["headers"]["set"]
        assert headers["X-UniBridge-Internal-Proxy"] == "proxy-secret"

    # The converter routes preserve their consumer-restriction and inject the
    # same master-key / end-user headers as llm-proxy.
    for _route_id, _consumer in (("llm-messages", "msgs-consumer"), ("llm-responses", "resp-consumer")):
        assert route_calls[_route_id]["plugins"]["consumer-restriction"] == {
            "whitelist": [_consumer]
        }
        _headers = route_calls[_route_id]["plugins"]["proxy-rewrite"]["headers"]["set"]
        assert _headers["Authorization"] == "Bearer sk-test"
        assert _headers["x-litellm-end-user-id"] == "$consumer_name"
        assert route_calls[_route_id]["upstream_id"] == "llm-converter"


@pytest.mark.asyncio
async def test_lifespan_treats_missing_protected_routes_as_first_boot_creation():
    app = FastAPI()
    put_resource = AsyncMock()
    # Empty mapping: every route and upstream lookup reports "missing", which is
    # exactly the first-boot state this test is about.
    get_resource = _keyed_get_resource({})

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
    assert route_calls["query-template-write-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    assert "consumer-restriction" not in route_calls["s3-api"]["plugins"]
    assert "consumer-restriction" not in route_calls["llm-proxy"]["plugins"]
    # nas-api ships deny-all by default so it is never callable by an arbitrary
    # key in the window before the consumer-restriction replay runs.
    assert route_calls["nas-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    # usages-api ships deny-all by default, same as nas-api.
    assert route_calls["usages-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    # prometheus-api likewise — an open metric read is the whole thing the
    # gateway route exists to prevent.
    assert route_calls["prometheus-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    assert route_calls["llm-metrics"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    # The converter routes ship deny-all by default so they are never callable by
    # an arbitrary key in the window before the consumer-restriction replay runs.
    assert route_calls["llm-messages"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    assert route_calls["llm-responses"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    assert route_calls["llm-models"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }


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

    # Provisioning retries up to _max_retries (10) times, sleeping
    # min(2**attempt, 15) between attempts: 2, 4, 8, then capped at 15.
    assert get_resource.await_count == 10
    assert sleep.await_args_list == [
        ((2,),), ((4,),), ((8,),), ((15,),), ((15,),),
        ((15,),), ((15,),), ((15,),), ((15,),),
    ]
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
    assert "query-template-write-api" in route_ids
    # Prometheus has nothing to do with LiteLLM: its route sits outside the
    # master-key gate, so a deployment without LiteLLM still gets it.
    assert "prometheus-api" in route_ids
    assert "llm-proxy" not in route_ids
    assert "llm-admin" not in route_ids
    # llm-metrics is the inverse of prometheus-api: its upstream is litellm,
    # which only exists inside the master-key gate, so it must not be written.
    assert "llm-metrics" not in route_ids
    # llm-models likewise: the converter upstream is created inside that gate.
    assert "llm-models" not in route_ids


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

    class _NasDb:
        async def execute(self, _query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    class _ServersDb:
        async def execute(self, _query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    class _SettingsDb:
        pass

    class _ReplayDb:
        pass

    class _AlertStateDb:
        async def execute(self, _query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    async def fake_get_db_sequence():
        yield next(db_iter)

    async def put_resource(resource_type, resource_id, body):
        del body
        events.append((resource_type, resource_id))

    replay_route_restrictions = AsyncMock(
        side_effect=lambda db: events.append(("replay", db.__class__.__name__))
    )
    db_iter = iter(
        [
            _ConnectionsDb(),
            _S3Db(),
            _NasDb(),
            _ServersDb(),
            _SettingsDb(),
            _ReplayDb(),
            _AlertStateDb(),
        ]
    )

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
    assert events.index(("routes", "query-template-write-api")) < events.index(
        ("replay", "_ReplayDb")
    )
    assert events.index(("routes", "s3-api")) < events.index(("replay", "_ReplayDb"))
    assert events.index(("routes", "usages-api")) < events.index(("replay", "_ReplayDb"))
    assert events.index(("routes", "llm-proxy")) < events.index(("replay", "_ReplayDb"))
    assert events.index(("routes", "llm-admin")) < events.index(("replay", "_ReplayDb"))


class TestApiDocsGate:
    """/docs, /redoc and /openapi.json disclose the whole admin API schema and
    the edge proxies them straight through — they must stay off unless
    ENABLE_API_DOCS is set (it is not in tests, matching the default)."""

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    async def test_api_docs_endpoints_are_disabled_by_default(self, client, path):
        resp = await client.get(path)
        assert resp.status_code == 404
