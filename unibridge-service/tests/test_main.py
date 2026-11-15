from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.routers.gateway import _shadowed_system_uri
from app.services.apisix_client import route_paths
from app.services.apisix_system_resources import SYSTEM_ROUTE_URIS
from tests.helpers import BIFROST_VK, DummyTask, keyed_get_resource, put_bodies, run_lifespan


async def test_lifespan_provisions_the_system_routes():
    put_resource = AsyncMock()

    await run_lifespan(
        SimpleNamespace(
            BIFROST_TEST_VK=BIFROST_VK,
            APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
            APISIX_ADMIN_KEY="admin-secret",
        ),
        put_resource=put_resource,
    )

    upstream_calls = put_bodies(put_resource, "upstreams")
    route_calls = put_bodies(put_resource, "routes")

    # The prometheus global rule must pin prefer_name — it is what makes the
    # Prometheus `route` label carry route *names* instead of ids (Grafana
    # Route picker); reverting the body to {} silently regresses to id labels.
    global_rule_calls = put_bodies(put_resource, "global_rules")
    assert global_rule_calls["prometheus"] == {
        "plugins": {"prometheus": {"prefer_name": True}}
    }

    assert upstream_calls["bifrost"]["scheme"] == "http"
    assert "llm-proxy" in route_calls
    # The gateway's shadowing guard knows every provisioned path, so a custom
    # route cannot take one even when the live APISIX listing comes back
    # without the system routes.
    for route_id, body in route_calls.items():
        for path in route_paths(body):
            assert _shadowed_system_uri(path, list(SYSTEM_ROUTE_URIS)), (route_id, path)
    for route_id in (
        "query-api", "query-template-write-api", "s3-api", "network-api", "network-write-api"
    ):
        assert (
            route_calls[route_id]["plugins"]["proxy-rewrite"][
                "use_real_request_uri_unsafe"
            ]
            is True
        )
    # llm-proxy goes straight to Bifrost, which gets the normalized path.
    assert route_calls["llm-proxy"]["plugins"]["proxy-rewrite"]["use_real_request_uri_unsafe"] is False


