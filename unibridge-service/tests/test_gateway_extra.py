"""Extra coverage for gateway router: LLM metrics, route helpers, errors."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.routers.api_keys import DENY_ALL_CONSUMER
from app.routers.gateway import (
    PROTECTED_ROUTE_IDS,
    PROTECTED_UPSTREAM_IDS,
    _extract_scalar,
    _extract_service_keys,
    _extract_strip_prefix,
    _extract_timeseries,
    _get_step,
    _mask_value,
    _validate_route,
)
from tests.conftest import auth_header


def _http_status(code: int, body: str = "boom") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://apisix")
    res = httpx.Response(code, request=req, text=body)
    return httpx.HTTPStatusError("err", request=req, response=res)


# ── Pure helpers ────────────────────────────────────────────────────────────


def test_mask_value_short_and_long():
    assert _mask_value("abc") == "***"
    assert _mask_value("supersecretkey") == "***tkey"


def test_extract_service_keys_present_and_absent():
    assert _extract_service_keys({}) == []
    assert (
        _extract_service_keys({"plugins": {"proxy-rewrite": {"headers": {"set": {}}}}})
        == []
    )
    out = _extract_service_keys({
        "plugins": {"proxy-rewrite": {"headers": {"set": {"X-Token": "longvalue123"}}}}
    })
    assert out == [{"header_name": "X-Token", "header_value": "***e123"}]

    multi = _extract_service_keys({
        "plugins": {
            "proxy-rewrite": {
                "headers": {
                    "set": {
                        "X-Token": "longvalue123",
                        "Authorization": "Bearer abcdef",
                    }
                }
            }
        }
    })
    assert {entry["header_name"] for entry in multi} == {"X-Token", "Authorization"}


def test_extract_strip_prefix_true_when_regex_uri():
    assert _extract_strip_prefix({"plugins": {"proxy-rewrite": {"regex_uri": ["a", "b"]}}})
    assert not _extract_strip_prefix({"plugins": {}})


def test_get_step_known_and_unknown():
    assert _get_step("1h") == "60s"
    assert _get_step("nonsense") == "60s"


def test_validate_route_accepts_safe_and_rejects_unsafe():
    _validate_route(None)
    _validate_route("query-api")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _validate_route("bad/route")
    assert exc.value.status_code == 400


def test_extract_scalar_handles_empty_and_invalid():
    assert _extract_scalar([]) == 0.0
    assert _extract_scalar([{"value": [0, "12.5"]}]) == 12.5
    assert _extract_scalar([{"value": [0, "not-a-num"]}]) == 0.0
    assert _extract_scalar([{}]) == 0.0


def test_extract_timeseries_normalizes():
    raw = [{"values": [[1.0, "5"], [2.0, "bad"], [3.0, "7"]]}]
    assert _extract_timeseries(raw) == [
        {"timestamp": 1.0, "value": 5.0},
        {"timestamp": 2.0, "value": 0.0},
        {"timestamp": 3.0, "value": 7.0},
    ]
    assert _extract_timeseries([]) == []


# ── Routes endpoints ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_routes_marks_system_routes(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.list_resources = AsyncMock(return_value={
            "items": [
                {"id": "query-api", "uri": "/q", "plugins": {}},
                {"id": "custom", "uri": "/c", "plugins": {}},
            ]
        })
        resp = await client.get("/admin/gateway/routes", headers=auth_header(admin_token))
    assert resp.status_code == 200
    items = {i["id"]: i for i in resp.json()["items"]}
    assert items["query-api"]["system"] is True
    assert items["custom"]["system"] is False


@pytest.mark.asyncio
async def test_list_routes_apisix_http_error(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.list_resources = AsyncMock(side_effect=_http_status(500, "x"))
        resp = await client.get("/admin/gateway/routes", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_list_routes_generic_error(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.list_resources = AsyncMock(side_effect=RuntimeError("nope"))
        resp = await client.get("/admin/gateway/routes", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_get_route_404(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=_http_status(404, "not found"))
        resp = await client.get("/admin/gateway/routes/missing", headers=auth_header(admin_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_route_generic_error(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=RuntimeError("ouch"))
        resp = await client.get("/admin/gateway/routes/x", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_save_route_inline_upstream_rejected(client, admin_token):
    resp = await client.put(
        "/admin/gateway/routes/r1",
        json={"upstream": {"nodes": {"x": 1}}, "uri": "/api/x"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_save_route_uri_prefix_required(client, admin_token):
    resp = await client.put(
        "/admin/gateway/routes/r1",
        json={"uri": "/x", "upstream_id": "u1"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_save_route_upstream_id_required(client, admin_token):
    resp = await client.put(
        "/admin/gateway/routes/r1",
        json={"uri": "/api/x"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_save_route_apisix_http_and_generic_errors(client, admin_token):
    # get_resource returns 404 (new route), then put_resource fails — both
    # error variants must surface as 502 from the put path.
    body = {"uri": "/api/x", "upstream_id": "u1"}
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=_http_status(404, "no"))
        mock.put_resource = AsyncMock(side_effect=_http_status(500, "boom"))
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json=body,
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=_http_status(404, "no"))
        mock.put_resource = AsyncMock(side_effect=RuntimeError("fail"))
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json=body,
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_save_route_success(client, admin_token):
    body = {"uri": "/api/x", "upstream_id": "u1"}
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(return_value={"plugins": {"prometheus": {}}})
        mock.put_resource = AsyncMock(return_value={
            "uri": "/api/x", "upstream_id": "u1", "plugins": {},
        })
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json=body,
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "service_keys" in data
    assert isinstance(data["service_keys"], list)
    assert "require_auth" in data
    assert "strip_prefix" in data


@pytest.mark.asyncio
async def test_save_keyauth_route_defaults_to_deny_all_when_no_master(client, admin_token):
    captured_body: dict = {}

    async def put_resource(_resource_type, _resource_id, body):
        captured_body.update(body)
        return dict(body)

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=_http_status(404, "missing"))
        mock.put_resource = AsyncMock(side_effect=put_resource)
        resp = await client.put(
            "/admin/gateway/routes/secure",
            json={"uri": "/api/secure/*", "upstream_id": "u1", "require_auth": True},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    assert captured_body["plugins"]["consumer-restriction"] == {
        "whitelist": [DENY_ALL_CONSUMER]
    }


@pytest.mark.asyncio
async def test_save_keyauth_route_whitelists_master_consumers(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as api_keys_apisix:
        api_keys_apisix.put_resource = AsyncMock(return_value={"username": "master-app"})
        api_keys_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        api_keys_apisix.list_resources = AsyncMock(return_value={"items": []})
        create_resp = await client.post(
            "/admin/api-keys",
            json={"name": "master-app", "api_key": "mk", "is_master": True},
            headers=auth_header(admin_token),
        )
    assert create_resp.status_code == 201

    captured_body: dict = {}

    async def put_resource(_resource_type, _resource_id, body):
        captured_body.update(body)
        return dict(body)

    with patch("app.routers.gateway.apisix_client") as gateway_apisix:
        gateway_apisix.get_resource = AsyncMock(side_effect=_http_status(404, "missing"))
        gateway_apisix.put_resource = AsyncMock(side_effect=put_resource)
        resp = await client.put(
            "/admin/gateway/routes/secure",
            json={"uri": "/api/secure/*", "upstream_id": "u1", "require_auth": True},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    assert captured_body["plugins"]["consumer-restriction"] == {
        "whitelist": ["master-app"]
    }


@pytest.mark.asyncio
async def test_delete_route_protected_blocks(client, admin_token):
    for rid in PROTECTED_ROUTE_IDS:
        resp = await client.delete(
            f"/admin/gateway/routes/{rid}",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_route_apisix_errors(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.delete_resource = AsyncMock(side_effect=_http_status(404, "gone"))
        resp = await client.delete("/admin/gateway/routes/foo", headers=auth_header(admin_token))
    assert resp.status_code == 404

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.delete_resource = AsyncMock(side_effect=RuntimeError("net"))
        resp = await client.delete("/admin/gateway/routes/foo", headers=auth_header(admin_token))
    assert resp.status_code == 502


# ── Route test endpoint ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_test_no_upstream(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(return_value={"uri": "/x", "plugins": {}})
        resp = await client.post("/admin/gateway/routes/r1/test", headers=auth_header(admin_token))
    assert resp.status_code == 400
    assert "upstream" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_route_test_apisix_404(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=_http_status(404, "no"))
        resp = await client.post("/admin/gateway/routes/r1/test", headers=auth_header(admin_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_test_apisix_generic(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=RuntimeError("net"))
        resp = await client.post("/admin/gateway/routes/r1/test", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_route_test_upstream_no_nodes(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=[
            {"uri": "/x", "upstream_id": "u1"},
            {"nodes": {}},
        ])
        resp = await client.post(
            "/admin/gateway/routes/r1/test",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 400
    assert "node" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_route_test_success_path(client, admin_token):
    fake_response = httpx.Response(200, json={"hello": "world"}, request=httpx.Request("GET", "http://x"))
    with patch("app.routers.gateway.apisix_client") as apisix, \
            patch("httpx.AsyncClient") as cls:
        apisix.get_resource = AsyncMock(side_effect=[
            {"uri": "/x", "upstream_id": "u1"},
            {"nodes": {"127.0.0.1:9000": 1}},
        ])
        instance = AsyncMock()
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        instance.get = AsyncMock(return_value=fake_response)
        cls.return_value = instance

        resp = await client.post(
            "/admin/gateway/routes/r1/test",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert body["status_code"] == 200


@pytest.mark.asyncio
async def test_route_test_uses_https_upstream_scheme(client, admin_token):
    fake_response = httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://x"))
    with patch("app.routers.gateway.apisix_client") as apisix, \
            patch("httpx.AsyncClient") as cls:
        apisix.get_resource = AsyncMock(side_effect=[
            {"uri": "/x", "upstream_id": "u1"},
            {"scheme": "https", "pass_host": "node", "nodes": {"secure.example.com:443": 1}},
        ])
        instance = AsyncMock()
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        instance.get = AsyncMock(return_value=fake_response)
        cls.return_value = instance

        resp = await client.post(
            "/admin/gateway/routes/r1/test",
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    instance.get.assert_awaited_once()
    assert instance.get.await_args.args[0] == "https://secure.example.com:443/health"
    assert instance.get.await_args.kwargs["headers"]["Host"] == "secure.example.com"


@pytest.mark.asyncio
async def test_route_test_uses_rewrite_upstream_host(client, admin_token):
    fake_response = httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://x"))
    with patch("app.routers.gateway.apisix_client") as apisix, \
            patch("httpx.AsyncClient") as cls:
        apisix.get_resource = AsyncMock(side_effect=[
            {"uri": "/x", "upstream_id": "u1"},
            {
                "scheme": "https",
                "pass_host": "rewrite",
                "upstream_host": "api.example.com",
                "nodes": {"10.0.0.10:443": 1},
            },
        ])
        instance = AsyncMock()
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        instance.get = AsyncMock(return_value=fake_response)
        cls.return_value = instance

        resp = await client.post(
            "/admin/gateway/routes/r1/test",
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 200
    instance.get.assert_awaited_once()
    assert instance.get.await_args.args[0] == "https://10.0.0.10:443/health"
    assert instance.get.await_args.kwargs["headers"]["Host"] == "api.example.com"


@pytest.mark.asyncio
async def test_route_test_request_failure(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as apisix, \
            patch("httpx.AsyncClient") as cls:
        apisix.get_resource = AsyncMock(side_effect=[
            {"uri": "/x", "upstream_id": "u1"},
            {"nodes": {"127.0.0.1:9000": 1}},
        ])
        instance = AsyncMock()
        instance.__aenter__.return_value = instance
        instance.__aexit__.return_value = None
        instance.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        cls.return_value = instance

        resp = await client.post(
            "/admin/gateway/routes/r1/test",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is False
    assert body["error"]


# ── Curl generation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_curl_basic(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(return_value={
            "uri": "/api/things/*",
            "methods": ["POST"],
            "plugins": {"key-auth": {}},
        })
        resp = await client.get("/admin/gateway/routes/x/curl", headers=auth_header(admin_token))
    assert resp.status_code == 200
    curl = resp.json()["curl"]
    assert "curl" in curl
    assert "-X POST" in curl
    assert "apikey: <YOUR_API_KEY>" in curl


@pytest.mark.asyncio
async def test_route_curl_default_get_no_keyauth(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(return_value={
            "uri": "/health",
            "methods": [],
            "plugins": {},
        })
        resp = await client.get("/admin/gateway/routes/y/curl", headers=auth_header(admin_token))
    assert resp.status_code == 200
    curl = resp.json()["curl"]
    assert "-X" not in curl  # default GET, no -X flag
    assert "apikey" not in curl


@pytest.mark.asyncio
async def test_route_curl_apisix_404_and_error(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=_http_status(404, "no"))
        resp = await client.get("/admin/gateway/routes/x/curl", headers=auth_header(admin_token))
    assert resp.status_code == 404

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=RuntimeError("ouch"))
        resp = await client.get("/admin/gateway/routes/x/curl", headers=auth_header(admin_token))
    assert resp.status_code == 502


# ── Upstream endpoints ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_upstreams_marks_system(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.list_resources = AsyncMock(return_value={
            "items": [{"id": "unibridge-service"}, {"id": "custom-up"}],
        })
        resp = await client.get("/admin/gateway/upstreams", headers=auth_header(admin_token))
    assert resp.status_code == 200
    items = {i["id"]: i["system"] for i in resp.json()["items"]}
    assert items["unibridge-service"] is True
    assert items["custom-up"] is False


@pytest.mark.asyncio
async def test_list_upstreams_errors(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.list_resources = AsyncMock(side_effect=_http_status(500))
        resp = await client.get("/admin/gateway/upstreams", headers=auth_header(admin_token))
    assert resp.status_code == 502

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.list_resources = AsyncMock(side_effect=RuntimeError("net"))
        resp = await client.get("/admin/gateway/upstreams", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_get_upstream_paths(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(return_value={"id": "u1", "type": "roundrobin"})
        resp = await client.get("/admin/gateway/upstreams/u1", headers=auth_header(admin_token))
    assert resp.status_code == 200

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=_http_status(404))
        resp = await client.get("/admin/gateway/upstreams/missing", headers=auth_header(admin_token))
    assert resp.status_code == 404

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=RuntimeError("?"))
        resp = await client.get("/admin/gateway/upstreams/x", headers=auth_header(admin_token))
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_save_upstream_paths(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.put_resource = AsyncMock(return_value={"id": "u1"})
        resp = await client.put(
            "/admin/gateway/upstreams/u1",
            json={"type": "roundrobin", "nodes": {"x:1": 1}},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.put_resource = AsyncMock(side_effect=_http_status(500))
        resp = await client.put(
            "/admin/gateway/upstreams/u1",
            json={"type": "roundrobin"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.put_resource = AsyncMock(side_effect=RuntimeError("?"))
        resp = await client.put(
            "/admin/gateway/upstreams/u1",
            json={"type": "roundrobin"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_delete_upstream_protected_blocks(client, admin_token):
    for uid in PROTECTED_UPSTREAM_IDS:
        resp = await client.delete(
            f"/admin/gateway/upstreams/{uid}",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_upstream_paths(client, admin_token):
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.delete_resource = AsyncMock()
        resp = await client.delete("/admin/gateway/upstreams/u1", headers=auth_header(admin_token))
    assert resp.status_code == 204

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.delete_resource = AsyncMock(side_effect=_http_status(404))
        resp = await client.delete("/admin/gateway/upstreams/u1", headers=auth_header(admin_token))
    assert resp.status_code == 404

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.delete_resource = AsyncMock(side_effect=RuntimeError("?"))
        resp = await client.delete("/admin/gateway/upstreams/u1", headers=auth_header(admin_token))
    assert resp.status_code == 502


# ── Generic metrics edge cases ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_requests_invalid_range_defaults(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               return_value=[]):
        resp = await client.get(
            "/admin/gateway/metrics/requests?range=zzz",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_metrics_requests_prometheus_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p down")):
        resp = await client.get(
            "/admin/gateway/metrics/requests?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_metrics_status_codes_filters_zero(client, admin_token):
    results = [
        {"metric": {"code": "200"}, "value": [0, "10"]},
        {"metric": {"code": "500"}, "value": [0, "0"]},
        {"metric": {"code": "404"}, "value": [0, "bogus"]},
    ]
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               return_value=results):
        resp = await client.get(
            "/admin/gateway/metrics/status-codes?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    codes = resp.json()
    assert {c["code"] for c in codes} == {"200"}


@pytest.mark.asyncio
async def test_metrics_status_codes_prometheus_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/status-codes?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_metrics_latency_prometheus_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/latency?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_metrics_top_routes_prometheus_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/top-routes?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_metrics_requests_total(client, admin_token):
    points = [{"values": [[1.0, "5"]]}]
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               return_value=points):
        resp = await client.get(
            "/admin/gateway/metrics/requests-total?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json() == [{"timestamp": 1.0, "value": 5.0}]


@pytest.mark.asyncio
async def test_metrics_requests_total_prom_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/requests-total?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


# ── LLM metrics endpoints ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_metrics_summary_success(client, admin_token):
    side_effects = [
        [{"value": [0, "12000"]}],  # tokens
        [{"value": [0, "8000"]}],   # prompt
        [{"value": [0, "4000"]}],   # completion
        [{"value": [0, "1.2345"]}], # spend
        [{"value": [0, "150"]}],    # requests
        [{"value": [0, "30"]}],     # latency_sum
        [{"value": [0, "10"]}],     # latency_count
        [{"value": [0, "2000"]}],   # cached
    ]
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=side_effects):
        resp = await client.get(
            "/admin/gateway/metrics/llm/summary?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_tokens"] == 12000
    assert data["estimated_cost"] == 1.2345
    assert data["avg_latency_ms"] == 3000.0  # 30/10*1000
    assert data["cached_tokens"] == 2000
    assert data["cache_hit_rate"] == round(2000 / 8000, 4)


@pytest.mark.asyncio
async def test_llm_metrics_summary_zero_count(client, admin_token):
    """latency_count = 0 → avg_latency = 0."""
    side_effects = [[{"value": [0, "0"]}]] * 8
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=side_effects):
        resp = await client.get(
            "/admin/gateway/metrics/llm/summary?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["avg_latency_ms"] == 0.0


@pytest.mark.asyncio
async def test_llm_metrics_summary_prometheus_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p down")):
        resp = await client.get(
            "/admin/gateway/metrics/llm/summary?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_llm_metrics_summary_invalid_range_defaults(client, admin_token):
    side_effects = [[{"value": [0, "0"]}]] * 8
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=side_effects):
        resp = await client.get(
            "/admin/gateway/metrics/llm/summary?range=zzz",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_llm_metrics_summary_cache_hit_rate_zero_prompt(client, admin_token):
    """cache_hit_rate is 0.0 when prompt tokens are 0 even if cached is present."""
    side_effects = [
        [{"value": [0, "0"]}],      # tokens
        [{"value": [0, "0"]}],      # prompt
        [{"value": [0, "0"]}],      # completion
        [{"value": [0, "0"]}],      # spend
        [{"value": [0, "0"]}],      # requests
        [{"value": [0, "0"]}],      # latency_sum
        [{"value": [0, "0"]}],      # latency_count
        [{"value": [0, "500"]}],    # cached
    ]
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=side_effects):
        resp = await client.get(
            "/admin/gateway/metrics/llm/summary?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["cached_tokens"] == 500
    assert data["cache_hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_llm_metrics_tokens(client, admin_token):
    series = [{"values": [[1.0, "100"]]}]
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=[series, series, series]):
        resp = await client.get(
            "/admin/gateway/metrics/llm/tokens?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt"] == [{"timestamp": 1.0, "value": 100.0}]
    assert body["completion"] == [{"timestamp": 1.0, "value": 100.0}]
    assert body["cached"] == [{"timestamp": 1.0, "value": 100.0}]


@pytest.mark.asyncio
async def test_llm_metrics_tokens_prom_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/llm/tokens?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_llm_metrics_by_model(client, admin_token):
    tokens = [
        {
            "metric": {
                "requested_model": "GaussO3.2-260402-vllm",
                "model": "GaussO3.2-260402",
            },
            "value": [0, "5000"],
        },
        {"metric": {"model": "claude"}, "value": [0, "0"]},
        {"metric": {"model": "bad"}, "value": [0, "bogus"]},
    ]
    cost = [
        {
            "metric": {
                "requested_model": "GaussO3.2-260402-vllm",
                "model": "GaussO3.2-260402",
            },
            "value": [0, "12.345"],
        },
        {"metric": {"model": "missing-cost"}, "value": [0, "bad"]},
    ]
    input_tokens = [
        {
            "metric": {
                "requested_model": "GaussO3.2-260402-vllm",
                "model": "GaussO3.2-260402",
            },
            "value": [0, "3000"],
        },
        {
            "metric": {"requested_model": "split-only"},
            "value": [0, "7"],
        },
    ]
    output_tokens = [
        {
            "metric": {
                "requested_model": "GaussO3.2-260402-vllm",
                "model": "GaussO3.2-260402",
            },
            "value": [0, "2000"],
        },
        {
            "metric": {"requested_model": "split-only"},
            "value": [0, "5"],
        },
    ]
    requests = [
        {
            "metric": {
                "requested_model": "GaussO3.2-260402-vllm",
                "model": "GaussO3.2-260402",
            },
            "value": [0, "25"],
        },
        {"metric": {"requested_model": "request-only"}, "value": [0, "3"]},
        {"metric": {"requested_model": "bad"}, "value": [0, "bogus"]},
    ]
    cached = [
        {
            "metric": {
                "requested_model": "GaussO3.2-260402-vllm",
                "model": "GaussO3.2-260402",
            },
            "value": [0, "1500"],
        },
    ]
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=[tokens, input_tokens, output_tokens, cost, requests, cached]) as prom_query:
        resp = await client.get(
            "/admin/gateway/metrics/llm/by-model?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 3
    assert rows[0]["model"] == "GaussO3.2-260402-vllm"
    assert rows[0]["tokens"] == 5000
    assert rows[0]["input_tokens"] == 3000
    assert rows[0]["output_tokens"] == 2000
    assert rows[0]["cost"] == 12.345
    assert rows[0]["requests"] == 25
    assert rows[0]["cached_tokens"] == 1500
    assert rows[1] == {
        "model": "split-only",
        "tokens": 12,
        "input_tokens": 7,
        "output_tokens": 5,
        "cost": 0.0,
        "requests": 0,
        "cached_tokens": 0,
    }
    assert rows[2] == {
        "model": "request-only",
        "tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost": 0.0,
        "requests": 3,
        "cached_tokens": 0,
    }
    assert all("sum by (requested_model, model)" in call.args[0] for call in prom_query.call_args_list)


@pytest.mark.asyncio
async def test_llm_metrics_by_model_prom_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/llm/by-model?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_llm_metrics_top_keys(client, admin_token):
    tokens = [
        {"metric": {"end_user": "customer-portal"}, "value": [0, "1000"]},
        {"metric": {"end_user": "internal-batch"}, "value": [0, "0"]},
        {"metric": {"end_user": "bad-key"}, "value": [0, "bogus"]},
    ]
    input_tokens = [
        {"metric": {"end_user": "customer-portal"}, "value": [0, "650"]},
        {"metric": {"end_user": "bad-key"}, "value": [0, "bogus"]},
    ]
    output_tokens = [
        {"metric": {"end_user": "customer-portal"}, "value": [0, "350"]},
    ]
    requests = [
        {"metric": {"end_user": "customer-portal"}, "value": [0, "50"]},
        {"metric": {"end_user": "internal-batch"}, "value": [0, "bad"]},
    ]
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=[tokens, input_tokens, output_tokens, requests]) as prom_query:
        resp = await client.get(
            "/admin/gateway/metrics/llm/top-keys?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0] == {
        "api_key": "customer-portal",
        "input_tokens": 650,
        "output_tokens": 350,
        "tokens": 1000,
        "requests": 50,
    }
    assert all("sum by (end_user)" in call.args[0] for call in prom_query.call_args_list)


@pytest.mark.asyncio
async def test_llm_metrics_top_keys_prom_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/llm/top-keys?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_llm_metrics_errors(client, admin_token):
    success = [{"values": [[1.0, "10"], [2.0, "20"]]}]
    error = [{"values": [[1.0, "1"], [2.0, "2"]]}]
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=[success, error]):
        resp = await client.get(
            "/admin/gateway/metrics/llm/errors?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert rows == [
        {"timestamp": 1.0, "success": 10, "error": 1},
        {"timestamp": 2.0, "success": 20, "error": 2},
    ]


@pytest.mark.asyncio
async def test_llm_metrics_errors_prom_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/llm/errors?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_llm_metrics_status_codes(client, admin_token):
    # Sourced from APISIX apisix_http_status (instant_query), broken out by code.
    results = [
        {"metric": {"code": "200"}, "value": [0, "300"]},
        {"metric": {"code": "429"}, "value": [0, "12"]},
        {"metric": {"code": "500"}, "value": [0, "3"]},
        {"metric": {"code": "204"}, "value": [0, "0"]},
    ]
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               return_value=results):
        resp = await client.get(
            "/admin/gateway/metrics/llm/status-codes?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    rows = resp.json()
    # Zero-count codes dropped, sorted by count desc.
    assert rows == [
        {"code": "200", "count": 300},
        {"code": "429", "count": 12},
        {"code": "500", "count": 3},
    ]


@pytest.mark.asyncio
async def test_llm_metrics_status_codes_prom_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/llm/status-codes?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_metrics_consumers_comparison(client, admin_token):
    requests_result = [
        {"metric": {"consumer": "alice"}, "value": [0, "800"]},
        {"metric": {"consumer": "bob"}, "value": [0, "200"]},
        {"metric": {"consumer": ""}, "value": [0, "100"]},
    ]
    errors_result = [
        {"metric": {"consumer": "alice"}, "value": [0, "8"]},
    ]
    p50_result = [
        {"metric": {"consumer": "alice"}, "value": [0, "12.0"]},
    ]
    p95_result = [
        {"metric": {"consumer": "alice"}, "value": [0, "40.0"]},
    ]
    # Global total is a separate query (> sum of the top rows here) so share stays
    # accurate when traffic exists beyond the top-10 consumers.
    total_result = [{"value": [0, "2000"]}]
    mock = AsyncMock(side_effect=[requests_result, errors_result, p50_result, p95_result, total_result])
    with patch("app.routers.gateway.prometheus_client.instant_query", mock):
        resp = await client.get(
            "/admin/gateway/metrics/consumers-comparison?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 2000
    by_consumer = {c["consumer"]: c for c in data["consumers"]}
    # Empty consumer label surfaces as the collision-proof "(no api key)" sentinel.
    assert set(by_consumer) == {"alice", "bob", "(no api key)"}
    assert by_consumer["alice"]["requests"] == 800
    # Share is relative to the global total (800/2000 = 40%), not the top-row sum.
    assert by_consumer["alice"]["share"] == pytest.approx(40.0, rel=0.01)
    assert by_consumer["alice"]["error_rate"] == pytest.approx(1.0, rel=0.01)
    assert by_consumer["alice"]["latency_p50_ms"] == pytest.approx(12.0)
    assert by_consumer["bob"]["error_rate"] == 0.0
    assert by_consumer["bob"]["latency_p50_ms"] is None
    # Sorted by requests desc.
    assert [c["consumer"] for c in data["consumers"]] == ["alice", "bob", "(no api key)"]


@pytest.mark.asyncio
async def test_metrics_consumers_comparison_prom_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/consumers-comparison?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_llm_metrics_requests_total(client, admin_token):
    series = [{"values": [[1.0, "42"]]}]
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               return_value=series):
        resp = await client.get(
            "/admin/gateway/metrics/llm/requests-total?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json() == [{"timestamp": 1.0, "value": 42.0}]


@pytest.mark.asyncio
async def test_llm_metrics_requests_total_prom_error(client, admin_token):
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=RuntimeError("p")):
        resp = await client.get(
            "/admin/gateway/metrics/llm/requests-total?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502
