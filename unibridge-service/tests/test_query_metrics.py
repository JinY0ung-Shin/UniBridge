"""Tests for DB query metrics (/admin/query/metrics).

Prometheus is stubbed via AsyncMock on prometheus_client.instant_query /
range_query (the same module object the gateway helpers call), mirroring
test_external_metrics.py.
"""
from __future__ import annotations

import inspect
import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKeyAccess
from app.routers import query_metrics
from app.routers.query_metrics import _gated, _sel
from tests.conftest import auth_header

_INSTANT = "app.routers.query_metrics.prometheus_client.instant_query"
_RANGE = "app.routers.query_metrics.prometheus_client.range_query"
_BASE = "/admin/query/metrics"

_COLORS = 'job="unibridge-service-colors"'
_SINGLE = 'job="unibridge-service"'
_GATE = 'unless on() (max(up{job="unibridge-service-colors"}) == 1)'


def _scalar(value: str) -> list[dict]:
    return [{"metric": {}, "value": [0, value]}]


def _by(label: str, rows: dict[str, str]) -> list[dict]:
    """Instant result grouped by one label; an empty key yields an unlabeled item."""
    return [
        {"metric": {label: key} if key else {}, "value": [0, value]}
        for key, value in rows.items()
    ]


def _sent(*mocks: AsyncMock) -> list[str]:
    return [call.args[0] for mock in mocks for call in mock.call_args_list]


def _assert_every_selector_has(expr: str, matcher: str) -> None:
    """Every metric selector (per job, per operand) carries the matcher."""
    selectors = expr.count('{job="unibridge-service') - expr.count("up{job=")
    assert selectors >= 2
    assert expr.count(matcher) == selectors, expr


def _assert_gated(expr: str) -> None:
    """``(colors) or ((single) unless on() (colors up))`` — order and nesting."""
    assert _GATE in expr
    assert (
        expr.index(_COLORS) < expr.index(" or ") < expr.index(_SINGLE) < expr.index(" unless ")
    ), expr
    depth = 0
    for ch in expr:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        assert depth >= 0, expr
    assert depth == 0, expr