# What first-boot provisioning sends for the fixed service routes, key order
# included: scripts/deploy-bluegreen.sh matches the route JSON APISIX hands
# back, and the bodies are built from a table (apisix_system_resources), so a
# table edit must not reshape them unnoticed. Written with the internal-proxy
# secret "proxy-secret" and a 310s query route timeout.
_PINNED_SERVICE_BODIES = {
    ("upstreams", "unibridge-service"): '{"name": "unibridge-service", "type": "roundrobin", "nodes": {"unibridge-service:8000": 1}}',
    ("routes", "query-api"): '{"name": "query-api", "uri": "/api/query/*", "methods": ["POST", "GET"], "upstream_id": "unibridge-service", "timeout": {"connect": 10, "send": 310, "read": 310}, "plugins": {"key-auth": {}, "proxy-rewrite": {"regex_uri": ["^/api/query(.*)", "/query$1"], "use_real_request_uri_unsafe": true, "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}}}, "status": 1}',
    ("routes", "query-template-write-api"): '{"name": "query-template-write-api", "desc": "Create, edit, and delete query templates", "uri": "/api/query/templates/*", "methods": ["PUT", "PATCH", "DELETE"], "priority": 20, "upstream_id": "unibridge-service", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["__deny_all__"]}, "proxy-rewrite": {"regex_uri": ["^/api/query(.*)", "/query$1"], "use_real_request_uri_unsafe": true, "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}}}, "status": 1}',
    ("routes", "s3-api"): '{"name": "s3-api", "uri": "/api/s3/*", "methods": ["GET"], "upstream_id": "unibridge-service", "plugins": {"key-auth": {}, "proxy-rewrite": {"regex_uri": ["^/api/s3(.*)", "/s3$1"], "use_real_request_uri_unsafe": true, "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}}}, "status": 1}',
    ("routes", "nas-api"): '{"name": "nas-api", "uri": "/api/nas/*", "methods": ["GET", "POST"], "upstream_id": "unibridge-service", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["__deny_all__"]}, "proxy-rewrite": {"regex_uri": ["^/api/nas(.*)", "/nas$1"], "use_real_request_uri_unsafe": true, "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}}}, "status": 1}',
    ("routes", "usages-api"): '{"name": "usages-api", "uri": "/api/usages", "methods": ["GET"], "upstream_id": "unibridge-service", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["__deny_all__"]}, "proxy-rewrite": {"regex_uri": ["^/api/usages(.*)", "/usages$1"], "use_real_request_uri_unsafe": true, "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}}}, "status": 1}',
    ("routes", "mcp-api"): '{"name": "mcp-api", "desc": "MCP server (Streamable HTTP): query templates, databases, S3, NAS", "uris": ["/api/mcp", "/api/mcp/artifacts", "/api/mcp/artifacts/*"], "methods": ["GET", "POST", "DELETE"], "upstream_id": "unibridge-service", "timeout": {"connect": 10, "send": 310, "read": 310}, "plugins": {"key-auth": {"hide_credentials": false}, "consumer-restriction": {"whitelist": ["__deny_all__"]}, "proxy-rewrite": {"regex_uri": ["^/api/mcp(.*)", "/mcp$1"], "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}}}, "status": 1}',
    ("routes", "network-api"): '{"name": "network-api", "desc": "Network map: regions, hosts, firewall openings, port usage, access checks", "uri": "/api/network/*", "methods": ["GET"], "upstream_id": "unibridge-service", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["__deny_all__"]}, "proxy-rewrite": {"regex_uri": ["^/api/network(.*)", "/network$1"], "use_real_request_uri_unsafe": true, "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}}}, "status": 1}',
    ("routes", "network-write-api"): '{"name": "network-write-api", "desc": "Network map: record regions, hosts, firewall openings and port usage", "uri": "/api/network/*", "methods": ["POST", "PUT", "DELETE"], "upstream_id": "unibridge-service", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["__deny_all__"]}, "proxy-rewrite": {"regex_uri": ["^/api/network(.*)", "/network$1"], "use_real_request_uri_unsafe": true, "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}}}, "status": 1}',
    ("upstreams", "prometheus"): '{"name": "prometheus", "type": "roundrobin", "scheme": "http", "nodes": {"prometheus:9090": 1}}',
    ("routes", "prometheus-api"): '{"name": "prometheus-api", "desc": "Prometheus HTTP API (PromQL queries, read-only) via the gateway", "uri": "/api/prometheus/*", "methods": ["GET", "POST"], "upstream_id": "prometheus", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["__deny_all__"]}, "proxy-rewrite": {"regex_uri": ["^/api/prometheus(.*)", "$1"], "use_real_request_uri_unsafe": true}}, "status": 1}',
}


async def test_lifespan_writes_the_fixed_service_routes_exactly_as_pinned():
    put_resource = AsyncMock()

    await run_lifespan(
        SimpleNamespace(BIFROST_TEST_VK=BIFROST_VK, APISIX_INTERNAL_PROXY_SECRET="proxy-secret"),
        put_resource=put_resource,
        replay=AsyncMock(),
        extra_patches=[patch("app.main.settings_manager.query_route_timeout", 310)],
    )

    puts = [call.args for call in put_resource.await_args_list]
    # The provisioning order: global rules, the service routes behind their
    # upstreams, then the /api/llm routes and the two explaining 404s.
    assert [args[:2] for args in puts] == [
        ("global_rules", "prometheus"),
        ("global_rules", "unibridge-node-path-prefix"),
        *_PINNED_SERVICE_BODIES,
        ("upstreams", "bifrost"),
        ("upstreams", "llm-converter"),
        ("routes", "llm-messages"),
        ("routes", "llm-responses"),
        ("routes", "llm-models"),
        ("routes", "llm-proxy"),
        ("routes", "llm-metrics"),
        ("routes", "llm-not-found"),
        ("routes", "llm-bi-not-found"),
    ]
    written = {args[:2]: json.dumps(args[2]) for args in puts}
    for key, expected in _PINNED_SERVICE_BODIES.items():
        assert written[key] == expected, key


