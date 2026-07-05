"""Tests for external-service traffic metrics (/admin/external/metrics)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, patch

from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

_INSTANT = "app.routers.external_metrics.prometheus_client.instant_query"
_RANGE = "app.routers.external_metrics.prometheus_client.range_query"


class TestSummary:
    async def test_returns_summary_with_or_fallback(self, client, admin_token):
        total = [{"value": [1000, "1500"]}]
        error_rate = [{"value": [1000, "2.35"]}]
        latency = [{"value": [1000, "45.678"]}]
        mock = AsyncMock(side_effect=[total, error_rate, latency])
        with patch(_INSTANT, mock):
            resp = await client.get(
                "/admin/external/metrics/summary?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 1500
        assert data["error_rate"] == 2.35
        assert data["avg_latency_ms"] == 45.68

        # The request-count expression carries the counter→histogram `or` fallback
        # over the full resolved window, always scoped to the external-services job.
        total_q = mock.call_args_list[0].args[0]
        assert " or " in total_q
        assert "http_requests_total" in total_q
        assert "http_request_duration_seconds_count" in total_q
        assert "[1h]" in total_q
        assert 'job="external-services"' in total_q

    async def test_service_filter_scopes_selector(self, client, admin_token):
        empty = [{"value": [0, "0"]}]
        mock = AsyncMock(side_effect=[empty, empty, empty])
        with patch(_INSTANT, mock):
            resp = await client.get(
                "/admin/external/metrics/summary?range=1h&service=orders-api",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'service="orders-api"' in mock.call_args_list[0].args[0]

    async def test_invalid_service_rejected(self, client, admin_token):
        resp = await client.get(
            '/admin/external/metrics/summary?range=1h&service="; drop',
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 400

    async def test_requires_monitoring_permission(self, client, user_token):
        # Seeded 'user' role has only gateway.monitoring.self, not .read.
        resp = await client.get(
            "/admin/external/metrics/summary?range=1h",
            headers=auth_header(user_token),
        )
        assert resp.status_code == 403

    async def test_prometheus_error_returns_502(self, client, admin_token):
        with patch(_INSTANT, new_callable=AsyncMock, side_effect=ConnectionError("down")):
            resp = await client.get(
                "/admin/external/metrics/summary?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502
        assert "Prometheus" in resp.json()["detail"]


class TestStatusCodes:
    async def test_reads_status_label_sorted_desc(self, client, admin_token):
        results = [
            {"metric": {"status": "200"}, "value": [0, "900"]},
            {"metric": {"status": "500"}, "value": [0, "12"]},
            {"metric": {"status": "404"}, "value": [0, "40"]},
        ]
        with patch(_INSTANT, new_callable=AsyncMock, return_value=results):
            resp = await client.get(
                "/admin/external/metrics/status-codes?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert resp.json() == [
            {"code": "200", "count": 900},
            {"code": "404", "count": 40},
            {"code": "500", "count": 12},
        ]


class TestLatency:
    async def test_percentiles_converted_to_ms(self, client, admin_token):
        # PromQL applies * 1000 so the mocked (already-ms) values pass through.
        series = [{"values": [[1000, "12.5"], [1060, "18.0"]]}]
        mock = AsyncMock(return_value=series)
        with patch(_RANGE, mock):
            resp = await client.get(
                "/admin/external/metrics/latency?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["p50"][0]["value"] == 12.5
        assert set(data.keys()) == {"p50", "p95", "p99"}
        # Conversion to ms happens in PromQL, not post-processing.
        assert "* 1000" in mock.call_args_list[0].args[0]
        assert "http_request_duration_seconds_bucket" in mock.call_args_list[0].args[0]


class TestServicesComparison:
    async def test_share_uses_grand_total_not_top_rows(self, client, admin_token):
        # top-10 rows sum to 40, but the grand total across ALL services is 100;
        # share must divide by 100 (mirrors routes-comparison).
        requests_res = [
            {"metric": {"service": "a"}, "value": [0, "30"]},
            {"metric": {"service": "b"}, "value": [0, "10"]},
        ]
        errors_res = [{"metric": {"service": "a"}, "value": [0, "3"]}]
        p50_res = [{"metric": {"service": "a"}, "value": [0, "0.05"]}]  # seconds
        p95_res = []
        total_res = [{"value": [0, "100"]}]
        mock = AsyncMock(side_effect=[requests_res, errors_res, p50_res, p95_res, total_res])
        with patch(_INSTANT, mock):
            resp = await client.get(
                "/admin/external/metrics/services-comparison?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 100
        by_name = {s["service"]: s for s in data["services"]}
        assert by_name["a"]["share"] == 30.0  # 30/100, not 30/40
        assert by_name["b"]["share"] == 10.0
        assert by_name["a"]["error_rate"] == 10.0  # 3/30
        assert by_name["a"]["latency_p50_ms"] == 50.0  # 0.05s → 50ms
        assert by_name["b"]["latency_p50_ms"] is None
        # topk expression wraps the or-fallback count.
        assert "topk(10" in mock.call_args_list[0].args[0]
        assert " or " in mock.call_args_list[0].args[0]


class TestRequestsTotal:
    async def test_returns_timeseries(self, client, admin_token):
        ts_data = [{"values": [[1000, "150"], [1060, "200"]]}]
        mock = AsyncMock(return_value=ts_data)
        with patch(_RANGE, mock):
            resp = await client.get(
                "/admin/external/metrics/requests-total?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["value"] == 150.0
        assert "increase(" in mock.call_args.args[0]
        assert " or " in mock.call_args.args[0]

    async def test_day_bucket_zero_fills_missing_buckets(self, client, admin_token):
        # Reuses the gateway KST-grid pattern: sparse services return no samples
        # for quiet days; the axis must still list every calendar bucket with 0.
        kst = timezone(timedelta(hours=9))
        start = int(datetime(2026, 6, 10, 3, 0, tzinfo=kst).timestamp())
        end = int(datetime(2026, 6, 13, 12, 0, tzinfo=kst).timestamp())
        aligned_start = int(datetime(2026, 6, 10, 0, 0, tzinfo=kst).timestamp())
        current_start = int(datetime(2026, 6, 13, 0, 0, tzinfo=kst).timestamp())
        completed = [{"values": [[aligned_start + 2 * 86400, "5"]]}]
        partial = [{"value": [end, "7"]}]
        range_mock = AsyncMock(return_value=completed)
        instant_mock = AsyncMock(return_value=partial)

        with patch(_RANGE, range_mock), patch(_INSTANT, instant_mock):
            resp = await client.get(
                f"/admin/external/metrics/requests-total?start={start}&end={end}&bucket=day",
                headers=auth_header(admin_token),
            )

        assert resp.status_code == 200
        assert resp.json() == [
            {"timestamp": aligned_start, "value": 0.0},
            {"timestamp": aligned_start + 86400, "value": 5.0},
            {"timestamp": aligned_start + 2 * 86400, "value": 0.0},
            {"timestamp": current_start, "value": 7.0},
        ]


class TestServicesComparisonSeries:
    async def test_returns_grouped_breakdown(self, client, admin_token):
        # Auto (non-calendar) path: one range query grouped by service.
        results = [
            {"metric": {"service": "a"}, "values": [[1000, "10"], [1060, "20"]]},
            {"metric": {"service": "b"}, "values": [[1000, "5"], [1060, "5"]]},
        ]
        with patch(_RANGE, new_callable=AsyncMock, return_value=results):
            resp = await client.get(
                "/admin/external/metrics/services-comparison-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["unit"] == "requests"
        keys = {s["key"] for s in data["series"]}
        assert keys == {"a", "b"}
