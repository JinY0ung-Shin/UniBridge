"""Accuracy rules for the gateway / external-service monitoring queries.

- Latency selectors pin APISIX's end-to-end ``type="request"`` series (APISIX
  also records ``upstream`` and ``apisix`` latencies into the same histogram).
- Range-query rate windows span at least one step, so no traffic falls between
  samples at long ranges.
- routes-comparison lists every route and maps each label back to its route id.
- Quantile series report "no traffic" as null rather than 0ms.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.routers.gateway import (
    _extract_timeseries,
    _rate_window,
    _route_id_map,
    _route_name_map,
)
from tests.conftest import auth_header, fresh_module_copy

_GW_INSTANT = "app.routers.gateway.prometheus_client.instant_query"
_GW_RANGE = "app.routers.gateway.prometheus_client.range_query"
_EXT_RANGE = "app.routers.external_metrics.prometheus_client.range_query"
_LIST = "app.routers.gateway.apisix_client.list_resources"


@pytest.fixture(autouse=True)
def _empty_route_listing():
    """Default empty APISIX listing so no test reaches a real APISIX."""
    with patch(_LIST, new=AsyncMock(return_value={"items": [], "total": 0})):
        yield


class TestRequestLatencyType:
    async def test_summary_avg_latency_uses_request_series_only(self, client, admin_token):
        scalar = [{"value": [0, "1"]}]
        mock = AsyncMock(side_effect=[scalar, scalar, scalar])
        with patch(_GW_INSTANT, mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        total_q, error_q, latency_q = (c.args[0] for c in mock.call_args_list)
        assert "apisix_http_latency_sum{" in latency_q
        assert "apisix_http_latency_count{" in latency_q
        assert latency_q.count('type="request"') == 2
        assert "type=" not in total_q
        assert "type=" not in error_q

    async def test_summary_route_drilldown_keeps_type_filter(self, client, admin_token):
        scalar = [{"value": [0, "1"]}]
        mock = AsyncMock(side_effect=[scalar, scalar, scalar])
        with patch(_GW_INSTANT, mock):
            resp = await client.get(
                "/admin/gateway/metrics/summary?range=1h&route=query-api",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        latency_q = mock.call_args_list[2].args[0]
        assert latency_q.count('type="request"') == 2
        assert latency_q.count('route="query-api"') == 2

    async def test_latency_percentiles_use_request_series_only(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with patch(_GW_RANGE, mock):
            resp = await client.get(
                "/admin/gateway/metrics/latency?range=1h", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert len(mock.call_args_list) == 3
        for call in mock.call_args_list:
            assert "apisix_http_latency_bucket{" in call.args[0]
            assert 'type="request"' in call.args[0]

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/gateway/metrics/routes-comparison",
            "/admin/gateway/metrics/consumers-comparison",
        ],
    )
    async def test_comparison_latency_columns_use_request_series_only(
        self, client, admin_token, path
    ):
        mock = AsyncMock(side_effect=[[], [], [], [], []])
        with patch(_GW_INSTANT, mock):
            resp = await client.get(f"{path}?range=1h", headers=auth_header(admin_token))
        assert resp.status_code == 200
        requests_q, errors_q, p50_q, p95_q, total_q = (c.args[0] for c in mock.call_args_list)
        for q in (p50_q, p95_q):
            assert "apisix_http_latency_bucket{" in q
            assert 'type="request"' in q
        for q in (requests_q, errors_q, total_q):
            assert "apisix_http_status{" in q
            assert "type=" not in q


class TestRateWindow:
    @pytest.mark.parametrize(
        "step, window",
        [
            ("15s", "300s"),
            ("60s", "300s"),
            ("300s", "300s"),
            ("600s", "600s"),
            ("3600s", "3600s"),
            ("21600s", "21600s"),
            ("86400s", "86400s"),
        ],
    )
    def test_window_is_at_least_one_step_and_five_minutes(self, step, window):
        assert _rate_window(step) == window

    RANGES = [
        ("1h", "60s", "300s"),
        ("24h", "600s", "600s"),
        ("7d", "3600s", "3600s"),
        ("30d", "21600s", "21600s"),
    ]

    @pytest.mark.parametrize("range_, step, window", RANGES)
    async def test_gateway_request_rate(self, client, admin_token, range_, step, window):
        mock = AsyncMock(return_value=[])
        with patch(_GW_RANGE, mock):
            resp = await client.get(
                f"/admin/gateway/metrics/requests?range={range_}",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        query = mock.call_args.args[0]
        assert "rate(apisix_http_status" in query
        assert f"[{window}]" in query
        assert "[5m]" not in query
        assert mock.call_args.kwargs["step"] == step

    @pytest.mark.parametrize("range_, step, window", RANGES)
    async def test_gateway_latency(self, client, admin_token, range_, step, window):
        mock = AsyncMock(return_value=[])
        with patch(_GW_RANGE, mock):
            resp = await client.get(
                f"/admin/gateway/metrics/latency?range={range_}",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert len(mock.call_args_list) == 3
        for call in mock.call_args_list:
            assert f"[{window}]" in call.args[0]
            assert "[5m]" not in call.args[0]
            assert call.kwargs["step"] == step

    @pytest.mark.parametrize("range_, step, window", RANGES)
    async def test_external_request_rate(self, client, admin_token, range_, step, window):
        mock = AsyncMock(return_value=[])
        with patch(_EXT_RANGE, mock):
            resp = await client.get(
                f"/admin/external/metrics/requests?range={range_}",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        query = mock.call_args.args[0]
        # Both operands of the counter → histogram-count `or` fallback.
        assert query.count(f"[{window}]") == 2
        assert "[5m]" not in query
        assert mock.call_args.kwargs["step"] == step

    @pytest.mark.parametrize("range_, step, window", RANGES)
    async def test_external_latency(self, client, admin_token, range_, step, window):
        mock = AsyncMock(return_value=[])
        with patch(_EXT_RANGE, mock):
            resp = await client.get(
                f"/admin/external/metrics/latency?range={range_}",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert len(mock.call_args_list) == 3
        for call in mock.call_args_list:
            assert f"[{window}]" in call.args[0]
            assert "[5m]" not in call.args[0]
            assert call.kwargs["step"] == step


class TestRoutesComparisonAllRoutes:
    async def test_returns_every_route_busiest_first(self, client, admin_token):
        counts = {f"route-{i:02d}": 100 + i * 10 for i in range(15)}
        requests_result = [
            {"metric": {"route": route}, "value": [0, str(n)]} for route, n in counts.items()
        ]
        total_result = [{"value": [0, str(sum(counts.values()))]}]
        mock = AsyncMock(side_effect=[requests_result, [], [], [], total_result])
        with patch(_GW_INSTANT, mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        rows = resp.json()["routes"]
        assert len(rows) == 15
        assert {r["route"] for r in rows} == set(counts)
        assert [r["requests"] for r in rows] == sorted(counts.values(), reverse=True)
        assert sum(r["share"] for r in rows) == pytest.approx(100, abs=0.1)
        for call in mock.call_args_list:
            assert "topk" not in call.args[0]


class TestRouteIdentity:
    LISTING = {
        "items": [
            # Named route: under prefer_name its label is the name.
            {"id": "r-orders", "name": "orders"},
            # Unnamed route: its label is the id.
            {"id": "r-plain"},
            # Two routes sharing a name: the label can't be attributed.
            {"id": "dup-1", "name": "shared"},
            {"id": "dup-2", "name": "shared"},
            # Fixed route: name == id.
            {"id": "query-api", "name": "query-api"},
            # A name colliding with another route's id: the id wins.
            {"id": "billing", "name": "Billing Service"},
            {"id": "b-uuid", "name": "billing"},
        ],
        "total": 7,
    }

    async def _rows(self, client, admin_token, labels, list_mock):
        requests_result = [
            {"metric": {"route": label}, "value": [0, str(100 - i)]}
            for i, label in enumerate(labels)
        ]
        prom = AsyncMock(side_effect=[requests_result, [], [], [], []])
        with patch(_GW_INSTANT, prom), patch(_LIST, list_mock):
            resp = await client.get(
                "/admin/gateway/metrics/routes-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        return {r["route"]: (r["route_id"], r["name"]) for r in resp.json()["routes"]}

    async def test_labels_resolve_to_route_ids(self, client, admin_token):
        rows = await self._rows(
            client,
            admin_token,
            [
                "orders", "r-orders", "r-plain", "shared", "query-api",
                "billing", "Billing Service", "ghost",
            ],
            AsyncMock(return_value=self.LISTING),
        )
        assert rows["orders"] == ("r-orders", "orders")  # prefer_name label
        assert rows["r-orders"] == ("r-orders", "orders")  # pre-flip id label
        assert rows["r-plain"] == ("r-plain", None)
        assert rows["shared"] == (None, "shared")
        assert rows["query-api"] == ("query-api", "query-api")
        assert rows["billing"] == ("billing", "Billing Service")
        assert rows["Billing Service"] == ("billing", "Billing Service")
        assert rows["ghost"] == (None, None)

    async def test_listing_failure_keeps_rows_with_nulls(self, client, admin_token):
        rows = await self._rows(
            client, admin_token, ["orders"], AsyncMock(side_effect=RuntimeError("apisix down"))
        )
        assert rows == {"orders": (None, None)}

    async def test_listing_is_fetched_once_within_ttl(self, client, admin_token):
        list_mock = AsyncMock(return_value=self.LISTING)
        requests_result = [{"metric": {"route": "orders"}, "value": [0, "10"]}]
        prom = AsyncMock(side_effect=[requests_result, [], [], [], []] * 2)
        with patch(_GW_INSTANT, prom), patch(_LIST, list_mock):
            for _ in range(2):
                resp = await client.get(
                    "/admin/gateway/metrics/routes-comparison?range=1h",
                    headers=auth_header(admin_token),
                )
                assert resp.status_code == 200
                assert resp.json()["routes"][0]["route_id"] == "r-orders"
        assert list_mock.await_count == 1

    def test_name_map_is_unchanged_for_an_unnamed_id_that_is_also_a_name(self):
        # Label "x" is route A's id (A unnamed) and route B's name. The name stays
        # filled (as before); the id resolves id-first.
        items = [{"id": "x"}, {"id": "b", "name": "x"}]
        assert _route_name_map(items).get("x") == "x"
        assert _route_id_map(items).get("x") == "x"


class TestRouteListingCacheClock:
    """monotonic() counts from host boot, so the cache must work seconds after
    a reboot: the never-fetched/invalidated stamp has to read as stale."""

    async def test_fetches_and_refetches_after_invalidation_at_low_uptime(
        self, monkeypatch
    ):
        # A fresh copy starts from the import-time cache state, untouched by
        # earlier tests (the shared module is reset by a conftest fixture).
        gw = fresh_module_copy("app.routers.gateway")
        monkeypatch.setattr(gw, "_monotonic", lambda: 5.0)  # 5s of host uptime
        items = [{"id": "r1", "name": "orders"}]
        list_mock = AsyncMock(return_value={"items": items, "total": 1})
        with patch(_LIST, list_mock):
            assert await gw._list_routes_cached() == items
            assert await gw._list_routes_cached() == items
            assert list_mock.await_count == 1  # second call served from cache
            gw._invalidate_route_listing_cache()
            assert await gw._list_routes_cached() == items
            assert list_mock.await_count == 2


class TestLatencyNanIsNull:
    SERIES = [{"values": [[1000, "12.5"], [1060, "NaN"], [1120, "20"]]}]

    @pytest.mark.parametrize(
        "path, target",
        [
            ("/admin/gateway/metrics/latency", _GW_RANGE),
            ("/admin/external/metrics/latency", _EXT_RANGE),
        ],
    )
    async def test_idle_window_percentile_is_null(self, client, admin_token, path, target):
        with patch(target, AsyncMock(return_value=self.SERIES)):
            resp = await client.get(f"{path}?range=1h", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        for key in ("p50", "p95", "p99"):
            # The point is kept (index-aligned across p50/p95/p99), valued null.
            assert [p["timestamp"] for p in data[key]] == [1000, 1060, 1120]
            assert [p["value"] for p in data[key]] == [12.5, None, 20.0]

    async def test_request_rate_nan_stays_zero(self, client, admin_token):
        with patch(_GW_RANGE, AsyncMock(return_value=self.SERIES)):
            resp = await client.get(
                "/admin/gateway/metrics/requests?range=1h", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert [p["value"] for p in resp.json()] == [12.5, 0.0, 20.0]

    def test_unparseable_quantile_sample_is_null(self):
        points = _extract_timeseries([{"values": [[1000, "bad"]]}], nan_as_none=True)
        assert points == [{"timestamp": 1000, "value": None}]
