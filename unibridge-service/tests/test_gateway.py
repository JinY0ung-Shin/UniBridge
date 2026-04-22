"""Comprehensive tests for the gateway router (app/routers/gateway.py)."""

from __future__ import annotations

import math
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.routers.gateway import (
    _extract_scalar,
    _extract_service_key,
    _extract_timeseries,
    _get_step,
    _inject_plugins,
)
from tests.conftest import auth_header


# ---------------------------------------------------------------------------
# Helper function unit tests (no HTTP, no mocking)
# ---------------------------------------------------------------------------


class TestExtractServiceKey:
    def test_route_with_proxy_rewrite(self):
        route = {
            "plugins": {
                "proxy-rewrite": {"headers": {"set": {"X-Api-Key": "supersecret1234"}}}
            }
        }
        result = _extract_service_key(route)
        assert result is not None
        assert result["header_name"] == "X-Api-Key"
        assert result["header_value"] == "***1234"

    def test_route_without_plugins(self):
        route = {"uri": "/test"}
        assert _extract_service_key(route) is None

    def test_route_with_empty_plugins(self):
        route = {"plugins": {}}
        assert _extract_service_key(route) is None

    def test_route_with_proxy_rewrite_no_headers_set(self):
        route = {"plugins": {"proxy-rewrite": {}}}
        assert _extract_service_key(route) is None

    def test_route_with_proxy_rewrite_empty_headers_set(self):
        route = {"plugins": {"proxy-rewrite": {"headers": {"set": {}}}}}
        assert _extract_service_key(route) is None


class TestInjectPlugins:
    def test_with_service_key(self):
        body = {
            "uri": "/test",
            "service_key": {"header_name": "X-Api-Key", "header_value": "my-secret"},
        }
        result = _inject_plugins(body)
        assert "service_key" not in result
        assert (
            result["plugins"]["proxy-rewrite"]["headers"]["set"]["X-Api-Key"]
            == "my-secret"
        )

    def test_with_require_auth_true(self):
        body = {"uri": "/test", "require_auth": True}
        result = _inject_plugins(body)
        assert "require_auth" not in result
        assert "key-auth" in result["plugins"]

    def test_with_require_auth_false_removes_key_auth(self):
        body = {"uri": "/test", "require_auth": False}
        existing = {"key-auth": {}, "rate-limiting": {"rate": 10}}
        result = _inject_plugins(body, existing_plugins=existing)
        assert "key-auth" not in result["plugins"]
        assert "rate-limiting" in result["plugins"]

    def test_require_auth_none_preserves_existing(self):
        body = {"uri": "/test"}
        existing = {"key-auth": {}}
        result = _inject_plugins(body, existing_plugins=existing)
        assert "key-auth" in result["plugins"]

    def test_preserves_existing_plugins(self):
        body = {
            "uri": "/test",
            "service_key": {"header_name": "X-Key", "header_value": "val"},
        }
        existing = {"rate-limiting": {"rate": 5}}
        result = _inject_plugins(body, existing_plugins=existing)
        assert "rate-limiting" in result["plugins"]
        assert "proxy-rewrite" in result["plugins"]

    def test_no_plugins_removes_key(self):
        body = {"uri": "/test"}
        result = _inject_plugins(body)
        assert "plugins" not in result

    def test_clears_plugins_key_when_empty(self):
        body = {"uri": "/test", "plugins": {}, "require_auth": False}
        result = _inject_plugins(body, existing_plugins=None)
        assert "plugins" not in result

    def test_service_key_missing_header_name_ignored(self):
        body = {
            "uri": "/test",
            "service_key": {"header_name": "", "header_value": "val"},
        }
        result = _inject_plugins(body)
        assert "plugins" not in result


class TestExtractScalar:
    def test_valid_data(self):
        results = [{"value": [1234567890, "42.5"]}]
        assert _extract_scalar(results) == 42.5

    def test_empty_results(self):
        assert _extract_scalar([]) == 0.0

    def test_nan_value(self):
        results = [{"value": [1234567890, "NaN"]}]
        assert _extract_scalar(results) == 0.0

    def test_missing_value_key(self):
        results = [{}]
        # When "value" is missing, defaults to [0, "0"]
        assert _extract_scalar(results) == 0.0

    def test_non_numeric_string(self):
        results = [{"value": [1234567890, "not-a-number"]}]
        assert _extract_scalar(results) == 0.0