async def _add_api_key(seeded_db, consumer_name: str, owner: str | None = None) -> None:
    session_factory = async_sessionmaker(
        seeded_db, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as db:
        db.add(ApiKeyAccess(consumer_name=consumer_name, owner=owner))
        await db.commit()


class TestSelectorBuilders:
    def test_gated_prefers_colors_job_and_suppresses_single_stack(self):
        expr = _gated(lambda job: f'sum(m{{job="{job}"}})')
        assert expr == (
            '(sum(m{job="unibridge-service-colors"})) or '
            '((sum(m{job="unibridge-service"})) '
            'unless on() (max(up{job="unibridge-service-colors"}) == 1))'
        )

    def test_sel_escapes_filter_values(self):
        assert _sel("j", 'a"b', 'd\\e"f') == 'job="j",consumer="a\\"b",db_alias="d\\\\e\\"f"'

    def test_sel_omits_unset_filters(self):
        assert _sel("j", None, None, 'status="success"') == 'job="j",status="success"'


class TestSummary:
    async def test_returns_rounded_summary_with_gated_promql(self, client, admin_token):
        mock = AsyncMock(side_effect=[
            _scalar("1500"),     # total
            _scalar("30"),       # errors (error + timeout)
            _scalar("3.4"),      # timeouts
            _scalar("12.3456"),  # avg latency ms
            _scalar("87.654"),   # p95 ms
            _scalar("12.5"),     # avg rows
        ])
        with patch(_INSTANT, mock):
            resp = await client.get(f"{_BASE}/summary?range=1h", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json() == {
            "total_queries": 1500,
            "error_rate": 2.0,
            "timeouts": 3,
            "avg_latency_ms": 12.35,
            "p95_latency_ms": 87.65,
            "avg_rows": 12.5,
        }

        total_q, errors_q, timeouts_q, latency_q, p95_q, rows_q = _sent(mock)
        for q in _sent(mock):
            _assert_gated(q)
            assert "[1h]" in q
        assert "sum (increase(unibridge_query_duration_seconds_count{" in total_q
        assert "status" not in total_q
        assert 'status!="success"' in errors_q
        assert 'status="timeout"' in timeouts_q
        # Latency is success-only and converted to ms in PromQL.
        assert "unibridge_query_duration_seconds_sum" in latency_q
        assert 'status="success"' in latency_q
        assert "* 1000" in latency_q
        assert "histogram_quantile(0.95, sum by (le)" in p95_q
        assert "unibridge_query_duration_seconds_bucket" in p95_q
        assert 'status="success"' in p95_q
        # The rows histogram has no status label.
        assert "unibridge_query_rows_returned_sum" in rows_q
        assert "status" not in rows_q
        assert all(call.kwargs["eval_time"] is None for call in mock.call_args_list)

    async def test_empty_window_reads_zero_counts_and_null_averages(self, client, admin_token):
        with patch(_INSTANT, AsyncMock(return_value=[])):
            resp = await client.get(f"{_BASE}/summary?range=1h", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json() == {
            "total_queries": 0,
            "error_rate": 0.0,
            "timeouts": 0,
            "avg_latency_ms": None,
            "p95_latency_ms": None,
            "avg_rows": None,
        }

    async def test_nan_averages_become_null(self, client, admin_token):
        # Only failures in the window: every success-only ratio is 0/0.
        mock = AsyncMock(side_effect=[
            _scalar("10"), _scalar("10"), _scalar("0"),
            _scalar("NaN"), _scalar("NaN"), _scalar("NaN"),
        ])
        with patch(_INSTANT, mock):
            resp = await client.get(f"{_BASE}/summary?range=1h", headers=auth_header(admin_token))
        data = resp.json()
        assert data["error_rate"] == 100.0
        assert data["p95_latency_ms"] is None
        assert data["avg_latency_ms"] is None
        assert data["avg_rows"] is None

    async def test_filters_scope_both_jobs(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/summary?range=1h&consumer=etl-app&database=main",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for q in _sent(mock):
            _assert_every_selector_has(q, 'consumer="etl-app"')
            _assert_every_selector_has(q, 'db_alias="main"')

    async def test_ui_consumer_filter_is_accepted(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/summary?range=1h&consumer=__ui__", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert 'consumer="__ui__"' in _sent(mock)[0]

    async def test_database_filter_is_escaped(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/summary",
                params={"range": "1h", "database": 'we"ird'},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert 'db_alias="we\\"ird"' in _sent(mock)[0]

    async def test_invalid_consumer_rejected(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/summary",
                params={"range": "1h", "consumer": "bad name"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid consumer name"
        mock.assert_not_called()

    @pytest.mark.parametrize("database", ["line\nbreak", "x" * 101])
    async def test_invalid_database_rejected(self, client, admin_token, database):
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/summary",
                params={"range": "1h", "database": database},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid database filter"
        mock.assert_not_called()

    async def test_custom_range_evaluates_at_window_end(self, client, admin_token):
        end = int(time.time())
        start = end - 3600
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/summary?start={start}&end={end}", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert "[3600s]" in _sent(mock)[0]
        assert all(call.kwargs["eval_time"] == float(end) for call in mock.call_args_list)

    async def test_self_scoped_user_without_key_sees_nothing(self, client, user_token):
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(f"{_BASE}/summary?range=1h", headers=auth_header(user_token))
        assert resp.status_code == 200
        assert 'consumer="__no_self_api_key__"' in _sent(mock)[0]

    async def test_role_without_monitoring_permission_is_forbidden(self, client, querier_token):
        resp = await client.get(f"{_BASE}/summary?range=1h", headers=auth_header(querier_token))
        assert resp.status_code == 403

    async def test_prometheus_error_returns_502(self, client, admin_token):
        with patch(_INSTANT, AsyncMock(side_effect=ConnectionError("down"))):
            resp = await client.get(f"{_BASE}/summary?range=1h", headers=auth_header(admin_token))
        assert resp.status_code == 502
        assert "Prometheus error" in resp.json()["detail"]


class TestQueriesTotal:
    async def test_counts_are_rounded_per_bucket(self, client, admin_token):
        mock = AsyncMock(return_value=[{"metric": {}, "values": [[1000, "2.6"], [1300, "0.4"]]}])
        with patch(_RANGE, mock):
            resp = await client.get(
                f"{_BASE}/queries-total?range=1h&consumer=etl-app&database=main",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert resp.json() == [
            {"timestamp": 1000, "value": 3},
            {"timestamp": 1300, "value": 0},
        ]
        q = mock.call_args.args[0]
        _assert_gated(q)
        assert "[5m]" in q  # 1h preset → 5m volume window
        _assert_every_selector_has(q, 'consumer="etl-app"')
        _assert_every_selector_has(q, 'db_alias="main"')
        assert mock.call_args.kwargs["step"] == "300s"

    async def test_prometheus_error_returns_502(self, client, admin_token):
        with patch(_RANGE, AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await client.get(
                f"{_BASE}/queries-total?range=1h", headers=auth_header(admin_token)
            )
        assert resp.status_code == 502


class TestOutcomes:
    async def test_one_status_grouped_query_split_into_outcomes(self, client, admin_token):
        mock = AsyncMock(return_value=[
            {"metric": {"status": "success"}, "values": [[1000, "5"], [2000, "7.4"]]},
            {"metric": {"status": "error"}, "values": [[2000, "1"], [3000, "2"]]},
        ])
        with patch(_RANGE, mock):
            resp = await client.get(f"{_BASE}/outcomes?range=1h", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json() == [
            {"timestamp": 1000, "success": 5, "error": 0, "timeout": 0},
            {"timestamp": 2000, "success": 7, "error": 1, "timeout": 0},
            {"timestamp": 3000, "success": 0, "error": 2, "timeout": 0},
        ]
        # One query keeps every outcome on the same evaluation timestamps.
        mock.assert_called_once()
        q = mock.call_args.args[0]
        _assert_gated(q)
        assert "sum by (status)" in q
        assert 'status="' not in q
        assert "status!=" not in q

    async def test_no_samples_returns_empty_list(self, client, admin_token):
        with patch(_RANGE, AsyncMock(return_value=[])):
            resp = await client.get(f"{_BASE}/outcomes?range=1h", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_calendar_buckets_without_queries_return_empty_list(self, client, admin_token):
        # Calendar buckets always have an axis; with nothing counted it is still "no data".
        with patch(_RANGE, AsyncMock(return_value=[])), patch(_INSTANT, AsyncMock(return_value=[])):
            resp = await client.get(
                f"{_BASE}/outcomes?range=24h&bucket=hour", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert resp.json() == []


class TestLatency:
    async def test_success_only_quantiles_share_one_pinned_window(self, client, admin_token):
        mock = AsyncMock(return_value=[
            {"metric": {}, "values": [[1000, "12.5"], [1060, "NaN"], [1120, "+Inf"]]}
        ])
        with patch(_RANGE, mock):
            resp = await client.get(
                f"{_BASE}/latency?range=1h&database=main", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        # A step without successful queries is a gap, not a fake 0 ms.
        points = [
            {"timestamp": 1000, "value": 12.5},
            {"timestamp": 1060, "value": None},
            {"timestamp": 1120, "value": None},
        ]
        assert resp.json() == {"p50": points, "p95": points, "p99": points}

        sent = _sent(mock)
        for quantile, q in zip(("0.5", "0.95", "0.99"), sent):
            _assert_gated(q)
            assert f"histogram_quantile({quantile}, sum by (le)" in q
            assert "[300s]" in q  # max(5m, 60s step)
            assert 'status="success"' in q
            assert "* 1000" in q
            _assert_every_selector_has(q, 'db_alias="main"')
        windows = {(call.kwargs["start"], call.kwargs["end"]) for call in mock.call_args_list}
        assert len(windows) == 1
        ((start, end),) = windows
        assert start is not None and end - start == 3600
        assert all(call.kwargs["step"] == "60s" for call in mock.call_args_list)

    @pytest.mark.parametrize(
        "time_range,step",
        [("15m", "15s"), ("24h", "600s"), ("7d", "3600s"), ("60d", "43200s")],
    )
    async def test_rate_window_is_at_least_five_minutes_and_widens_to_the_step(
        self, client, admin_token, time_range, step
    ):
        mock = AsyncMock(return_value=[])
        with patch(_RANGE, mock):
            resp = await client.get(
                f"{_BASE}/latency?range={time_range}", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        window = f"[{max(300, int(step[:-1]))}s]"
        for call in mock.call_args_list:
            assert window in call.args[0]
            assert call.kwargs["step"] == step

    async def test_custom_range_is_used_as_is(self, client, admin_token):
        end = int(time.time())
        start = end - 7200
        mock = AsyncMock(return_value=[])
        with patch(_RANGE, mock):
            resp = await client.get(
                f"{_BASE}/latency?start={start}&end={end}", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        for call in mock.call_args_list:
            assert call.kwargs["start"] == float(start)
            assert call.kwargs["end"] == float(end)


class TestDatabasesComparison:
    async def test_rows_shares_types_and_nulls(self, client, admin_token):
        queries = [
            {"metric": {"db_alias": "main", "db_type": "postgres"}, "value": [0, "100"]},
            {"metric": {"db_alias": "warehouse", "db_type": "clickhouse"}, "value": [0, "40"]},
            # Re-registered alias: counts merge, the busier type wins.
            {"metric": {"db_alias": "warehouse", "db_type": "postgres"}, "value": [0, "10"]},
            {"metric": {"db_alias": "idle", "db_type": "mssql"}, "value": [0, "0.2"]},
        ]
        mock = AsyncMock(side_effect=[
            queries,
            _by("db_alias", {"main": "5"}),
            _by("db_alias", {"main": "12.3456", "warehouse": "NaN"}),
            _by("db_alias", {"main": "40.001"}),
            _by("db_alias", {"main": "3.333"}),
            _scalar("150"),
        ])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/databases-comparison?range=1h", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert resp.json() == {
            "total_queries": 150,
            "databases": [
                {
                    "database": "main",
                    "db_type": "postgres",
                    "queries": 100,
                    "share": 66.67,
                    "error_rate": 5.0,
                    "avg_latency_ms": 12.35,
                    "latency_p95_ms": 40.0,
                    "avg_rows": 3.33,
                },
                {
                    "database": "warehouse",
                    "db_type": "clickhouse",
                    "queries": 50,
                    "share": 33.33,
                    "error_rate": 0.0,
                    "avg_latency_ms": None,
                    "latency_p95_ms": None,
                    "avg_rows": None,
                },
            ],
        }

        queries_q, errors_q, latency_q, p95_q, rows_q, total_q = _sent(mock)
        for q in _sent(mock):
            _assert_gated(q)
        assert "sum by (db_alias, db_type)" in queries_q
        assert "topk" not in queries_q  # every database with traffic is returned
        assert "sum by (db_alias)" in errors_q
        assert 'status!="success"' in errors_q
        assert "sum by (db_alias, le)" in p95_q
        assert "sum by (db_alias)" in latency_q
        assert "sum by (db_alias)" in rows_q
        assert "by (" not in total_q

    async def test_consumer_filter_applies_to_every_query(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/databases-comparison?range=1h&consumer=etl-app",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert resp.json() == {"total_queries": 0, "databases": []}
        for q in _sent(mock):
            _assert_every_selector_has(q, 'consumer="etl-app"')
            assert 'db_alias="' not in q



class TestConsumersComparison:
    async def test_topk_wraps_gated_expression_and_sentinels(self, client, admin_token):
        queries = [
            {"metric": {"consumer": "etl-app"}, "value": [0, "90"]},
            {"metric": {"consumer": "__ui__"}, "value": [0, "30"]},
            {"metric": {}, "value": [0, "10"]},  # recorded before the consumer label
        ]
        mock = AsyncMock(side_effect=[
            queries,
            _by("consumer", {"etl-app": "9", "": "1"}),
            _by("consumer", {"etl-app": "20", "__ui__": "5.5"}),
            [],
            _by("consumer", {"__ui__": "2"}),
            _scalar("130"),
        ])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/consumers-comparison?range=1h", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert resp.json() == {
            "total_queries": 130,
            "consumers": [
                {
                    "consumer": "etl-app",
                    "queries": 90,
                    "share": 69.23,
                    "error_rate": 10.0,
                    "avg_latency_ms": 20.0,
                    "latency_p95_ms": None,
                    "avg_rows": None,
                },
                {
                    "consumer": "__ui__",
                    "queries": 30,
                    "share": 23.08,
                    "error_rate": 0.0,
                    "avg_latency_ms": 5.5,
                    "latency_p95_ms": None,
                    "avg_rows": 2.0,
                },
                {
                    "consumer": "(untracked)",
                    "queries": 10,
                    "share": 7.69,
                    "error_rate": 10.0,
                    "avg_latency_ms": None,
                    "latency_p95_ms": None,
                    "avg_rows": None,
                },
            ],
        }

        queries_q = _sent(mock)[0]
        # topk applies to the gated result, not inside either job's operand.
        assert queries_q.startswith("topk(20, (")
        assert queries_q.endswith("== 1)))")
        assert "sum by (consumer)" in queries_q
        for q in _sent(mock):
            _assert_gated(q)
            assert 'consumer="' not in q  # admins are not force-filtered

    async def test_database_filter_applies_to_every_query(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with patch(_INSTANT, mock):
            resp = await client.get(
                f"{_BASE}/consumers-comparison?range=1h&database=main",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        for q in _sent(mock):
            _assert_every_selector_has(q, 'db_alias="main"')


class TestComparisonSeries:
    async def test_databases_series_rounds_query_counts(self, client, admin_token):
        mock = AsyncMock(return_value=[
            {"metric": {"db_alias": "main"}, "values": [[1000, "2.6"], [2000, "3.2"]]},
            {"metric": {"db_alias": "warehouse"}, "values": [[1000, "1.4"]]},
        ])
        with patch(_RANGE, mock):
            resp = await client.get(
                f"{_BASE}/databases-comparison-series?range=1h&consumer=etl-app",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["unit"] == "queries"
        assert body["buckets"] == [1000, 2000]
        assert body["series"] == [
            {"key": "main", "total": 6, "points": [3, 3]},
            {"key": "warehouse", "total": 1, "points": [1, 0]},
        ]
        q = mock.call_args.args[0]
        _assert_gated(q)
        assert "sum by (db_alias)" in q
        _assert_every_selector_has(q, 'consumer="etl-app"')

    async def test_consumers_series_names_untracked_in_promql(self, client, admin_token):
        # label_replace hands back the sentinel itself; the UI key passes through.
        mock = AsyncMock(return_value=[
            {"metric": {"consumer": "__ui__"}, "values": [[1000, "4"]]},
            {"metric": {"consumer": "(untracked)"}, "values": [[1000, "2"]]},
        ])
        with patch(_RANGE, mock):
            resp = await client.get(
                f"{_BASE}/consumers-comparison-series?range=1h&database=main",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert [s["key"] for s in resp.json()["series"]] == ["__ui__", "(untracked)"]
        q = mock.call_args.args[0]
        assert q.startswith("label_replace((")
        assert q.endswith(', "consumer", "(untracked)", "consumer", "")')
        _assert_gated(q)
        assert "sum by (consumer)" in q
        _assert_every_selector_has(q, 'db_alias="main"')

    async def test_key_named_unknown_keeps_its_name(self, client, admin_token):
        mock = AsyncMock(return_value=[
            {"metric": {"consumer": "unknown"}, "values": [[1000, "3"]]},
        ])
        with patch(_RANGE, mock):
            resp = await client.get(
                f"{_BASE}/consumers-comparison-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert [s["key"] for s in resp.json()["series"]] == ["unknown"]

    async def test_prometheus_error_returns_502(self, client, admin_token):
        with patch(_RANGE, AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await client.get(
                f"{_BASE}/databases-comparison-series?range=1h",
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502
        assert "Prometheus error" in resp.json()["detail"]


# ── Filter-param injection defences (signature-driven, like the gateway suite) ──

# Closes the label matcher, then widens the selector to every series.
BREAKOUT = 'x",job=~".+'


def _filtered_endpoints(param: str) -> list[str]:
    """Paths of every query-metrics endpoint taking the given filter param."""
    found: list[str] = []
    for route in query_metrics.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None and param in inspect.signature(endpoint).parameters:
            found.append(route.path)
    return sorted(found)


class TestFilterInjection:
    def test_the_scan_found_the_endpoints(self):
        """A refactor that renames the params must not silently empty this suite."""
        assert len(_filtered_endpoints("consumer")) == 6
        assert len(_filtered_endpoints("database")) == 6
        assert f"{_BASE}/summary" in _filtered_endpoints("consumer")
        assert f"{_BASE}/consumers-comparison" in _filtered_endpoints("database")

    @pytest.mark.parametrize("path", _filtered_endpoints("consumer"))
    async def test_consumer_breakout_is_rejected(self, client, admin_token, path):
        instant = AsyncMock(return_value=[])
        range_q = AsyncMock(return_value=[])
        with patch(_INSTANT, instant), patch(_RANGE, range_q):
            resp = await client.get(
                path,
                params={"range": "1h", "consumer": BREAKOUT},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400, path
        instant.assert_not_called()
        range_q.assert_not_called()

    @pytest.mark.parametrize("path", _filtered_endpoints("database"))
    async def test_database_breakout_stays_inside_one_matcher(self, client, admin_token, path):
        # DB aliases are free-form, so this value is valid — escaping must hold it.
        instant = AsyncMock(return_value=[])
        range_q = AsyncMock(return_value=[])
        with patch(_INSTANT, instant), patch(_RANGE, range_q):
            resp = await client.get(
                path,
                params={"range": "1h", "database": BREAKOUT},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200, path
        sent = _sent(instant, range_q)
        assert sent
        for q in sent:
            assert 'db_alias="x\\",job=~\\".+"' in q
            assert 'db_alias="x",job=~".+"' not in q


# ── Self-scope forcing on every endpoint ─────────────────────────────────────


def _all_endpoints() -> list[str]:
    return sorted(route.path for route in query_metrics.router.routes)


class TestSelfScope:
    def test_every_endpoint_is_covered(self):
        assert len(_all_endpoints()) == 8

    @pytest.mark.parametrize("path", _all_endpoints())
    async def test_every_endpoint_forces_the_callers_key(
        self, client, user_token, seeded_db, path
    ):
        await _add_api_key(seeded_db, "self_testuser", owner="testuser")
        instant = AsyncMock(return_value=[])
        range_q = AsyncMock(return_value=[])
        with patch(_INSTANT, instant), patch(_RANGE, range_q):
            resp = await client.get(
                path,
                params={"range": "1h", "consumer": "__ui__"},
                headers=auth_header(user_token),
            )
        assert resp.status_code == 200, path
        sent = _sent(instant, range_q)
        assert sent
        for q in sent:
            _assert_every_selector_has(q, 'consumer="self_testuser"')
            assert "__ui__" not in q