async def test_lifespan_uses_configured_apisix_upstream_nodes():
    put_resource = AsyncMock()

    await run_lifespan(
        SimpleNamespace(
            BIFROST_TEST_VK=BIFROST_VK,
            APISIX_PROVISION_ON_START=True,
            APISIX_UNIBRIDGE_SERVICE_NODE="unibridge-service-green:8000",
            APISIX_LLM_CONVERTER_NODE="llm-converter-green:4001",
            APISIX_PROMETHEUS_NODE="prometheus-alt:9090",
        ),
        put_resource=put_resource,
    )

    upstream_calls = put_bodies(put_resource, "upstreams")

    assert upstream_calls["unibridge-service"]["nodes"] == {
        "unibridge-service-green:8000": 1
    }
    assert upstream_calls["llm-converter"]["nodes"] == {
        "llm-converter-green:4001": 1
    }
    assert upstream_calls["prometheus"]["nodes"] == {"prometheus-alt:9090": 1}


async def test_lifespan_provisions_prometheus_query_route():
    put_resource = AsyncMock()

    await run_lifespan(
        SimpleNamespace(
            BIFROST_TEST_VK=BIFROST_VK,
            APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
            APISIX_ADMIN_KEY="admin-secret",
        ),
        put_resource=put_resource,
    )

    upstream_calls = put_bodies(put_resource, "upstreams")
    route_calls = put_bodies(put_resource, "routes")

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


async def test_lifespan_provisions_llm_metrics_route():
    put_resource = AsyncMock()

    await run_lifespan(
        SimpleNamespace(
            BIFROST_TEST_VK=BIFROST_VK,
            APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
            APISIX_ADMIN_KEY="admin-secret",
        ),
        put_resource=put_resource,
    )

    route = put_bodies(put_resource, "routes")["llm-metrics"]

    assert route["uri"] == "/api/llm/metrics"
    assert route["upstream_id"] == "bifrost"
    # Scrape-only: no other verb reaches Bifrost through this route.
    assert route["methods"] == ["GET"]
    # Its own grant, carved out of /api/llm: the exposition carries every key's usage.
    assert route["priority"] == 10
    assert route["plugins"]["key-auth"] == {}
    assert route["plugins"]["consumer-restriction"] == {"whitelist": ["__deny_all__"]}
    assert route["plugins"]["proxy-rewrite"]["regex_uri"] == ["^/api/llm(.*)", "$1"]
    assert route["plugins"]["proxy-rewrite"]["use_real_request_uri_unsafe"] is False

    # Bifrost serves /metrics without a login: nothing is injected, and a scrape
    # has no consumer to attribute.
    headers = route["plugins"]["proxy-rewrite"]["headers"]
    assert "set" not in headers
    assert "Authorization" in headers["remove"]