class TestExtractTimeseries:
    def test_valid_data(self):
        results = [
            {
                "values": [
                    [1000, "10.1234"],
                    [1060, "20.5678"],
                ]
            }
        ]
        points = _extract_timeseries(results)
        assert len(points) == 2
        assert points[0] == {"timestamp": 1000, "value": 10.1234}
        assert points[1] == {"timestamp": 1060, "value": 20.5678}

    def test_empty_results(self):
        assert _extract_timeseries([]) == []

    def test_nan_in_values(self):
        results = [{"values": [[1000, "NaN"]]}]
        points = _extract_timeseries(results)
        assert points[0]["value"] == 0.0

    def test_non_numeric_value(self):
        results = [{"values": [[1000, "bad"]]}]
        points = _extract_timeseries(results)
        assert points[0]["value"] == 0.0

    def test_empty_values(self):
        results = [{"values": []}]
        assert _extract_timeseries(results) == []


class TestGetStep:
    def test_15m(self):
        assert _get_step("15m") == "15s"

    def test_1h(self):
        assert _get_step("1h") == "60s"

    def test_6h(self):
        assert _get_step("6h") == "300s"

    def test_24h(self):
        assert _get_step("24h") == "600s"

    def test_unknown_defaults_to_60s(self):
        assert _get_step("99h") == "60s"


# ---------------------------------------------------------------------------
# Route CRUD endpoint tests (mock apisix_client)
# ---------------------------------------------------------------------------


