"""Comprehensive tests for the gateway router (app/routers/gateway.py)."""
from __future__ import annotations

import math
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from app.routers.gateway import (
    _extract_api_key,
    _extract_scalar,
    _extract_service_key,
    _extract_timeseries,
    _get_step,
    _inject_consumer_key,
    _inject_plugins,
    _mask_value,
    _strip_consumer_secrets,
)
from tests.conftest import auth_header


# ---------------------------------------------------------------------------
# Helper function unit tests (no HTTP, no mocking)
# ---------------------------------------------------------------------------


class TestMaskValue:
    def test_long_value_keeps_last_four(self):
        assert _mask_value("abcdef1234") == "***1234"

    def test_short_value_fully_masked(self):
        assert _mask_value("abc") == "***"

    def test_exact_boundary_fully_masked(self):
        assert _mask_value("abcd") == "***"

    def test_five_chars_keeps_last_four(self):
        assert _mask_value("abcde") == "***bcde"


class TestExtractServiceKey:
    def test_route_with_proxy_rewrite(self):
        route = {
            "plugins": {
                "proxy-rewrite": {
                    "headers": {
                        "set": {"X-Api-Key": "supersecret1234"}
                    }
                }
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
        assert result["plugins"]["proxy-rewrite"]["headers"]["set"]["X-Api-Key"] == "my-secret"

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
        body = {"uri": "/test", "service_key": {"header_name": "", "header_value": "val"}}
        result = _inject_plugins(body)
        assert "plugins" not in result


class TestExtractApiKey:
    def test_masked(self):
        consumer = {"plugins": {"key-auth": {"key": "secret-api-key-1234"}}}
        result = _extract_api_key(consumer, mask=True)
        assert result == "***1234"

    def test_unmasked(self):
        consumer = {"plugins": {"key-auth": {"key": "secret-api-key-1234"}}}
        result = _extract_api_key(consumer, mask=False)
        assert result == "secret-api-key-1234"

    def test_no_key_auth_plugin(self):
        consumer = {"plugins": {}}
        assert _extract_api_key(consumer) is None

    def test_no_plugins_at_all(self):
        consumer = {"username": "test"}
        assert _extract_api_key(consumer) is None

    def test_key_auth_without_key(self):
        consumer = {"plugins": {"key-auth": {}}}
        assert _extract_api_key(consumer) is None


class TestInjectConsumerKey:
    def test_with_api_key(self):
        body = {"username": "alice", "api_key": "new-key-value"}
        result = _inject_consumer_key(body)
        assert "api_key" not in result
        assert result["plugins"]["key-auth"]["key"] == "new-key-value"

    def test_without_api_key(self):
        body = {"username": "alice"}
        result = _inject_consumer_key(body)
        assert "plugins" not in result

    def test_preserves_existing_plugins(self):
        body = {"username": "alice", "api_key": "key123"}
        existing = {"rate-limiting": {"rate": 10}}
        result = _inject_consumer_key(body, existing_plugins=existing)
        assert result["plugins"]["key-auth"]["key"] == "key123"
        assert result["plugins"]["rate-limiting"]["rate"] == 10

    def test_without_api_key_preserves_existing(self):
        body = {"username": "alice"}
        existing = {"key-auth": {"key": "old-key"}}
        result = _inject_consumer_key(body, existing_plugins=existing)
        assert result["plugins"]["key-auth"]["key"] == "old-key"


class TestStripConsumerSecrets:
    def test_removes_key(self):
        consumer = {"plugins": {"key-auth": {"key": "secret"}}}
        _strip_consumer_secrets(consumer)
        assert "key" not in consumer["plugins"]["key-auth"]

    def test_no_plugins(self):
        consumer = {"username": "test"}
        _strip_consumer_secrets(consumer)  # should not raise

    def test_no_key_auth(self):
        consumer = {"plugins": {"rate-limiting": {}}}
        _strip_consumer_secrets(consumer)
        assert "rate-limiting" in consumer["plugins"]


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
            resp = await client.get("/admin/gateway/routes", headers=auth_header(admin_token))
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
            resp = await client.get("/admin/gateway/routes", headers=auth_header(admin_token))
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
            resp = await client.get("/admin/gateway/routes/r1", headers=auth_header(admin_token))
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
                "proxy-rewrite": {
                    "headers": {"set": {"X-Key": "newsecretkey1234"}}
                },
            },
        }
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            side_effect=Exception("not found"),
        ), patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(saved_route),
        ) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={
                    "uri": "/api/test/*",
                    "upstream_id": "u1",
                    "service_key": {"header_name": "X-Key", "header_value": "newsecretkey1234"},
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
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(existing_route),
        ), patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(saved_route),
        ) as mock_put:
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
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            side_effect=Exception("timeout"),
        ), patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            side_effect=ConnectionError("connection refused"),
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
            resp = await client.delete("/admin/gateway/routes/r1", headers=auth_header(admin_token))
        assert resp.status_code == 204
        assert resp.content == b""

    async def test_apisix_connection_error_returns_502(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.delete_resource",
            new_callable=AsyncMock,
            side_effect=ConnectionError("refused"),
        ):
            resp = await client.delete("/admin/gateway/routes/r1", headers=auth_header(admin_token))
        assert resp.status_code == 502


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
            resp = await client.get("/admin/gateway/upstreams", headers=auth_header(admin_token))
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
            resp = await client.get("/admin/gateway/upstreams/u1", headers=auth_header(admin_token))
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
            resp = await client.delete("/admin/gateway/upstreams/u1", headers=auth_header(admin_token))
        assert resp.status_code == 204
        assert resp.content == b""