async def test_lifespan_provisions_llm_models_route():
    put_resource = AsyncMock()

    await run_lifespan(
        SimpleNamespace(
            BIFROST_TEST_VK=BIFROST_VK,
            APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
            APISIX_ADMIN_KEY="admin-secret",
        ),
        put_resource=put_resource,
    )

    route = put_bodies(put_resource, "routes")["llm-models"]

    assert route["uri"] == "/api/llm/v1/models"
    # The converter serves the listing (it appends the claude/ aliases), not
    # Bifrost directly.
    assert route["upstream_id"] == "llm-converter"
    assert route["methods"] == ["GET"]
    # Beats the /api/llm/* catch-all, which is what gives this path its own grant.
    assert route["priority"] == 10
    assert route["plugins"]["key-auth"] == {}
    assert route["plugins"]["consumer-restriction"] == {"whitelist": ["__deny_all__"]}
    assert route["plugins"]["proxy-rewrite"]["regex_uri"] == ["^/api/llm(.*)", "$1"]
    assert route["plugins"]["proxy-rewrite"]["use_real_request_uri_unsafe"] is True

    headers = route["plugins"]["proxy-rewrite"]["headers"]["set"]
    assert headers["x-bf-vk"] == BIFROST_VK
    assert headers["x-bf-dim-consumer"] == "$consumer_name"
    assert "Authorization" not in headers