class TestListRoutes:
    async def test_returns_enriched_routes(self, client, admin_token):
        mock_data = {
            "items": [
                {
                    "id": "r1",
                    "uri": "/api/v1/test",
                    "plugins": {
                        "key-auth": {},
                        "proxy-rewrite": {
                            "headers": {"set": {"X-Api-Key": "longapikey1234"}}
                        },
                    },
                },
                {
                    "id": "r2",
                    "uri": "/api/v1/open",
                    "plugins": {},
                },
            ],
            "total": 2,
        }
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value=deepcopy(mock_data),
        ):
            resp = await client.get(
                "/admin/gateway/routes", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        item0 = data["items"][0]
        assert item0["service_key"]["header_name"] == "X-Api-Key"
        assert item0["service_key"]["header_value"] == "***1234"
        assert item0["require_auth"] is True
        item1 = data["items"][1]
        assert item1["service_key"] is None
        assert item1["require_auth"] is False

    async def test_apisix_connection_error_returns_502(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            side_effect=ConnectionError("refused"),
        ):
            resp = await client.get(
                "/admin/gateway/routes", headers=auth_header(admin_token)
            )
        assert resp.status_code == 502
        assert "APISIX" in resp.json()["detail"]


class TestGetRoute:
    async def test_returns_enriched_route(self, client, admin_token):
        route = {
            "id": "r1",
            "uri": "/test",
            "plugins": {
                "key-auth": {},
                "proxy-rewrite": {
                    "headers": {"set": {"Authorization": "Bearer longtoken1234"}}
                },
            },
        }
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(route),
        ):
            resp = await client.get(
                "/admin/gateway/routes/r1", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["service_key"]["header_name"] == "Authorization"
        assert data["service_key"]["header_value"] == "***1234"
        assert data["require_auth"] is True


class TestSaveRoute:
    async def test_injects_plugins_and_returns_enriched(self, client, admin_token):
        saved_route = {
            "id": "r1",
            "uri": "/test",
            "plugins": {
                "key-auth": {},
                "proxy-rewrite": {"headers": {"set": {"X-Key": "newsecretkey1234"}}},
            },
        }
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=Exception("not found"),
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=deepcopy(saved_route),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={
                    "uri": "/api/test/*",
                    "upstream_id": "u1",
                    "service_key": {
                        "header_name": "X-Key",
                        "header_value": "newsecretkey1234",
                    },
                    "require_auth": True,
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["require_auth"] is True
        assert data["service_key"]["header_value"] == "***1234"
        # Verify the body passed to put_resource had plugins injected
        call_body = mock_put.call_args[0][2]
        assert "service_key" not in call_body
        assert "require_auth" not in call_body

    async def test_inline_upstream_rejected(self, client, admin_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={"uri": "/test", "upstream": {"nodes": {"httpbin.org:80": 1}}},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "Inline upstream" in resp.json()["detail"]

    async def test_inline_nodes_rejected(self, client, admin_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={"uri": "/test", "nodes": {"httpbin.org:80": 1}},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_preserves_existing_plugins_on_update(self, client, admin_token):
        existing_route = {
            "id": "r1",
            "uri": "/test",
            "plugins": {
                "rate-limiting": {"rate": 5},
                "key-auth": {},
            },
        }
        saved_route = {
            "id": "r1",
            "uri": "/test",
            "plugins": {
                "rate-limiting": {"rate": 5},
                "key-auth": {},
            },
        }
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                return_value=deepcopy(existing_route),
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=deepcopy(saved_route),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={"uri": "/api/test/*", "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # The body sent to APISIX should have preserved rate-limiting and key-auth
        call_body = mock_put.call_args[0][2]
        assert "rate-limiting" in call_body.get("plugins", {})
        assert "key-auth" in call_body.get("plugins", {})

    async def test_apisix_error_returns_502(self, client, admin_token):
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=Exception("timeout"),
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                side_effect=ConnectionError("connection refused"),
            ),
        ):
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={"uri": "/api/test/*", "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502


class TestDeleteRoute:
    async def test_returns_204(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.delete_resource",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = await client.delete(
                "/admin/gateway/routes/r1", headers=auth_header(admin_token)
            )
        assert resp.status_code == 204
        assert resp.content == b""

    async def test_apisix_connection_error_returns_502(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.delete_resource",
            new_callable=AsyncMock,
            side_effect=ConnectionError("refused"),
        ):
            resp = await client.delete(
                "/admin/gateway/routes/r1", headers=auth_header(admin_token)
            )
        assert resp.status_code == 502

    async def test_system_managed_llm_admin_route_cannot_be_deleted(
        self, client, admin_token
    ):
        resp = await client.delete(
            "/admin/gateway/routes/llm-admin", headers=auth_header(admin_token)
        )

        assert resp.status_code == 400
        assert "System-managed route" in resp.json()["detail"]


class _MockAsyncClient:
    def __init__(self, response, capture: dict[str, str]):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str):
        self._capture["url"] = url
        return self._response


class TestRouteTest:
    async def test_uses_http_health_for_query_api(self, client, admin_token):
        route = {"id": "query-api", "upstream_id": "query-service"}
        upstream = {"id": "query-service", "nodes": {"unibridge-service:8000": 1}}
        response = SimpleNamespace(
            status_code=200, json=lambda: {"status": "ok"}, text="ok"
        )
        capture: dict[str, str] = {}

        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=[route, upstream],
            ),
            patch(
                "app.routers.gateway.httpx.AsyncClient",
                side_effect=lambda *args, **kwargs: _MockAsyncClient(response, capture),
            ),
        ):
            resp = await client.post(
                "/admin/gateway/routes/query-api/test", headers=auth_header(admin_token)
            )

        assert resp.status_code == 200
        assert resp.json()["reachable"] is True
        assert capture["url"] == "http://unibridge-service:8000/health"

    async def test_uses_litellm_liveliness_for_llm_proxy(self, client, admin_token):
        route = {"id": "llm-proxy", "upstream_id": "litellm"}
        upstream = {"id": "litellm", "nodes": {"litellm:4000": 1}}
        response = SimpleNamespace(
            status_code=200, json=lambda: {"status": "ok"}, text="ok"
        )
        capture: dict[str, str] = {}

        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=[route, upstream],
            ),
            patch(
                "app.routers.gateway.httpx.AsyncClient",
                side_effect=lambda *args, **kwargs: _MockAsyncClient(response, capture),
            ),
        ):
            resp = await client.post(
                "/admin/gateway/routes/llm-proxy/test", headers=auth_header(admin_token)
            )

        assert resp.status_code == 200
        assert resp.json()["reachable"] is True
        assert capture["url"] == "https://litellm:4000/health/liveliness"

    async def test_uses_litellm_liveliness_for_llm_admin(self, client, admin_token):
        route = {"id": "llm-admin", "upstream_id": "litellm"}
        upstream = {"id": "litellm", "nodes": {"litellm:4000": 1}}
        response = SimpleNamespace(
            status_code=200, json=lambda: {"status": "ok"}, text="ok"
        )
        capture: dict[str, str] = {}

        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=[route, upstream],
            ),
            patch(
                "app.routers.gateway.httpx.AsyncClient",
                side_effect=lambda *args, **kwargs: _MockAsyncClient(response, capture),
            ),
        ):
            resp = await client.post(
                "/admin/gateway/routes/llm-admin/test", headers=auth_header(admin_token)
            )

        assert resp.status_code == 200
        assert resp.json()["reachable"] is True
        assert capture["url"] == "https://litellm:4000/health/liveliness"


