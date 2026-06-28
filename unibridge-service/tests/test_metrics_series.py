"""Tests for the bucketed per-dimension *-series breakdown endpoints.

Covers the grouped-volume breakdown helper and the four new endpoints added to
app/routers/gateway.py. Prometheus is stubbed via AsyncMock on
prometheus_client.range_query / instant_query, mirroring test_gateway.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKeyAccess
from tests.conftest import auth_header


def _series(label_name: str, label_value: str, points: list[tuple[int, str]]) -> dict:
    return {"metric": {label_name: label_value}, "values": [list(p) for p in points]}


def _instant(label_name: str, label_value: str, value: str) -> dict:
    return {"metric": {label_name: label_value}, "value": [0, value]}


class TestConsumersComparisonSeries:
    async def test_auto_bucket_shape_and_alignment(self, client, admin_token):
        """auto bucket → single range_query grouped by consumer; series points
        align to the shared bucket axis and per-series totals are correct."""
        # Two consumers sampled at two shared timestamps.
        results = [
            _series("consumer", "alice", [(1000, "3"), (2000, "5")]),
            _series("consumer", "bob", [(1000, "1"), (2000, "9")]),
        ]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/consumers-comparison-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["unit"] == "requests"
        assert body["buckets"] == [1000, 2000]
        by_key = {s["key"]: s for s in body["series"]}
        assert set(by_key) == {"alice", "bob"}
        for s in body["series"]:
            assert len(s["points"]) == len(body["buckets"])
        assert by_key["alice"]["points"] == [3, 5]
        assert by_key["alice"]["total"] == 8
        assert by_key["bob"]["points"] == [1, 9]
        assert by_key["bob"]["total"] == 10

    async def test_empty_consumer_label_becomes_no_api_key(self, client, admin_token):
        # An empty consumer label → _metric_label returns "unknown" → relabeled.
        results = [{"metric": {}, "values": [(1000, "4")]}]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/consumers-comparison-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        keys = [s["key"] for s in resp.json()["series"]]
        assert keys == ["(no api key)"]

    async def test_topn_and_others_aggregation(self, client, admin_token):
        # 15 consumers → top 12 kept, remaining 3 collapse into "(others)".
        results = [
            _series("consumer", f"c{i:02d}", [(1000, str(100 - i))])
            for i in range(15)
        ]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/consumers-comparison-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        series = resp.json()["series"]
        assert len(series) == 13  # 12 + (others)
        assert series[-1]["key"] == "(others)"
        # The three smallest totals: c12=88, c13=87, c14=86 → 261.
        assert series[-1]["total"] == 261
        assert series[-1]["points"] == [261]

    async def test_zero_total_series_dropped(self, client, admin_token):
        results = [
            _series("consumer", "alice", [(1000, "5")]),
            _series("consumer", "ghost", [(1000, "0")]),
        ]
        mock = AsyncMock(return_value=results)
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/consumers-comparison-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        keys = [s["key"] for s in resp.json()["series"]]
        assert keys == ["alice"]

    async def test_self_scoped_user_forced_to_own_consumer(
        self, client, user_token, seeded_db
    ):
        session_factory = async_sessionmaker(
            seeded_db, class_=AsyncSession, expire_on_commit=False
        )
        async with session_factory() as db:
            db.add(ApiKeyAccess(consumer_name="self_testuser", owner="testuser"))
            await db.commit()
        mock = AsyncMock(return_value=[])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/consumers-comparison-series?range=1h",
                headers=auth_header(user_token),
            )
        assert resp.status_code == 200
        assert 'consumer="self_testuser"' in mock.call_args.args[0]

    async def test_prometheus_error_returns_502(self, client, admin_token):
        mock = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/consumers-comparison-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502
        assert "Prometheus error" in resp.json()["detail"]


class TestByModelSeries:
    async def test_calendar_bucket_completed_plus_partial(self, client, admin_token):
        """bucket=day → range_query for completed buckets (timestamps shifted
        back one bucket) + instant_query for the partial current bucket. Series
        points align to the shared axis and totals sum across all buckets."""
        import time

        # Completed range samples are emitted at the bucket END; the helper
        # shifts them back one day (86400s) to mark the bucket start. Place the
        # samples a few days before "now" so they land inside the 30d window.
        bsec = 86400
        now = int(time.time())
        b1 = now - 5 * bsec  # bucket-end timestamps (within the window)
        b2 = now - 4 * bsec
        completed = [
            _series("model", "gpt-4", [(b1, "100"), (b2, "200")]),
            _series("model", "claude", [(b1, "50")]),
        ]
        partial = [_instant("model", "gpt-4", "7")]
        # by-model-series fans out to 3 specs (1 LiteLLM + 2 Bifrost). This
        # fixture drives the LiteLLM spec; the 2 Bifrost specs return empty so
        # the same data isn't triple-counted across providers.
        range_mock = AsyncMock(side_effect=[completed, [], []])
        instant_mock = AsyncMock(side_effect=[partial, [], []])
        with patch(
            "app.routers.gateway.prometheus_client.range_query", range_mock
        ), patch(
            "app.routers.gateway.prometheus_client.instant_query", instant_mock
        ):
            resp = await client.get(
                "/admin/gateway/metrics/llm/by-model-series?range=30d&bucket=day",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["unit"] == "tokens"
        # All series points are aligned to the shared bucket axis.
        n = len(body["buckets"])
        assert n >= 2
        for s in body["series"]:
            assert len(s["points"]) == n
            assert s["total"] == pytest.approx(sum(s["points"]))
        by_key = {s["key"]: s for s in body["series"]}
        assert by_key["gpt-4"]["total"] == pytest.approx(307)  # 100 + 200 + 7
        assert by_key["claude"]["total"] == pytest.approx(50)

    async def test_groups_by_requested_model_then_model(self, client, admin_token):
        # _metric_label prefers requested_model over model.
        results = [
            {
                "metric": {"requested_model": "gpt-4o", "model": "azure/gpt-4o"},
                "values": [(1000, "12")],
            }
        ]
        # Only the LiteLLM spec (1st of 3) returns the row; the 2 Bifrost specs
        # return empty. Otherwise the same row, grouped by the Bifrost labels
        # (alias, model), would also surface as a separate "azure/gpt-4o" key.
        mock = AsyncMock(side_effect=[results, [], []])
        with patch("app.routers.gateway.prometheus_client.range_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/llm/by-model-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        keys = [s["key"] for s in resp.json()["series"]]
        assert keys == ["gpt-4o"]

    async def test_requires_admin_permission(self, client, user_token):
        resp = await client.get(
            "/admin/gateway/metrics/llm/by-model-series?range=1h",
            headers=auth_header(user_token),
        )
        assert resp.status_code == 403