async def test_lifespan_can_skip_apisix_route_provisioning():
    put_resource = AsyncMock()
    get_resource = AsyncMock()
    list_resources = AsyncMock(return_value={"items": []})
    replay = AsyncMock()
    start_checker = AsyncMock(return_value=DummyTask())

    await run_lifespan(
        SimpleNamespace(
            BIFROST_TEST_VK=BIFROST_VK,
            APISIX_PROVISION_ON_START=False,
        ),
        get_resource=get_resource,
        put_resource=put_resource,
        list_resources=list_resources,
        replay=replay,
        start_checker=start_checker,
    )

    # Route/upstream provisioning is skipped entirely when the flag is false —
    # but the global rules are still reconciled on every boot: prometheus (the
    # only place prefer_name lives; see main.py), which must pin prefer_name,
    # then the per-node path-prefix rule. Exactly those two PUTs go out. The
    # internal-proxy header reconcile also runs on every boot but has no secret
    # configured here, so it touches nothing; test_internal_proxy_secret.py
    # covers it with one set.
    assert [call.args[:2] for call in put_resource.await_args_list] == [
        ("global_rules", "prometheus"),
        ("global_rules", "unibridge-node-path-prefix"),
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


async def test_lifespan_loads_persisted_alert_state_before_starting_checker():
    put_resource = AsyncMock()
    load_alert_state = AsyncMock()
    start_checker = AsyncMock(return_value=DummyTask())

    await run_lifespan(
        SimpleNamespace(BIFROST_TEST_VK=None),
        put_resource=put_resource,
        start_checker=start_checker,
        extra_patches=[patch("app.services.alert_state.load_alert_state_from_db", load_alert_state)],
    )

    load_alert_state.assert_awaited_once()
    start_checker.assert_awaited_once()
    assert load_alert_state.await_args.args[1] is start_checker.await_args.args[0]


async def test_lifespan_keeps_apisix_alert_state_when_the_listing_fails(caplog):
    """The stale-state purge gets no upstream/route ids (None, not an empty
    set) when APISIX cannot list them, so an outage wipes no alert state."""
    purge = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await run_lifespan(
            SimpleNamespace(BIFROST_TEST_VK=None, APISIX_PROVISION_ON_START=False),
            list_resources=AsyncMock(side_effect=RuntimeError("apisix down")),
            replay=AsyncMock(),
            extra_patches=[patch("app.services.alert_state.purge_stale_states", purge)],
        )

    purge.assert_awaited_once()
    assert purge.await_args.kwargs["known_upstream_ids"] is None
    assert purge.await_args.kwargs["known_route_ids"] is None
    assert "could not list upstreams" in caplog.text
    assert "could not list routes" in caplog.text


async def test_metrics_endpoint_exposes_prometheus_text(client):
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert b"# HELP" in response.content


async def test_lifespan_preserves_consumer_restriction_for_protected_routes():
    put_resource = AsyncMock()
    get_resource = keyed_get_resource(
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
            **{
                ("routes", route_id): {
                    "id": route_id,
                    "name": route_id,
                    "uri": "/api/network/*",
                    "methods": methods,
                    "upstream_id": "unibridge-service",
                    "plugins": {
                        "key-auth": {},
                        "consumer-restriction": {"whitelist": [consumer]},
                        "proxy-rewrite": {
                            "headers": {"set": {"X-UniBridge-Internal-Proxy": "proxy-secret"}}
                        },
                    },
                    "status": 1,
                }
                for route_id, methods, consumer in (
                    ("network-api", ["GET"], "network-reader"),
                    ("network-write-api", ["POST", "PUT", "DELETE"], "network-recorder"),
                )
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
                "uris": ["/api/llm/v1/chat/completions"],
                "methods": ["POST", "OPTIONS"],
                "upstream_id": "bifrost",
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
                "upstream_id": "bifrost",
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

    await run_lifespan(
        SimpleNamespace(
            BIFROST_TEST_VK=BIFROST_VK,
            APISIX_INTERNAL_PROXY_SECRET="proxy-secret",
            APISIX_ADMIN_KEY="admin-secret",
        ),
        get_resource=get_resource,
        put_resource=put_resource,
    )

    route_calls = put_bodies(put_resource, "routes")
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
    assert route_calls["network-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["network-reader"]
    }
    assert route_calls["network-write-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["network-recorder"]
    }
    assert route_calls["network-api"]["methods"] == ["GET"]
    assert route_calls["network-write-api"]["methods"] == ["POST", "PUT", "DELETE"]
    for route_id in ("network-api", "network-write-api"):
        assert route_calls[route_id]["uri"] == "/api/network/*"
        assert route_calls[route_id]["upstream_id"] == "unibridge-service"
        assert route_calls[route_id]["plugins"]["proxy-rewrite"]["regex_uri"] == [
            "^/api/network(.*)",
            "/network$1",
        ]
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
    assert llm_headers["x-bf-vk"] == BIFROST_VK
    assert llm_headers["x-bf-dim-consumer"] == "$consumer_name"
    for route_id in (
        "query-api", "query-template-write-api", "s3-api", "nas-api", "usages-api",
        "network-api", "network-write-api",
    ):
        headers = route_calls[route_id]["plugins"]["proxy-rewrite"]["headers"]["set"]
        assert headers["X-UniBridge-Internal-Proxy"] == "proxy-secret"

    # The converter routes preserve their consumer-restriction and inject the
    # same virtual key / attribution headers as llm-proxy.
    for _route_id, _consumer in (("llm-messages", "msgs-consumer"), ("llm-responses", "resp-consumer")):
        assert route_calls[_route_id]["plugins"]["consumer-restriction"] == {
            "whitelist": [_consumer]
        }
        _headers = route_calls[_route_id]["plugins"]["proxy-rewrite"]["headers"]["set"]
        assert _headers["x-bf-vk"] == BIFROST_VK
        assert _headers["x-bf-dim-consumer"] == "$consumer_name"
        assert route_calls[_route_id]["upstream_id"] == "llm-converter"


async def test_lifespan_treats_missing_protected_routes_as_first_boot_creation():
    put_resource = AsyncMock()
    # Empty mapping: every route and upstream lookup reports "missing", which is
    # exactly the first-boot state this test is about.
    get_resource = keyed_get_resource({})

    await run_lifespan(
        SimpleNamespace(BIFROST_TEST_VK=BIFROST_VK),
        get_resource=get_resource,
        put_resource=put_resource,
        replay=AsyncMock(),
    )

    route_calls = put_bodies(put_resource, "routes")
    assert "consumer-restriction" not in route_calls["query-api"]["plugins"]
    assert route_calls["query-template-write-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    assert "consumer-restriction" not in route_calls["s3-api"]["plugins"]
    # llm-proxy ships deny-all like the converter routes below.
    assert route_calls["llm-proxy"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    # nas-api ships deny-all by default so it is never callable by an arbitrary
    # key in the window before the consumer-restriction replay runs.
    assert route_calls["nas-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    # usages-api ships deny-all by default, same as nas-api.
    assert route_calls["usages-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["__deny_all__"]
    }
    # Both network routes ship deny-all, same as nas-api.
    for route_id in ("network-api", "network-write-api"):
        assert route_calls[route_id]["plugins"]["consumer-restriction"] == {
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


async def test_lifespan_retries_when_protected_route_state_lookup_fails():
    put_resource = AsyncMock()
    get_resource = AsyncMock(side_effect=RuntimeError("APISIX unavailable"))
    sleep = AsyncMock()

    with pytest.raises(RuntimeError, match="APISIX unavailable"):
        await run_lifespan(
            SimpleNamespace(BIFROST_TEST_VK=""),
            get_resource=get_resource,
            put_resource=put_resource,
            extra_patches=[patch("asyncio.sleep", sleep)],
        )

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


async def test_lifespan_skips_llm_routes_without_a_usable_virtual_key():
    put_resource = AsyncMock()

    await run_lifespan(SimpleNamespace(BIFROST_TEST_VK=""), put_resource=put_resource)

    route_ids = [
        call.args[1]
        for call in put_resource.await_args_list
        if call.args[0] == "routes"
    ]

    assert "query-api" in route_ids
    assert "query-template-write-api" in route_ids
    # Prometheus has nothing to do with Bifrost: its route sits outside the
    # virtual-key gate, so a deployment without the key still gets it.
    assert "prometheus-api" in route_ids
    # Without the key every /api/llm request would 401, so the routes are left
    # as they are.
    assert "llm-proxy" not in route_ids
    assert "llm-metrics" not in route_ids
    assert "llm-models" not in route_ids
    assert "llm-not-found" not in route_ids


async def test_lifespan_replays_api_key_route_restrictions_after_provisioning_with_fresh_db_block():
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

    class _McpServersDb:
        async def execute(self, _query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    class _ReplayDb:
        pass

    class _AlertStateDb:
        async def execute(self, _query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: []),
            )

    @asynccontextmanager
    async def fake_session_sequence():
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
            _McpServersDb(),
            _ReplayDb(),
            _AlertStateDb(),
        ]
    )

    await run_lifespan(
        SimpleNamespace(BIFROST_TEST_VK=BIFROST_VK),
        put_resource=AsyncMock(side_effect=put_resource),
        replay=replay_route_restrictions,
        session=fake_session_sequence,
    )

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
    assert events.index(("routes", "network-api")) < events.index(("replay", "_ReplayDb"))
    assert events.index(("routes", "network-write-api")) < events.index(
        ("replay", "_ReplayDb")
    )
    assert events.index(("routes", "llm-proxy")) < events.index(("replay", "_ReplayDb"))
    assert events.index(("routes", "llm-models")) < events.index(("replay", "_ReplayDb"))


class TestApiDocsGate:
    """/docs, /redoc and /openapi.json disclose the whole admin API schema and
    the edge proxies them straight through — they must stay off unless
    ENABLE_API_DOCS is set (it is not in tests, matching the default)."""

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    async def test_api_docs_endpoints_are_disabled_by_default(self, client, path):
        resp = await client.get(path)
        assert resp.status_code == 404


def test_the_boot_purge_reads_every_resource_type_kept_in_the_meta_db():
    """Each type with an id column in alerts.RESOURCE_ID_COLUMNS feeds the
    stale-state purge, under a keyword purge_stale_states takes."""
    import inspect

    from app.main import _PURGE_KEYWORDS
    from app.routers.alerts import RESOURCE_ID_COLUMNS
    from app.services.alert_state import purge_stale_states

    assert sorted(t for t, _ in _PURGE_KEYWORDS) == sorted(RESOURCE_ID_COLUMNS)
    params = inspect.signature(purge_stale_states).parameters
    assert all(keyword in params for _, keyword in _PURGE_KEYWORDS)