# ---------------------------------------------------------------------------
# Upstream CRUD endpoint tests
# ---------------------------------------------------------------------------


class TestListUpstreams:
    async def test_returns_upstreams(self, client, admin_token):
        mock_data = {
            "items": [{"id": "u1", "nodes": {"httpbin.org:80": 1}}],
            "total": 1,
        }
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value=mock_data,
        ):
            resp = await client.get(
                "/admin/gateway/upstreams", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


class TestGetUpstream:
    async def test_returns_upstream(self, client, admin_token):
        upstream = {"id": "u1", "nodes": {"httpbin.org:80": 1}}
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            return_value=upstream,
        ):
            resp = await client.get(
                "/admin/gateway/upstreams/u1", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == "u1"


class TestSaveUpstream:
    async def test_saves_and_returns(self, client, admin_token):
        upstream = {"id": "u1", "nodes": {"httpbin.org:80": 1}, "type": "roundrobin"}
        with patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            return_value=upstream,
        ):
            resp = await client.put(
                "/admin/gateway/upstreams/u1",
                json={"nodes": {"httpbin.org:80": 1}, "type": "roundrobin"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert resp.json()["type"] == "roundrobin"


class TestDeleteUpstream:
    async def test_returns_204(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.delete_resource",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = await client.delete(
                "/admin/gateway/upstreams/u1", headers=auth_header(admin_token)
            )
        assert resp.status_code == 204
        assert resp.content == b""


# ---------------------------------------------------------------------------
# Metrics endpoint tests (mock prometheus_client)
# ---------------------------------------------------------------------------


class TestMetricsSummary:
    async def test_returns_summary(self, client, admin_token):
        total = [{"value": [1000, "1500"]}]
        error_rate = [{"value": [1000, "2.35"]}]
        latency = [{"value": [1000, "45.678"]}]

        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            side_effect=[total, error_rate, latency],
        ):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 1500
        assert data["error_rate"] == 2.35
        assert data["avg_latency_ms"] == 45.68

    async def test_invalid_range_defaults_to_1h(self, client, admin_token):
        empty = [{"value": [0, "0"]}]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            side_effect=[empty, empty, empty],
        ):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=invalid",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200

    async def test_prometheus_error_returns_502(self, client, admin_token):
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            side_effect=ConnectionError("prometheus down"),
        ):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502
        assert "Prometheus" in resp.json()["detail"]


