"""Comprehensive tests for the gateway router (app/routers/gateway.py)."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.routers.gateway import (
    _extract_scalar,
    _extract_service_key,
    _extract_service_keys,
    _extract_timeseries,
    _get_step,
    _inject_plugins,
    _labels,
    _service_headers_for_route,
    _validate_consumer,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKeyAccess
from tests.conftest import auth_header


def _http_status(code: int, body: str = "boom") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://apisix")
    res = httpx.Response(code, request=req, text=body)
    return httpx.HTTPStatusError("err", request=req, response=res)


# ---------------------------------------------------------------------------
# Helper function unit tests (no HTTP, no mocking)
# ---------------------------------------------------------------------------


class TestExtractServiceKeys:
    def test_route_with_single_header(self):
        route = {
            "plugins": {
                "proxy-rewrite": {"headers": {"set": {"X-Api-Key": "supersecret1234"}}}
            }
        }
        result = _extract_service_keys(route)
        assert result == [{"header_name": "X-Api-Key", "header_value": "***1234"}]
        assert _extract_service_key(route) == {
            "header_name": "X-Api-Key",
            "header_value": "***1234",
        }

    def test_route_with_multiple_headers(self):
        route = {
            "plugins": {
                "proxy-rewrite": {
                    "headers": {
                        "set": {
                            "X-Api-Key": "supersecret1234",
                            "Authorization": "Bearer longvalue5678",
                        }
                    }
                }
            }
        }
        result = _extract_service_keys(route)
        by_name = {entry["header_name"]: entry["header_value"] for entry in result}
        assert by_name == {
            "X-Api-Key": "***1234",
            "Authorization": "***5678",
        }

    def test_route_without_plugins(self):
        assert _extract_service_keys({"uri": "/test"}) == []

    def test_route_with_malformed_plugins(self):
        assert _extract_service_keys({"plugins": None}) == []
        assert _extract_service_keys({"plugins": {"proxy-rewrite": None}}) == []
        assert (
            _extract_service_keys(
                {"plugins": {"proxy-rewrite": {"headers": "bad"}}}
            )
            == []
        )

    def test_route_with_empty_plugins(self):
        assert _extract_service_keys({"plugins": {}}) == []

    def test_route_with_proxy_rewrite_no_headers_set(self):
        assert _extract_service_keys({"plugins": {"proxy-rewrite": {}}}) == []

    def test_route_with_proxy_rewrite_empty_headers_set(self):
        assert (
            _extract_service_keys({"plugins": {"proxy-rewrite": {"headers": {"set": {}}}}})
            == []
        )

    def test_service_headers_for_route_returns_unmasked_values(self):
        route = {
            "plugins": {
                "proxy-rewrite": {
                    "headers": {
                        "set": {
                            "X-Api-Key": "supersecret1234",
                            "Authorization": "Bearer longvalue5678",
                        }
                    }
                }
            }
        }
        assert _service_headers_for_route(route) == {
            "X-Api-Key": "supersecret1234",
            "Authorization": "Bearer longvalue5678",
        }


class TestInjectPlugins:
    def test_with_single_service_key(self):
        body = {
            "uri": "/test",
            "service_keys": [{"header_name": "X-Api-Key", "header_value": "my-secret"}],
        }
        result = _inject_plugins(body)
        assert "service_keys" not in result
        assert (
            result["plugins"]["proxy-rewrite"]["headers"]["set"]["X-Api-Key"]
            == "my-secret"
        )

    def test_with_multiple_service_keys(self):
        body = {
            "uri": "/test",
            "service_keys": [
                {"header_name": "X-Api-Key", "header_value": "secret-a"},
                {"header_name": "Authorization", "header_value": "Bearer xyz"},
            ],
        }
        result = _inject_plugins(body)
        headers_set = result["plugins"]["proxy-rewrite"]["headers"]["set"]
        assert headers_set == {
            "X-Api-Key": "secret-a",
            "Authorization": "Bearer xyz",
        }

    def test_empty_value_preserves_existing_for_same_header(self):
        body = {
            "uri": "/test",
            "service_keys": [
                {"header_name": "X-Api-Key", "header_value": ""},
                {"header_name": "Authorization", "header_value": "Bearer new"},
            ],
        }
        existing = {
            "proxy-rewrite": {"headers": {"set": {"X-Api-Key": "old-secret"}}}
        }
        result = _inject_plugins(body, existing_plugins=existing)
        headers_set = result["plugins"]["proxy-rewrite"]["headers"]["set"]
        assert headers_set == {
            "X-Api-Key": "old-secret",
            "Authorization": "Bearer new",
        }

    def test_empty_list_clears_all_headers(self):
        body = {"uri": "/test", "service_keys": []}
        existing = {
            "proxy-rewrite": {"headers": {"set": {"X-Api-Key": "old"}}},
            "rate-limiting": {"rate": 5},
        }
        result = _inject_plugins(body, existing_plugins=existing)
        assert "headers" not in result["plugins"].get("proxy-rewrite", {})
        assert "rate-limiting" in result["plugins"]

    def test_omitted_service_keys_preserves_existing_headers(self):
        body = {"uri": "/test"}
        existing = {
            "proxy-rewrite": {"headers": {"set": {"X-Api-Key": "keep-me"}}}
        }
        result = _inject_plugins(body, existing_plugins=existing)
        assert (
            result["plugins"]["proxy-rewrite"]["headers"]["set"]
            == {"X-Api-Key": "keep-me"}
        )

    def test_malformed_existing_proxy_rewrite_is_ignored(self):
        body = {
            "uri": "/test",
            "service_keys": [{"header_name": "X-Key", "header_value": "new"}],
        }
        result = _inject_plugins(body, existing_plugins={"proxy-rewrite": None})
        assert result["plugins"]["proxy-rewrite"]["headers"]["set"] == {"X-Key": "new"}

    def test_malformed_existing_headers_is_ignored(self):
        body = {
            "uri": "/test",
            "service_keys": [{"header_name": "X-Key", "header_value": "new"}],
        }
        existing = {"proxy-rewrite": {"headers": "bad"}}
        result = _inject_plugins(body, existing_plugins=existing)
        assert result["plugins"]["proxy-rewrite"]["headers"]["set"] == {"X-Key": "new"}

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
            "service_keys": [{"header_name": "X-Key", "header_value": "val"}],
        }
        existing = {"rate-limiting": {"rate": 5}}
        result = _inject_plugins(body, existing_plugins=existing)
        assert "rate-limiting" in result["plugins"]
        assert "proxy-rewrite" in result["plugins"]

    def test_strip_prefix_preserves_percent_encoded_upstream_path(self):
        body = {"uri": "/api/datahub/*", "strip_prefix": True}
        result = _inject_plugins(body)

        proxy_rewrite = result["plugins"]["proxy-rewrite"]
        assert proxy_rewrite["regex_uri"] == ["^/api/datahub(.*)", "$1"]
        assert proxy_rewrite["use_real_request_uri_unsafe"] is True

    def test_strip_prefix_false_removes_encoded_path_preservation_flag(self):
        body = {"uri": "/api/datahub/*", "strip_prefix": False}
        existing = {
            "proxy-rewrite": {
                "regex_uri": ["^/api/datahub(.*)", "$1"],
                "use_real_request_uri_unsafe": True,
                "headers": {"set": {"X-Key": "secret"}},
            }
        }
        result = _inject_plugins(body, existing_plugins=existing)

        proxy_rewrite = result["plugins"]["proxy-rewrite"]
        assert "regex_uri" not in proxy_rewrite
        assert "use_real_request_uri_unsafe" not in proxy_rewrite
        assert proxy_rewrite["headers"] == {"set": {"X-Key": "secret"}}

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
            "service_keys": [{"header_name": "", "header_value": "val"}],
        }
        result = _inject_plugins(body)
        assert "plugins" not in result

    def test_empty_value_for_unknown_header_skipped(self):
        body = {
            "uri": "/test",
            "service_keys": [
                {"header_name": "X-New", "header_value": ""},
            ],
        }
        result = _inject_plugins(body)
        assert "plugins" not in result

    def test_preserves_headers_add_when_updating_set(self):
        body = {
            "uri": "/test",
            "service_keys": [{"header_name": "X-Set", "header_value": "v"}],
        }
        existing = {
            "proxy-rewrite": {
                "headers": {
                    "set": {"X-Old": "stale"},
                    "add": {"X-Trace": "1"},
                    "remove": ["X-Internal"],
                }
            }
        }
        pr = _inject_plugins(body, existing_plugins=existing)["plugins"]["proxy-rewrite"]
        assert pr["headers"]["set"] == {"X-Set": "v"}
        assert pr["headers"]["add"] == {"X-Trace": "1"}
        assert pr["headers"]["remove"] == ["X-Internal"]

    def test_empty_list_keeps_other_header_ops_but_drops_set(self):
        body = {"uri": "/test", "service_keys": []}
        existing = {
            "proxy-rewrite": {
                "headers": {
                    "set": {"X-Old": "stale"},
                    "add": {"X-Trace": "1"},
                }
            }
        }
        pr = _inject_plugins(body, existing_plugins=existing)["plugins"]["proxy-rewrite"]
        assert "set" not in pr["headers"]
        assert pr["headers"]["add"] == {"X-Trace": "1"}


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
        assert item0["service_keys"] == [
            {"header_name": "X-Api-Key", "header_value": "***1234"}
        ]
        assert item0["service_key"] == {
            "header_name": "X-Api-Key",
            "header_value": "***1234",
        }
        assert item0["require_auth"] is True
        item1 = data["items"][1]
        assert item1["service_keys"] == []
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
        assert data["service_keys"] == [
            {"header_name": "Authorization", "header_value": "***1234"}
        ]
        assert data["service_key"] == {
            "header_name": "Authorization",
            "header_value": "***1234",
        }
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
                side_effect=_http_status(404, "not found"),
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
                    "service_keys": [
                        {
                            "header_name": "X-Key",
                            "header_value": "newsecretkey1234",
                        }
                    ],
                    "require_auth": True,
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["require_auth"] is True
        assert data["service_keys"] == [
            {"header_name": "X-Key", "header_value": "***1234"}
        ]
        # Verify the body passed to put_resource had plugins injected
        call_body = mock_put.call_args[0][2]
        assert "service_keys" not in call_body
        assert "require_auth" not in call_body

    async def test_legacy_service_key_payload_is_accepted(self, client, admin_token):
        saved_route = {
            "id": "r1",
            "uri": "/test",
            "plugins": {
                "proxy-rewrite": {"headers": {"set": {"X-Key": "legacy-secret"}}},
            },
        }
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=_http_status(404, "not found"),
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
                        "header_value": "legacy-secret",
                    },
                },
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        call_body = mock_put.call_args[0][2]
        assert "service_key" not in call_body
        assert call_body["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Key": "legacy-secret"
        }

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
        # get_resource returns 404 (new route) so we proceed to the put,
        # which fails — the response is the put failure surfaced as 502.
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=_http_status(404, "not found"),
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


class TestSaveRouteValidation:
    """Verify save_route rejects malformed payloads with 400."""

    async def test_service_keys_must_be_list(self, client, admin_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={
                "uri": "/api/test/*",
                "upstream_id": "u1",
                "service_keys": {"header_name": "X", "header_value": "v"},
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "service_keys" in resp.json()["detail"]

    async def test_service_keys_entry_must_be_object(self, client, admin_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={
                "uri": "/api/test/*",
                "upstream_id": "u1",
                "service_keys": ["X-Api-Key: v"],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_legacy_service_key_must_be_object(self, client, admin_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={
                "uri": "/api/test/*",
                "upstream_id": "u1",
                "service_key": "X-Api-Key: v",
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_service_keys_missing_header_name_rejected(self, client, admin_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={
                "uri": "/api/test/*",
                "upstream_id": "u1",
                "service_keys": [{"header_value": "v"}],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "header_name" in resp.json()["detail"]

    async def test_service_keys_empty_header_name_rejected(self, client, admin_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={
                "uri": "/api/test/*",
                "upstream_id": "u1",
                "service_keys": [{"header_name": "   ", "header_value": "v"}],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_service_keys_non_string_value_rejected(self, client, admin_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={
                "uri": "/api/test/*",
                "upstream_id": "u1",
                "service_keys": [{"header_name": "X", "header_value": 123}],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_service_keys_duplicate_case_insensitive_rejected(
        self, client, admin_token
    ):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={
                "uri": "/api/test/*",
                "upstream_id": "u1",
                "service_keys": [
                    {"header_name": "X-Api-Key", "header_value": "a"},
                    {"header_name": "x-api-key", "header_value": "b"},
                ],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400
        assert "Duplicate" in resp.json()["detail"]


class TestSaveRouteExistingLookup:
    """Verify save_route distinguishes 404 (new route) from transient errors."""

    async def test_404_treated_as_new_route(self, client, admin_token):
        saved = {
            "id": "r1",
            "uri": "/api/test/*",
            "plugins": {},
        }
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=_http_status(404, "not found"),
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=deepcopy(saved),
            ),
        ):
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={"uri": "/api/test/*", "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200

    async def test_non_404_http_error_returns_502_before_put(
        self, client, admin_token
    ):
        put_mock = AsyncMock(return_value={"id": "r1", "plugins": {}})
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=_http_status(500, "etcd down"),
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                put_mock,
            ),
        ):
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={"uri": "/api/test/*", "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502
        # We must NOT proceed to PUT when the lookup failed for a non-404 reason;
        # otherwise we'd silently drop preserved service-key values.
        assert put_mock.await_count == 0

    async def test_connection_error_returns_502_before_put(
        self, client, admin_token
    ):
        put_mock = AsyncMock(return_value={"id": "r1", "plugins": {}})
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=ConnectionError("network down"),
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                put_mock,
            ),
        ):
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={"uri": "/api/test/*", "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502
        assert put_mock.await_count == 0


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
    def __init__(self, response, capture: dict[str, object]):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, **kwargs):
        self._capture["url"] = url
        self._capture["headers"] = kwargs.get("headers")
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

    async def test_forwards_service_headers_to_upstream_probe(self, client, admin_token):
        route = {
            "id": "external-api",
            "upstream_id": "external",
            "plugins": {
                "proxy-rewrite": {
                    "headers": {
                        "set": {
                            "X-Api-Key": "raw-secret",
                            "Authorization": "Bearer token",
                        }
                    }
                }
            },
        }
        upstream = {"id": "external", "nodes": {"api.example.test:443": 1}}
        response = SimpleNamespace(
            status_code=200, json=lambda: {"status": "ok"}, text="ok"
        )
        capture: dict[str, object] = {}

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
                "/admin/gateway/routes/external-api/test",
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        assert capture["headers"] == {
            "X-Api-Key": "raw-secret",
            "Authorization": "Bearer token",
            "Host": "localhost",
        }

    async def test_uses_litellm_liveliness_for_llm_proxy(self, client, admin_token):
        route = {"id": "llm-proxy", "upstream_id": "litellm"}
        upstream = {"id": "litellm", "scheme": "https", "nodes": {"litellm:4000": 1}}
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
        upstream = {"id": "litellm", "scheme": "https", "nodes": {"litellm:4000": 1}}
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

    async def test_clears_lingering_alert_state(self, client, admin_token):
        from app.routers import alerts as alerts_router
        from app.services.alert_state import AlertStateManager

        mgr = AlertStateManager()
        mgr.update("upstream_health", "u1", is_healthy=False, trigger_after_failures=1)
        assert mgr.get_status("upstream_health", "u1") == "alert"
        alerts_router.set_alert_state(mgr)
        try:
            with patch(
                "app.routers.gateway.apisix_client.delete_resource",
                new_callable=AsyncMock,
                return_value=None,
            ):
                resp = await client.delete(
                    "/admin/gateway/upstreams/u1", headers=auth_header(admin_token)
                )
            assert resp.status_code == 204
            assert mgr.get_entry("upstream_health", "u1") is None
        finally:
            alerts_router.set_alert_state(None)


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
    @pytest.fixture(autouse=True)
    def _patch_routes_listing(self):
        """Default empty routes listing so tests don't hit a real APISIX."""
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new=AsyncMock(return_value={"items": [], "total": 0}),
        ):
            yield

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

    async def test_missing_latency_returns_null(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "only-a"}, "value": [0, "200"]},
        ]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        only = data["routes"][0]
        assert only["latency_p50_ms"] is None
        assert only["latency_p95_ms"] is None

    async def test_nan_latency_returns_null(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "x"}, "value": [0, "100"]},
        ]
        p50_result = [
            {"metric": {"route": "x"}, "value": [0, "NaN"]},
        ]
        mock = AsyncMock(side_effect=[requests_result, [], p50_result, []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        assert data["routes"][0]["latency_p50_ms"] is None

    async def test_latency_entry_without_value_treated_as_null(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "x"}, "value": [0, "100"]},
        ]
        # p50 result is missing the `value` key entirely
        p50_result = [
            {"metric": {"route": "x"}},
        ]
        mock = AsyncMock(side_effect=[requests_result, [], p50_result, []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        assert data["routes"][0]["latency_p50_ms"] is None

    async def test_zero_requests_returns_empty(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "route-a"}, "value": [0, "0"]},
        ]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        data = resp.json()
        assert data["total_requests"] == 0
        assert data["routes"] == []

    async def test_invalid_range_falls_back_to_1h(self, client, admin_token):
        mock = AsyncMock(side_effect=[[], [], [], []])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=bogus",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        first_call_query = mock.call_args_list[0].args[0]
        assert "[1h]" in first_call_query

    async def test_prometheus_error_returns_502(self, client, admin_token):
        mock = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502

    async def test_forbidden_without_permission(self, client):
        resp = await client.get("/admin/gateway/metrics/routes-comparison?range=1h")
        assert resp.status_code in (401, 403)

    async def test_route_name_populated_from_apisix(self, client, admin_token):
        requests_result = [
            {"metric": {"route": "abc-uuid"}, "value": [0, "100"]},
            {"metric": {"route": "query-api"}, "value": [0, "50"]},
            {"metric": {"route": "no-name-id"}, "value": [0, "25"]},
        ]
        listing = {
            "items": [
                {"id": "abc-uuid", "name": "User Service"},
                {"id": "query-api", "name": "query-api"},
                {"id": "no-name-id"},
            ],
            "total": 3,
        }
        prom_mock = AsyncMock(side_effect=[requests_result, [], [], []])
        list_mock = AsyncMock(return_value=listing)
        with patch("app.routers.gateway.prometheus_client.instant_query", prom_mock), \
             patch("app.routers.gateway.apisix_client.list_resources", list_mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        by_route = {r["route"]: r for r in resp.json()["routes"]}
        assert by_route["abc-uuid"]["name"] == "User Service"
        assert by_route["query-api"]["name"] == "query-api"
        assert by_route["no-name-id"]["name"] is None

    async def test_apisix_failure_does_not_break_metrics(self, client, admin_token):
        requests_result = [{"metric": {"route": "x"}, "value": [0, "10"]}]
        prom_mock = AsyncMock(side_effect=[requests_result, [], [], []])
        list_mock = AsyncMock(side_effect=RuntimeError("apisix down"))
        with patch("app.routers.gateway.prometheus_client.instant_query", prom_mock), \
             patch("app.routers.gateway.apisix_client.list_resources", list_mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        row = resp.json()["routes"][0]
        assert row["route"] == "x"
        assert row["name"] is None
        assert row["requests"] == 10


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


class TestConsumerFilter:
    """Verify the optional consumer query parameter filters PromQL correctly."""

    async def test_summary_with_consumer(self, client, admin_token):
        total = [{"value": [0, "10"]}]
        err = [{"value": [0, "0"]}]
        lat = [{"value": [0, "5"]}]
        mock = AsyncMock(side_effect=[total, err, lat])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert 'consumer="alice"' in call.args[0]

    async def test_requests_with_consumer(self, client, admin_token):
        ts = [{"values": [[1000, "5"]]}]
        mock = AsyncMock(return_value=ts)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'consumer="alice"' in mock.call_args.args[0]

    async def test_status_codes_with_consumer(self, client, admin_token):
        results = [{"metric": {"code": "200"}, "value": [0, "5"]}]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/status-codes?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'consumer="alice"' in mock.call_args.args[0]

    async def test_latency_with_consumer(self, client, admin_token):
        p = [{"values": [[1000, "10"]]}]
        mock = AsyncMock(side_effect=[p, p, p])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/latency?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert 'consumer="alice"' in call.args[0]

    async def test_requests_total_with_consumer(self, client, admin_token):
        ts = [{"values": [[1000, "5"]]}]
        mock = AsyncMock(return_value=ts)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests-total?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'consumer="alice"' in mock.call_args.args[0]

    async def test_consumer_and_route_together(self, client, admin_token):
        total = [{"value": [0, "10"]}]
        err = [{"value": [0, "0"]}]
        lat = [{"value": [0, "5"]}]
        mock = AsyncMock(side_effect=[total, err, lat])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h&route=query-api&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            q = call.args[0]
            assert 'route="query-api"' in q
            assert 'consumer="alice"' in q
            # llm-proxy exclusion is replaced by explicit route filter
            assert 'route!="llm-proxy"' not in q

    async def test_invalid_consumer_returns_400(self, client, admin_token):
        resp = await client.get(
            '/admin/gateway/metrics/summary?range=1h&consumer="; drop',
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_no_consumer_excludes_llm_proxy(self, client, admin_token):
        total = [{"value": [0, "10"]}]
        err = [{"value": [0, "0"]}]
        lat = [{"value": [0, "5"]}]
        mock = AsyncMock(side_effect=[total, err, lat])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert 'route!="llm-proxy"' in call.args[0]

    async def test_routes_comparison_excludes_llm_proxy_by_default(self, client, admin_token):
        requests_result = [{"metric": {"route": "x"}, "value": [0, "10"]}]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        list_mock = AsyncMock(return_value={"items": [], "total": 0})
        with patch("app.routers.gateway.prometheus_client.instant_query", mock), \
             patch("app.routers.gateway.apisix_client.list_resources", list_mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # All 4 PromQL queries should include the llm-proxy exclusion
        for call in mock.call_args_list:
            assert 'route!="llm-proxy"' in call.args[0]

    async def test_routes_comparison_with_consumer(self, client, admin_token):
        requests_result = [{"metric": {"route": "x"}, "value": [0, "10"]}]
        mock = AsyncMock(side_effect=[requests_result, [], [], []])
        list_mock = AsyncMock(return_value={"items": [], "total": 0})
        with patch("app.routers.gateway.prometheus_client.instant_query", mock), \
             patch("app.routers.gateway.apisix_client.list_resources", list_mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            q = call.args[0]
            assert 'consumer="alice"' in q
            assert 'route!="llm-proxy"' in q

    async def test_routes_comparison_invalid_consumer_returns_400(self, client, admin_token):
        resp = await client.get(
            '/admin/gateway/metrics/routes-comparison?range=1h&consumer=bad name',
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_top_routes_excludes_llm_proxy_by_default(self, client, admin_token):
        results = [{"metric": {"route": "x"}, "value": [0, "10"]}]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/top-routes?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'route!="llm-proxy"' in mock.call_args.args[0]

    async def test_top_routes_with_consumer(self, client, admin_token):
        results = [{"metric": {"route": "x"}, "value": [0, "10"]}]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/top-routes?range=1h&consumer=alice",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        q = mock.call_args.args[0]
        assert 'consumer="alice"' in q
        assert 'route!="llm-proxy"' in q

    async def test_top_routes_invalid_consumer_returns_400(self, client, admin_token):
        resp = await client.get(
            '/admin/gateway/metrics/top-routes?range=1h&consumer=bad name',
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

    async def test_day_bucket_uses_completed_range_and_partial_instant(self, client, admin_token):
        from datetime import datetime, timedelta, timezone

        kst = timezone(timedelta(hours=9))
        start = int(datetime(2026, 6, 10, 3, 0, tzinfo=kst).timestamp())
        end = int(datetime(2026, 6, 12, 12, 0, tzinfo=kst).timestamp())
        aligned_start = int(datetime(2026, 6, 10, 0, 0, tzinfo=kst).timestamp())
        current_start = int(datetime(2026, 6, 12, 0, 0, tzinfo=kst).timestamp())
        completed = [{
            "values": [
                [aligned_start, "1"],
                [aligned_start + 86400, "2"],
                [current_start, "3"],
            ],
        }]
        partial = [{"value": [end, "4"]}]
        range_mock = AsyncMock(return_value=completed)
        instant_mock = AsyncMock(return_value=partial)

        with patch("app.routers.gateway.prometheus_client.range_query", range_mock), \
             patch("app.routers.gateway.prometheus_client.instant_query", instant_mock):
            resp = await client.get(
                f"/admin/gateway/metrics/requests-total?start={start}&end={end}&bucket=day",
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        assert resp.json() == [
            {"timestamp": aligned_start, "value": 2.0},
            {"timestamp": aligned_start + 86400, "value": 3.0},
            {"timestamp": current_start, "value": 4.0},
        ]
        _, range_kwargs = range_mock.call_args
        assert range_kwargs["start"] == float(aligned_start)
        assert range_kwargs["end"] == float(current_start)
        assert range_kwargs["step"] == "86400s"
        assert "[1d]" in range_mock.call_args.args[0]
        assert instant_mock.call_args.kwargs["eval_time"] == float(end)
        assert "[43200s]" in instant_mock.call_args.args[0]

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

    # -- Admin role: has gateway.routes.read and gateway.routes.write --

    async def test_admin_can_read_routes(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value={"items": [], "total": 0},
        ):
            resp = await client.get(
                "/admin/gateway/routes", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200

    async def test_admin_can_read_upstreams(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value={"items": [], "total": 0},
        ):
            resp = await client.get(
                "/admin/gateway/upstreams", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200

    # -- User role: only has gateway.monitoring.self (own traffic) --

    async def test_user_can_read_monitoring(self, client, user_token):
        empty = [{"value": [0, "0"]}]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            side_effect=[empty, empty, empty],
        ):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(user_token),
            )
        assert resp.status_code == 200

    async def test_user_monitoring_forced_to_own_consumer(self, client, user_token, seeded_db):
        """A self-scoped user's PromQL is locked to their own consumer; a
        ?consumer= naming someone else is ignored (no cross-tenant leak)."""
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            db.add(ApiKeyAccess(consumer_name="self_testuser", owner="testuser"))
            await db.commit()
        empty = [{"value": [0, "0"]}]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            side_effect=[empty, empty, empty],
        ) as mock_q:
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h&consumer=attacker-key",
                headers=auth_header(user_token),
            )
        assert resp.status_code == 200
        queries = " ".join(str(c.args[0]) for c in mock_q.call_args_list)
        assert 'consumer="self_testuser"' in queries
        assert "attacker-key" not in queries

    async def test_user_with_no_key_is_scoped_to_no_match_sentinel(self, client, user_token):
        """A self-scoped user with no API key is force-scoped to the no-match
        sentinel, never to all traffic."""
        empty = [{"value": [0, "0"]}]
        with patch(
            "app.routers.gateway.prometheus_client.instant_query",
            new_callable=AsyncMock,
            side_effect=[empty, empty, empty],
        ) as mock_q:
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h",
                headers=auth_header(user_token),
            )
        assert resp.status_code == 200
        queries = " ".join(str(c.args[0]) for c in mock_q.call_args_list)
        assert 'consumer="__no_self_api_key__"' in queries

    async def test_user_cannot_read_llm_monitoring(self, client, user_token):
        # LLM monitoring stays admin-only (gateway.monitoring.read).
        resp = await client.get(
            "/admin/gateway/metrics/llm/summary?range=1h",
            headers=auth_header(user_token),
        )
        assert resp.status_code == 403

    async def test_user_cannot_read_llm_proxy_route(self, client, user_token):
        # A self-scoped user cannot pull their own llm-proxy gateway metrics either.
        resp = await client.get(
            "/admin/gateway/metrics/summary?range=1h&route=llm-proxy",
            headers=auth_header(user_token),
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("llm_route", ["llm-messages", "llm-responses"])
    async def test_user_cannot_read_llm_converter_routes(self, client, user_token, llm_route):
        # The converter routes are admin-scope only, same as llm-proxy.
        resp = await client.get(
            f"/admin/gateway/metrics/summary?range=1h&route={llm_route}",
            headers=auth_header(user_token),
        )
        assert resp.status_code == 403

    async def test_user_cannot_read_routes(self, client, user_token):
        resp = await client.get(
            "/admin/gateway/routes", headers=auth_header(user_token)
        )
        assert resp.status_code == 403

    async def test_user_cannot_read_upstreams(self, client, user_token):
        resp = await client.get(
            "/admin/gateway/upstreams", headers=auth_header(user_token)
        )
        assert resp.status_code == 403

    async def test_user_cannot_write_routes(self, client, user_token):
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json={"uri": "/test"},
            headers=auth_header(user_token),
        )
        assert resp.status_code == 403

    async def test_user_cannot_delete_routes(self, client, user_token):
        resp = await client.delete(
            "/admin/gateway/routes/r1", headers=auth_header(user_token)
        )
        assert resp.status_code == 403

    async def test_user_cannot_write_upstreams(self, client, user_token):
        resp = await client.put(
            "/admin/gateway/upstreams/u1",
            json={"nodes": {}},
            headers=auth_header(user_token),
        )
        assert resp.status_code == 403

    # -- Unauthenticated --

    async def test_unauthenticated_returns_401(self, client):
        resp = await client.get("/admin/gateway/routes")
        assert resp.status_code in (401, 403)


class TestResolveTimeWindow:
    def test_preset_window(self):
        from app.routers.gateway import resolve_time_window

        tw = resolve_time_window(time_range="6h", start=None, end=None)
        assert tw.is_custom is False
        assert tw.promql_window == "6h"
        assert tw.step == "300s"          # RANGE_STEPS["6h"]
        assert tw.volume_window == "30m"  # RANGE_VOLUME["6h"][1]
        assert tw.volume_step == "1800s"  # RANGE_VOLUME["6h"][0]
        assert tw.eval_time is None and tw.start is None and tw.end is None

    def test_invalid_preset_defaults_to_1h(self):
        from app.routers.gateway import resolve_time_window

        tw = resolve_time_window(time_range="nope", start=None, end=None)
        assert tw.promql_window == "1h"
        assert tw.step == "60s"

    def test_custom_window_maps_to_tier(self):
        from app.routers.gateway import resolve_time_window

        # 2 day span → tier "7d"
        start, end = 1_000_000, 1_000_000 + 2 * 86400
        tw = resolve_time_window(time_range="1h", start=start, end=end)
        assert tw.is_custom is True
        assert tw.promql_window == f"{2 * 86400}s"
        assert tw.step == "3600s"          # RANGE_STEPS["7d"]
        assert tw.volume_window == "1h"    # RANGE_VOLUME["7d"][1]
        assert tw.eval_time == float(end)
        assert tw.start == float(start) and tw.end == float(end)

    def test_custom_requires_both_bounds(self):
        from fastapi import HTTPException
        from app.routers.gateway import resolve_time_window

        with pytest.raises(HTTPException) as exc:
            resolve_time_window(time_range="1h", start=100, end=None)
        assert exc.value.status_code == 400

    def test_custom_rejects_reversed_range(self):
        from fastapi import HTTPException
        from app.routers.gateway import resolve_time_window

        with pytest.raises(HTTPException) as exc:
            resolve_time_window(time_range="1h", start=500, end=100)
        assert exc.value.status_code == 400

    def test_custom_rejects_future_end(self):
        import time as _t
        from fastapi import HTTPException
        from app.routers.gateway import resolve_time_window

        future = int(_t.time()) + 10_000
        with pytest.raises(HTTPException) as exc:
            resolve_time_window(time_range="1h", start=future - 3600, end=future)
        assert exc.value.status_code == 400

    def test_custom_rejects_tiny_span(self):
        from fastapi import HTTPException
        from app.routers.gateway import resolve_time_window

        with pytest.raises(HTTPException) as exc:
            resolve_time_window(time_range="1h", start=1000, end=1030)  # 30s
        assert exc.value.status_code == 400


class TestBucketWindow:
    def test_align_down_kst_hour_day_week(self):
        from datetime import datetime, timedelta, timezone
        from app.routers.gateway import _align_down_kst, _KST_OFFSET

        kst = timezone(timedelta(hours=9))
        # Wednesday 2026-06-17 03:05 KST
        t = datetime(2026, 6, 17, 3, 5, tzinfo=kst).timestamp()

        hour = _align_down_kst(t, "hour")
        assert hour % 3600 == 0
        assert datetime.fromtimestamp(hour, kst).strftime("%H:%M") == "03:00"

        day = _align_down_kst(t, "day")
        assert (day + _KST_OFFSET) % 86400 == 0  # KST midnight
        assert datetime.fromtimestamp(day, kst).strftime("%Y-%m-%d %H:%M") == "2026-06-17 00:00"

        week = _align_down_kst(t, "week")
        wk = datetime.fromtimestamp(week, kst)
        assert wk.strftime("%H:%M") == "00:00"
        assert wk.weekday() == 0  # Monday
        assert wk.strftime("%Y-%m-%d") == "2026-06-15"

    def test_resolve_preset_with_day_bucket(self):
        import time as _t
        from app.routers.gateway import resolve_time_window, _KST_OFFSET

        tw = resolve_time_window(time_range="30d", start=None, end=None, bucket="day")
        assert tw.bucket == "day"
        assert tw.is_custom is True
        assert tw.volume_step == "86400s"
        assert tw.step == "86400s"
        assert tw.volume_window == "1d"
        # start floors to a KST midnight; end remains the real eval time, not a future midnight
        assert (int(tw.start) + _KST_OFFSET) % 86400 == 0
        assert tw.end <= _t.time() + 1

    def test_resolve_custom_with_week_bucket_aligns_monday(self):
        from datetime import datetime, timedelta, timezone
        from app.routers.gateway import resolve_time_window

        kst = timezone(timedelta(hours=9))
        start = int(datetime(2026, 5, 6, 12, 0, tzinfo=kst).timestamp())  # Wed
        end = int(datetime(2026, 6, 3, 12, 0, tzinfo=kst).timestamp())    # Wed
        tw = resolve_time_window(time_range="1h", start=start, end=end, bucket="week")
        assert tw.bucket == "week"
        assert tw.volume_step == "604800s"
        assert datetime.fromtimestamp(tw.start, kst).weekday() == 0  # Monday
        assert tw.end == float(end)

    def test_invalid_bucket_falls_back_to_auto(self):
        from app.routers.gateway import resolve_time_window

        tw = resolve_time_window(time_range="6h", start=None, end=None, bucket="nope")
        assert tw.bucket == "auto"
        assert tw.volume_window == "30m"  # auto behavior preserved

    def test_bucket_points_shifts_and_drops_leading(self):
        from app.routers.gateway import _bucketed_window, _bucket_points

        tw = _bucketed_window(1_700_000_000.0, 1_700_000_000.0 + 3 * 86400, "day")
        start = int(tw.start)
        raw = [
            {"timestamp": start, "value": 1.0},               # leading: window before range → dropped
            {"timestamp": start + 86400, "value": 2.0},       # → bucket starting at `start`
            {"timestamp": start + 2 * 86400, "value": 3.0},   # → bucket starting at start+1d
        ]
        out = _bucket_points(raw, tw)
        assert out == [
            {"timestamp": start, "value": 2.0},
            {"timestamp": start + 86400, "value": 3.0},
        ]

    def test_bucket_points_noop_for_auto(self):
        from app.routers.gateway import resolve_time_window, _bucket_points

        tw = resolve_time_window(time_range="1h", start=None, end=None)
        pts = [{"timestamp": 100, "value": 1.0}]
        assert _bucket_points(pts, tw) == pts


class TestMetricsCustomRange:
    async def test_summary_custom_passes_eval_time(self, client, admin_token):
        scalar = [{"value": [1000, "5"]}]
        mock = AsyncMock(side_effect=[scalar, scalar, scalar])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?start=1000000&end=1003600",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        # every instant_query call evaluated at end=1003600
        for call in mock.call_args_list:
            assert call.kwargs.get("eval_time") == 1003600.0

    async def test_requests_custom_passes_start_end(self, client, admin_token):
        mock = AsyncMock(return_value=[{"values": [[1000000, "1"]]}])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/requests?start=1000000&end=1003600",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock.call_args.kwargs.get("start") == 1000000.0
        assert mock.call_args.kwargs.get("end") == 1003600.0

    async def test_summary_rejects_reversed_custom_range(self, client, admin_token):
        resp = await client.get(
            "/admin/gateway/metrics/summary?start=2000&end=1000",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400


class TestLlmMetricsCustomRange:
    async def test_llm_summary_custom_eval_time(self, client, admin_token):
        scalar = [{"value": [1000, "3"]}]
        mock = AsyncMock(side_effect=[scalar] * 7)
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/llm/summary?start=1000000&end=1003600",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert call.kwargs.get("eval_time") == 1003600.0

    async def test_llm_tokens_custom_start_end(self, client, admin_token):
        mock = AsyncMock(return_value=[{"values": [[1000000, "2"]]}])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/llm/tokens?start=1000000&end=1003600",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock.call_args.kwargs.get("start") == 1000000.0
        assert mock.call_args.kwargs.get("end") == 1003600.0

    async def test_llm_tokens_day_bucket_appends_partial_bucket(self, client, admin_token):
        from datetime import datetime, timedelta, timezone

        kst = timezone(timedelta(hours=9))
        start = int(datetime(2026, 6, 10, 3, 0, tzinfo=kst).timestamp())
        end = int(datetime(2026, 6, 12, 12, 0, tzinfo=kst).timestamp())
        aligned_start = int(datetime(2026, 6, 10, 0, 0, tzinfo=kst).timestamp())
        current_start = int(datetime(2026, 6, 12, 0, 0, tzinfo=kst).timestamp())
        completed = [{
            "values": [
                [aligned_start, "1"],
                [aligned_start + 86400, "2"],
                [current_start, "3"],
            ],
        }]
        partial = [{"value": [end, "4"]}]
        range_mock = AsyncMock(return_value=completed)
        instant_mock = AsyncMock(return_value=partial)

        with patch("app.routers.gateway.prometheus_client.range_query", range_mock), \
             patch("app.routers.gateway.prometheus_client.instant_query", instant_mock):
            resp = await client.get(
                f"/admin/gateway/metrics/llm/tokens?start={start}&end={end}&bucket=day",
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        expected = [
            {"timestamp": aligned_start, "value": 2.0},
            {"timestamp": aligned_start + 86400, "value": 3.0},
            {"timestamp": current_start, "value": 4.0},
        ]
        assert resp.json() == {"prompt": expected, "completion": expected}
        assert range_mock.call_count == 2
        assert instant_mock.call_count == 2
        for call in range_mock.call_args_list:
            assert call.kwargs["end"] == float(current_start)
            assert "[1d]" in call.args[0]
        for call in instant_mock.call_args_list:
            assert call.kwargs["eval_time"] == float(end)
            assert "[43200s]" in call.args[0]


class TestLabelsHelper:
    """_labels() builds PromQL label selectors with llm-proxy exclusion default."""

    def test_no_args_excludes_llm_proxy(self):
        assert _labels(None, None) == \
            '{route!="llm-proxy",route!="llm-messages",route!="llm-responses"}'

    def test_route_replaces_llm_proxy_exclusion(self):
        # Explicit route filter should not include the LLM exclusions
        assert _labels("query-api", None) == '{route="query-api"}'

    def test_consumer_adds_label(self):
        assert _labels(None, "alice") == \
            '{route!="llm-proxy",route!="llm-messages",route!="llm-responses",consumer="alice"}'

    def test_route_and_consumer(self):
        assert _labels("query-api", "alice") == '{route="query-api",consumer="alice"}'

    def test_extra_labels_prepended(self):
        # Existing usage: _labels(route, None, 'code=~"5.."')
        assert _labels(None, None, 'code=~"5.."') == \
            '{code=~"5..",route!="llm-proxy",route!="llm-messages",route!="llm-responses"}'
        assert _labels("query-api", "alice", 'code=~"5.."') == \
            '{code=~"5..",route="query-api",consumer="alice"}'


class TestValidateConsumer:
    def test_accepts_safe_names(self):
        for name in ("alice", "my-app", "user_1", "svc.prod", "ABC123"):
            _validate_consumer(name)  # no exception

    def test_none_is_allowed(self):
        _validate_consumer(None)

    def test_empty_string_is_allowed(self):
        # Empty string is falsy; treated like None
        _validate_consumer("")

    def test_rejects_unsafe_names(self):
        for bad in ('alice"; drop', "a b", "name/etc", "x\"y", "name;"):
            with pytest.raises(HTTPException) as ei:
                _validate_consumer(bad)
            assert ei.value.status_code == 400