# ---------------------------------------------------------------------------
# Consumer CRUD endpoint tests
# ---------------------------------------------------------------------------


class TestListConsumers:
    async def test_masks_api_keys_and_strips_secrets(self, client, admin_token):
        mock_data = {
            "items": [
                {
                    "username": "alice",
                    "plugins": {"key-auth": {"key": "alice-secret-key-ABCD"}},
                },
                {
                    "username": "bob",
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
            resp = await client.get("/admin/gateway/consumers", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        alice = data["items"][0]
        assert alice["api_key"] == "***ABCD"
        # Secret should be stripped from plugins
        assert "key" not in alice["plugins"].get("key-auth", {})
        bob = data["items"][1]
        assert bob["api_key"] is None


class TestGetConsumer:
    async def test_returns_masked_consumer(self, client, admin_token):
        consumer = {
            "username": "alice",
            "plugins": {"key-auth": {"key": "alice-secret-key-ABCD"}},
        }
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(consumer),
        ):
            resp = await client.get(
                "/admin/gateway/consumers/alice", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_key"] == "***ABCD"
        assert "key" not in data["plugins"]["key-auth"]


class TestSaveConsumer:
    async def test_new_consumer_shows_unmasked_key(self, client, admin_token):
        """New consumer (get_resource raises) returns unmasked key and key_created=true."""
        saved = {
            "username": "newuser",
            "plugins": {"key-auth": {"key": "brand-new-key-5678"}},
        }
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            side_effect=Exception("not found"),
        ), patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(saved),
        ):
            resp = await client.put(
                "/admin/gateway/consumers/newuser",
                json={"api_key": "brand-new-key-5678"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_key"] == "brand-new-key-5678"
        assert data["key_created"] is True
        # Secret still stripped from plugins
        assert "key" not in data["plugins"]["key-auth"]

    async def test_existing_consumer_without_new_key_preserves(self, client, admin_token):
        """Existing consumer without api_key in body preserves existing key."""
        from httpx import HTTPStatusError

        existing = {
            "username": "alice",
            "plugins": {"key-auth": {"key": "existing-key-XYZW"}},
        }
        saved = {
            "username": "alice",
            "plugins": {"key-auth": {"key": "existing-key-XYZW"}},
        }
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(existing),
        ), patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(saved),
        ):
            resp = await client.put(
                "/admin/gateway/consumers/alice",
                json={"desc": "updated description"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        # No new key provided, not a new consumer -> masked, key_created=false
        assert data["api_key"] == "***XYZW"
        assert data["key_created"] is False

    async def test_existing_consumer_with_new_key(self, client, admin_token):
        """Existing consumer with new api_key returns unmasked key and key_created=true."""
        existing = {
            "username": "alice",
            "plugins": {"key-auth": {"key": "old-key-ABCD"}},
        }
        saved = {
            "username": "alice",
            "plugins": {"key-auth": {"key": "brand-new-key-9999"}},
        }
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(existing),
        ), patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(saved),
        ):
            resp = await client.put(
                "/admin/gateway/consumers/alice",
                json={"api_key": "brand-new-key-9999"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_key"] == "brand-new-key-9999"
        assert data["key_created"] is True


class TestDeleteConsumer:
    async def test_returns_204(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.delete_resource",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = await client.delete(
                "/admin/gateway/consumers/alice", headers=auth_header(admin_token)
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

    async def test_developer_can_read_consumers(self, client, developer_token):
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value={"items": [], "total": 0},
        ):
            resp = await client.get(
                "/admin/gateway/consumers", headers=auth_header(developer_token)
            )
        assert resp.status_code == 200

    async def test_developer_cannot_write_consumers(self, client, developer_token):
        resp = await client.put(
            "/admin/gateway/consumers/alice",
            json={"api_key": "test"},
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

    async def test_viewer_cannot_read_consumers(self, client, viewer_token):
        resp = await client.get(
            "/admin/gateway/consumers", headers=auth_header(viewer_token)
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_write_routes(self, client, viewer_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={"uri": "/test"},
            headers=auth_header(viewer_token),
        )
        assert resp.status_code == 403

    async def test_viewer_cannot_delete_consumers(self, client, viewer_token):
        resp = await client.delete(
            "/admin/gateway/consumers/alice", headers=auth_header(viewer_token)
        )
        assert resp.status_code == 403

    # -- Unauthenticated --

    async def test_unauthenticated_returns_401(self, client):
        resp = await client.get("/admin/gateway/routes")
        assert resp.status_code in (401, 403)