class TestMetricsRequests:
    async def test_returns_timeseries(self, client, admin_token):
        ts_data = [{"values": [[1000, "10"], [1060, "20"]]}]
        with patch(
            "app.routers.gateway.prometheus_client.range_query",
            new_callable=AsyncMock,
            return_value=ts_data,
        ):
            resp = await client.get(
                "/admin/gateway/metrics/requests?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["timestamp"] == 1000
        assert data[0]["value"] == 10.0


class TestMetricsStatusCodes:
    async def test_returns_sorted_codes(self, client, admin_token):
        results = [
            {"metric": {"code": "200"}, "value": [0, "500"]},
            {"metric": {"code": "500"}, "value": [0, "10"]},
            {"metric": {"code": "404"}, "value": [0, "50"]},
        ]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            return_value=results,
        ):
            resp = await client.get(
                "/admin/gateway/metrics/status-codes?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        # Sorted by count descending
        assert data[0]["code"] == "200"
        assert data[0]["count"] == 500
        assert data[1]["code"] == "404"
        assert data[2]["code"] == "500"


class TestMetricsLatency:
    async def test_returns_percentile_timeseries(self, client, admin_token):
        p50_data = [{"values": [[1000, "10.5"]]}]
        p95_data = [{"values": [[1000, "50.2"]]}]
        p99_data = [{"values": [[1000, "100.9"]]}]

        with patch(
            "app.routers.gateway.prometheus_client.range_query",
            new_callable=AsyncMock,
            side_effect=[p50_data, p95_data, p99_data],
        ):
            resp = await client.get(
                "/admin/gateway/metrics/latency?range=6h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "p50" in data
        assert "p95" in data
        assert "p99" in data
        assert data["p50"][0]["value"] == 10.5
        assert data["p95"][0]["value"] == 50.2
        assert data["p99"][0]["value"] == 100.9


class TestMetricsTopRoutes:
    async def test_returns_top_routes(self, client, admin_token):
        results = [
            {"metric": {"route": "route-1"}, "value": [0, "1000"]},
            {"metric": {"route": "route-2"}, "value": [0, "500"]},
        ]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            return_value=results,
        ):
            resp = await client.get(
                "/admin/gateway/metrics/top-routes?range=24h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["route"] == "route-1"
        assert data[0]["requests"] == 1000

    async def test_filters_zero_request_routes(self, client, admin_token):
        results = [
            {"metric": {"route": "active"}, "value": [0, "100"]},
            {"metric": {"route": "dead"}, "value": [0, "0"]},
        ]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            return_value=results,
        ):
            resp = await client.get(
                "/admin/gateway/metrics/top-routes?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        assert len(data) == 1
        assert data[0]["route"] == "active"


class TestMetricsRoutesComparison:
    async def test_returns_joined_route_metrics(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "route-a"}, "value": [0, "1000"]},
            {"metric": {"route": "route-b"}, "value": [0, "500"]},
        ]
        errors_result = [
            {"metric": {"route": "route-a"}, "value": [0, "10"]},
        ]
        p50_result = [
            {"metric": {"route": "route-a"}, "value": [0, "42.5"]},
            {"metric": {"route": "route-b"}, "value": [0, "30.0"]},
        ]
        p95_result = [
            {"metric": {"route": "route-a"}, "value": [0, "180.0"]},
            {"metric": {"route": "route-b"}, "value": [0, "60.0"]},
        ]

        mock = AsyncMock(side_effect=[requests_result, errors_result, p50_result, p95_result])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 1500
        assert len(data["routes"]) == 2
        by_route = {r["route"]: r for r in data["routes"]}

        a = by_route["route-a"]
        assert a["requests"] == 1000
        assert a["share"] == pytest.approx(66.67, rel=0.01)
        assert a["error_rate"] == pytest.approx(1.0, rel=0.01)
        assert a["latency_p50_ms"] == pytest.approx(42.5)
        assert a["latency_p95_ms"] == pytest.approx(180.0)

        b = by_route["route-b"]
        assert b["requests"] == 500
        assert b["error_rate"] == 0.0
        assert b["latency_p50_ms"] == pytest.approx(30.0)

    async def test_routes_sorted_by_requests_desc(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "small"}, "value": [0, "100"]},
            {"metric": {"route": "big"}, "value": [0, "900"]},
        ]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        assert [r["route"] for r in data["routes"]] == ["big", "small"]


# ---------------------------------------------------------------------------
# Route filter tests
# ---------------------------------------------------------------------------


