"""Extra coverage for gateway router: LLM metrics, route helpers, errors."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.routers.gateway import (
    PROTECTED_ROUTE_IDS,
    PROTECTED_UPSTREAM_IDS,
    _extract_scalar,
    _extract_service_key,
    _extract_strip_prefix,
    _extract_timeseries,
    _get_step,
    _labels,
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


def test_extract_service_key_present_and_absent():
    assert _extract_service_key({}) is None
    assert (
        _extract_service_key({"plugins": {"proxy-rewrite": {"headers": {"set": {}}}}})
        is None
    )
    out = _extract_service_key({
        "plugins": {"proxy-rewrite": {"headers": {"set": {"X-Token": "longvalue123"}}}}
    })
    assert out == {"header_name": "X-Token", "header_value": "***e123"}


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


def test_labels_with_and_without_route():
    assert _labels(None) == ""
    assert _labels(None, 'code=~"5.."') == '{code=~"5.."}'
    assert _labels("query-api") == '{route="query-api"}'
    out = _labels("query-api", 'code="200"')
    assert out.startswith("{") and out.endswith("}")
    parts = out.strip("{}").split(",")
    assert set(parts) == {'route="query-api"', 'code="200"'}


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
    body = {"uri": "/api/x", "upstream_id": "u1"}
    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=Exception("nope"))
        mock.put_resource = AsyncMock(side_effect=_http_status(500, "boom"))
        resp = await client.put(
            "/admin/gateway/routes/r1",
            json=body,
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502

    with patch("app.routers.gateway.apisix_client") as mock:
        mock.get_resource = AsyncMock(side_effect=Exception("nope"))
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
    assert "service_key" in data
    assert "require_auth" in data
    assert "strip_prefix" in data


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


@pytest.mark.asyncio
async def test_llm_metrics_summary_zero_count(client, admin_token):
    """latency_count = 0 → avg_latency = 0."""
    side_effects = [[{"value": [0, "0"]}]] * 7
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
    side_effects = [[{"value": [0, "0"]}]] * 7
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=side_effects):
        resp = await client.get(
            "/admin/gateway/metrics/llm/summary?range=zzz",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_llm_metrics_tokens(client, admin_token):
    series = [{"values": [[1.0, "100"]]}]
    with patch("app.routers.gateway.prometheus_client.range_query", new_callable=AsyncMock,
               side_effect=[series, series]):
        resp = await client.get(
            "/admin/gateway/metrics/llm/tokens?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt"] == [{"timestamp": 1.0, "value": 100.0}]
    assert body["completion"] == [{"timestamp": 1.0, "value": 100.0}]


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
        {"metric": {"model": "gpt-4"}, "value": [0, "5000"]},
        {"metric": {"model": "claude"}, "value": [0, "0"]},
        {"metric": {"model": "bad"}, "value": [0, "bogus"]},
    ]
    cost = [
        {"metric": {"model": "gpt-4"}, "value": [0, "12.345"]},
        {"metric": {"model": "missing-cost"}, "value": [0, "bad"]},
    ]
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=[tokens, cost]):
        resp = await client.get(
            "/admin/gateway/metrics/llm/by-model?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["model"] == "gpt-4"
    assert rows[0]["tokens"] == 5000
    assert rows[0]["cost"] == 12.345


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
        {"metric": {"hashed_api_key": "k1"}, "value": [0, "1000"]},
        {"metric": {"hashed_api_key": "k2"}, "value": [0, "0"]},
        {"metric": {"hashed_api_key": "k3"}, "value": [0, "bogus"]},
    ]
    requests = [
        {"metric": {"hashed_api_key": "k1"}, "value": [0, "50"]},
        {"metric": {"hashed_api_key": "k2"}, "value": [0, "bad"]},
    ]
    with patch("app.routers.gateway.prometheus_client.instant_query", new_callable=AsyncMock,
               side_effect=[tokens, requests]):
        resp = await client.get(
            "/admin/gateway/metrics/llm/top-keys?range=1h",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0] == {"api_key": "k1", "tokens": 1000, "requests": 50}


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