class TestRouteFilter:
    """Verify the optional route query parameter filters PromQL correctly."""

    async def test_summary_with_route_filter(self, client, admin_token):
        total = [{"value": [0, "50"]}]
        error = [{"value": [0, "5.0"]}]
        latency = [{"value": [0, "30.0"]}]

        mock = AsyncMock(side_effect=[total, error, latency])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h&route=query-api",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # Verify route filter appears in all 3 PromQL queries
        for call in mock.call_args_list:
            assert 'route="query-api"' in call.args[0]

    async def test_requests_with_route_filter(self, client, admin_token):
        ts_data = [{"values": [[1000, "5"]]}]
        mock = AsyncMock(return_value=ts_data)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests?range=1h&route=query-api",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'route="query-api"' in mock.call_args.args[0]

    async def test_status_codes_with_route_filter(self, client, admin_token):
        results = [{"metric": {"code": "200"}, "value": [0, "100"]}]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/status-codes?range=1h&route=query-api",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'route="query-api"' in mock.call_args.args[0]

    async def test_latency_with_route_filter(self, client, admin_token):
        p_data = [{"values": [[1000, "10"]]}]
        mock = AsyncMock(side_effect=[p_data, p_data, p_data])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/latency?range=1h&route=query-api",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert 'route="query-api"' in call.args[0]

    async def test_no_route_filter_omits_label(self, client, admin_token):
        empty = [{"value": [0, "0"]}]
        mock = AsyncMock(side_effect=[empty, empty, empty])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert "route=" not in call.args[0]

    async def test_invalid_route_returns_400(self, client, admin_token):
        resp = await client.get(
            '/admin/gateway/metrics/summary?range=1h&route="; drop table',
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Request volume endpoint tests
# ---------------------------------------------------------------------------


class TestMetricsRequestsTotal:
    async def test_returns_timeseries(self, client, admin_token):
        ts_data = [{"values": [[1000, "150"], [1060, "200"]]}]
        mock = AsyncMock(return_value=ts_data)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests-total?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["value"] == 150.0
        # Verify increase() is used instead of rate()
        assert "increase(" in mock.call_args.args[0]

    async def test_with_route_filter(self, client, admin_token):
        ts_data = [{"values": [[1000, "50"]]}]
        mock = AsyncMock(return_value=ts_data)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests-total?range=1h&route=query-api",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'route="query-api"' in mock.call_args.args[0]

    async def test_long_range_uses_correct_step(self, client, admin_token):
        ts_data = [{"values": [[1000, "500"]]}]
        mock = AsyncMock(return_value=ts_data)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests-total?range=30d",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # 30d should use step=86400s (1d) and window=1d
        _, kwargs = mock.call_args
        assert kwargs["step"] == "86400s"
        assert "[1d]" in mock.call_args.args[0]

    async def test_prometheus_error_returns_502(self, client, admin_token):
        with patch(
            "app.routers.gateway.prometheus_client.range_query",
            new_callable=AsyncMock,
            side_effect=ConnectionError("down"),
        ):
            resp = await client.get(
                "/admin/gateway/metrics/requests-total?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Long-range time range tests
# ---------------------------------------------------------------------------


class TestLongRanges:
    """Verify 7d, 30d, 60d ranges are accepted and use correct steps."""

    async def test_summary_accepts_7d(self, client, admin_token):
        empty = [{"value": [0, "0"]}]
        mock = AsyncMock(side_effect=[empty, empty, empty])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=7d",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # 7d should appear in the increase window
        assert "[7d]" in mock.call_args_list[0].args[0]

    async def test_requests_accepts_60d(self, client, admin_token):
        ts_data = [{"values": [[1000, "1"]]}]
        mock = AsyncMock(return_value=ts_data)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests?range=60d",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        _, kwargs = mock.call_args
        assert kwargs["step"] == "43200s"

    async def test_top_routes_accepts_30d(self, client, admin_token):
        results = [{"metric": {"route": "r1"}, "value": [0, "100"]}]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/top-routes?range=30d",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert "[30d]" in mock.call_args.args[0]


# ---------------------------------------------------------------------------
# Permission / RBAC tests
# ---------------------------------------------------------------------------


class TestPermissions:
    """Verify role-based access control on gateway endpoints."""

    # -- Developer role: has gateway.routes.read but NOT gateway.routes.write --

    async def test_developer_can_read_routes(self, client, developer_token):
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value={"items": [], "total": 0},
        ):
            resp = await client.get(
                "/admin/gateway/routes", headers=auth_header(developer_token)
            )
        assert resp.status_code == 200

    async def test_developer_cannot_write_routes(self, client, developer_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={"uri": "/test", "upstream_id": "u1"},
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 403

    async def test_developer_cannot_delete_routes(self, client, developer_token):
        resp = await client.delete(
            "/admin/gateway/routes/r1", headers=auth_header(developer_token)
        )
        assert resp.status_code == 403

    async def test_developer_can_read_upstreams(self, client, developer_token):
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value={"items": [], "total": 0},
        ):
            resp = await client.get(
                "/admin/gateway/upstreams", headers=auth_header(developer_token)
            )
        assert resp.status_code == 200

    async def test_developer_cannot_write_upstreams(self, client, developer_token):
        resp = await client.put(
            "/admin/gateway/upstreams/u1",
            json={"nodes": {}},
            headers=auth_header(developer_token),
        )
        assert resp.status_code == 403

    async def test_developer_can_read_monitoring(self, client, developer_token):
        empty = [{"value": [0, "0"]}]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            side_effect=[empty, empty, empty],
        ):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(developer_token),
            )
        assert resp.status_code == 200

    # -- Viewer role: only has gateway.monitoring.read --

    async def test_viewer_can_read_monitoring(self, client, viewer_token):
        empty = [{"value": [0, "0"]}]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            side_effect=[empty, empty, empty],
        ):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(viewer_token),
            )
        assert resp.status_code == 200

    async def test_viewer_cannot_read_routes(self, client, viewer_token):
        resp = await client.get(
            "/admin/gateway/routes", headers=auth_header(viewer_token)
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_read_upstreams(self, client, viewer_token):
        resp = await client.get(
            "/admin/gateway/upstreams", headers=auth_header(viewer_token)
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_write_routes(self, client, viewer_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={"uri": "/test"},
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    # -- Unauthenticated --

    async def test_unauthenticated_returns_401(self, client):
        resp = await client.get("/admin/gateway/routes")
        assert resp.status_code in (401, 403)
