"""DB query metrics: per-API-key × per-database query monitoring.

Read-only dashboards over UniBridge's own query instrumentation
(``unibridge_query_duration_seconds`` / ``unibridge_query_rows_returned``, see
:mod:`app.metrics`), labelled per database alias and per caller — the APISIX
consumer name for API-key calls, :data:`app.metrics.UI_QUERY_CONSUMER` for JWT
calls (the UI playground). Shapes mirror the gateway metrics endpoints so the same
frontend charting applies, and time semantics are inherited from the gateway
helpers (full-window ``increase()`` at ``eval_time`` + deterministic KST calendar
bucket axis) by importing them directly, like :mod:`app.routers.external_metrics`.

Two things differ from the gateway/external dashboards:

* **Blue-green job gating.** These series are scraped by two jobs (see
  ``prometheus/prometheus.yml``). ``unibridge-service-colors`` scrapes each color
  separately; ``unibridge-service`` targets a DNS alias BOTH colors answer on, so
  on a blue-green host it round-robins between two processes, its counters jump
  back and forth, and ``increase()`` reads every jump as a counter reset and
  inflates. Every expression is therefore built per job and combined by
  :func:`_gated`: the colors job is used whenever any of its targets is up, the
  single-stack job (whose colors targets are expectedly down) only otherwise.
  The two jobs are never summed.
* **Success-only latency.** Average and quantile latency count only
  ``status="success"``: queries rejected by pre-execution validation record 0s
  and timeouts record the full wait, either of which would skew the figures.
  The error rate counts both ``error`` and ``timeout``.

Access mirrors gateway monitoring: ``gateway.monitoring.read`` sees every
consumer, while callers with only ``gateway.monitoring.self`` are forced to their
own key (so the UI consumer and other keys never reach them).
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.metrics import QUERY_STATUSES
from app.routers.gateway import (
    TimeWindow,
    _extract_scalar,
    _gateway_monitoring_scope,
    _grouped_volume_series,
    _MonitoringScope,
    _promql_str,
    _scope_consumer,
    _volume_series,
    resolve_time_window,
)
from app.services import prometheus_client

router = APIRouter(prefix="/admin/query/metrics", tags=["Query metrics"])

COLORS_JOB = "unibridge-service-colors"
SINGLE_STACK_JOB = "unibridge-service"

_COUNT = "unibridge_query_duration_seconds_count"
_SUM = "unibridge_query_duration_seconds_sum"
_BUCKET = "unibridge_query_duration_seconds_bucket"
_ROWS_COUNT = "unibridge_query_rows_returned_count"
_ROWS_SUM = "unibridge_query_rows_returned_sum"

_SUCCESS = 'status="success"'
_NOT_SUCCESS = 'status!="success"'

# Series recorded before the consumer label existed carry none; they stay visible
# (so per-key rows still add up to the totals) under this sentinel. Parentheses are
# outside the API-key name charset, so it can never collide with a real key.
UNTRACKED_CONSUMER = "(untracked)"

# Keys are unbounded where registered databases are not, so only the key table is
# capped. Shares still use the grand total as denominator.
_CONSUMER_TOPK = 20

# DB aliases are free-form (the schema caps length only), so the filter is not
# charset-restricted; PromQL safety comes from escaping in ``_sel``.
_DATABASE_FILTER_MAX_LEN = 100

# Latency charts use a rate window of at least this many seconds, widened to the
# step on long ranges (see ``latency``).
_LATENCY_MIN_RATE_WINDOW_S = 300


def _validate_database(database: str | None) -> None:
    if database and (
        len(database) > _DATABASE_FILTER_MAX_LEN or any(ord(ch) < 32 for ch in database)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid database filter"
        )


def _sel(job: str, consumer: str | None, database: str | None, *extra: str) -> str:
    """Inner PromQL label selector for one scrape job (body without braces)."""
    parts = [f'job="{job}"']
    if consumer:
        parts.append(f'consumer="{_promql_str(consumer)}"')
    if database:
        parts.append(f'db_alias="{_promql_str(database)}"')
    parts.extend(extra)
    return ",".join(parts)


def _gated(expr_for_job: Callable[[str], str]) -> str:
    """Combine a per-job expression across the two scrape jobs.

    ``expr_for_job(job)`` must be fully aggregated so ``job``/``instance`` drop
    out and label sets match across the ``or``. The right operand is discarded
    whenever any colors target is up (see the module docstring). ``or`` has the
    lowest PromQL precedence, so callers wrapping the result (``topk``) are safe.
    """
    return (
        f"({expr_for_job(COLORS_JOB)}) or "
        f"(({expr_for_job(SINGLE_STACK_JOB)}) unless on() "
        f'(max(up{{job="{COLORS_JOB}"}}) == 1))'
    )


def _by(group_by: str | None) -> str:
    return f" by ({group_by})" if group_by else ""


def _count_expr(
    consumer: str | None,
    database: str | None,
    window: str,
    *extra: str,
    group_by: str | None = None,
) -> str:
    return _gated(
        lambda job: f"sum{_by(group_by)} "
        f"(increase({_COUNT}{{{_sel(job, consumer, database, *extra)}}}[{window}]))"
    )


def _avg_latency_ms_expr(
    consumer: str | None, database: str | None, window: str, group_by: str | None = None
) -> str:
    def per_job(job: str) -> str:
        sel = _sel(job, consumer, database, _SUCCESS)
        return (
            f"sum{_by(group_by)} (increase({_SUM}{{{sel}}}[{window}])) "
            f"/ sum{_by(group_by)} (increase({_COUNT}{{{sel}}}[{window}])) * 1000"
        )

    return _gated(per_job)


def _quantile_ms_expr(
    quantile: float,
    consumer: str | None,
    database: str | None,
    window: str,
    group_by: str | None = None,
) -> str:
    le = f"{group_by}, le" if group_by else "le"
    return _gated(
        lambda job: f"histogram_quantile({quantile}, sum by ({le}) "
        f"(rate({_BUCKET}{{{_sel(job, consumer, database, _SUCCESS)}}}[{window}]))) * 1000"
    )


def _avg_rows_expr(
    consumer: str | None, database: str | None, window: str, group_by: str | None = None
) -> str:
    # The rows histogram is only observed for successful queries and has no
    # status label, so no status matcher here.
    def per_job(job: str) -> str:
        sel = _sel(job, consumer, database)
        return (
            f"sum{_by(group_by)} (increase({_ROWS_SUM}{{{sel}}}[{window}])) "
            f"/ sum{_by(group_by)} (increase({_ROWS_COUNT}{{{sel}}}[{window}]))"
        )

    return _gated(per_job)


def _label_untracked(expr: str) -> str:
    """Name the empty consumer label in PromQL (see ``UNTRACKED_CONSUMER``).

    Done here rather than after ``_metric_label``, which also maps a missing
    label to "unknown", so a real key named ``unknown`` keeps its name.
    """
    return f'label_replace({expr}, "consumer", "{UNTRACKED_CONSUMER}", "consumer", "")'


def _seconds(duration: str) -> int:
    """``"300s"`` / ``"5m"`` / ``"1h"`` / ``"7d"`` → seconds."""
    if duration.endswith("s"):
        return int(duration[:-1])
    return prometheus_client._parse_duration(duration)


def _finite(value: float | None) -> float | None:
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return value


def _round_or_none(value: float | None, ndigits: int = 2) -> float | None:
    value = _finite(value)
    return round(value, ndigits) if value is not None else None


def _scalar_or_none(results: list[dict[str, Any]]) -> float | None:
    """Like ``_extract_scalar`` but empty/NaN → None instead of 0 (for quantiles)."""
    if not results:
        return None
    try:
        return _finite(float(results[0].get("value", [0, "NaN"])[1]))
    except (IndexError, ValueError, TypeError):
        return None


def _nullable_timeseries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Like ``_extract_timeseries`` but NaN/Inf → None instead of 0.

    A quantile over a window without successful queries is NaN; reporting it as
    0 ms would draw fake instant queries on the latency chart.
    """
    if not results:
        return []
    points = []
    for ts, val in results[0].get("values", []):
        try:
            value = _finite(float(val))
        except (ValueError, TypeError):
            value = None
        points.append({
            "timestamp": int(ts),
            "value": round(value, 4) if value is not None else None,
        })
    return points


def _map_by_label(
    results: list[dict[str, Any]], label_name: str, empty: str | None = None
) -> dict[str, float]:
    """Instant-query result → {label value: float} (NaN kept for the caller).

    Items missing the label are skipped, or mapped to ``empty`` when given.
    """
    out: dict[str, float] = {}
    for r in results or []:
        key = r.get("metric", {}).get(label_name) or empty
        if not key:
            continue
        value = r.get("value")
        if not value:
            continue
        try:
            out[key] = float(value[1])
        except (IndexError, ValueError, TypeError):
            continue
    return out


def _comparison_row(
    queries: float,
    total: float,
    errors: float,
    avg_latency_ms: float | None,
    p95_ms: float | None,
    avg_rows: float | None,
) -> dict[str, Any]:
    return {
        "queries": round(queries),
        "share": round(queries / total * 100, 2) if total > 0 else 0.0,
        "error_rate": round(errors / queries * 100, 2) if queries > 0 else 0.0,
        "avg_latency_ms": _round_or_none(avg_latency_ms),
        "latency_p95_ms": _round_or_none(p95_ms),
        "avg_rows": _round_or_none(avg_rows),
    }


def _prometheus_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
    )


@router.get("/summary")
async def summary(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(
        None, description="Filter by API key (APISIX consumer), or __ui__ for UI queries"
    ),
    database: str | None = Query(None, description="Filter by database alias"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Whole-window query count, error rate, timeouts, latency, and rows returned.

    Latency and rows read null (like ``p95_latency_ms``) when no successful query
    falls in the window.
    """
    consumer = _scope_consumer(scope, None, consumer)
    _validate_database(database)
    w = tw.promql_window
    try:
        (
            total_res,
            errors_res,
            timeouts_res,
            latency_res,
            p95_res,
            rows_res,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                _count_expr(consumer, database, w), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _count_expr(consumer, database, w, _NOT_SUCCESS), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _count_expr(consumer, database, w, 'status="timeout"'),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _avg_latency_ms_expr(consumer, database, w), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _quantile_ms_expr(0.95, consumer, database, w), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _avg_rows_expr(consumer, database, w), eval_time=tw.eval_time
            ),
        )
    except Exception as exc:
        raise _prometheus_error(exc)

    total = _extract_scalar(total_res)
    errors = _extract_scalar(errors_res)
    return {
        "total_queries": round(total),
        "error_rate": round(errors / total * 100, 2) if total > 0 else 0.0,
        "timeouts": round(_extract_scalar(timeouts_res)),
        "avg_latency_ms": _round_or_none(_scalar_or_none(latency_res)),
        "p95_latency_ms": _round_or_none(_scalar_or_none(p95_res)),
        "avg_rows": _round_or_none(_scalar_or_none(rows_res)),
    }


@router.get("/queries-total")
async def queries_total(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(
        None, description="Filter by API key (APISIX consumer), or __ui__ for UI queries"
    ),
    database: str | None = Query(None, description="Filter by database alias"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> list[dict[str, Any]]:
    """Query count per time bucket (total count, not rate)."""
    consumer = _scope_consumer(scope, None, consumer)
    _validate_database(database)
    try:
        points = await _volume_series(
            lambda window: _count_expr(consumer, database, window), tw
        )
    except Exception as exc:
        raise _prometheus_error(exc)
    return [{"timestamp": p["timestamp"], "value": round(p["value"])} for p in points]


@router.get("/outcomes")
async def outcomes(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(
        None, description="Filter by API key (APISIX consumer), or __ui__ for UI queries"
    ),
    database: str | None = Query(None, description="Filter by database alias"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> list[dict[str, Any]]:
    """Per-bucket query counts split by outcome (success / error / timeout).

    One query grouped by ``status`` rather than one per outcome, so all outcomes
    share the same evaluation timestamps (separate range queries each align their
    steps to their own "now"). Outcomes absent from a bucket read 0; a window
    without any query returns [].
    """
    consumer = _scope_consumer(scope, None, consumer)
    _validate_database(database)
    try:
        breakdown = await _grouped_volume_series(
            lambda window: _count_expr(consumer, database, window, group_by="status"),
            tw,
            ("status",),
            "queries",
        )
    except Exception as exc:
        raise _prometheus_error(exc)

    points_by_status = {series["key"]: series["points"] for series in breakdown["series"]}
    if not points_by_status:
        return []
    return [
        {
            "timestamp": bucket,
            **{
                name: points_by_status[name][i] if name in points_by_status else 0
                for name in QUERY_STATUSES
            },
        }
        for i, bucket in enumerate(breakdown["buckets"])
    ]


@router.get("/latency")
async def latency(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(
        None, description="Filter by API key (APISIX consumer), or __ui__ for UI queries"
    ),
    database: str | None = Query(None, description="Filter by database alias"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, list[dict[str, Any]]]:
    """p50/p95/p99 latency (ms) series of successful queries.

    Each point is a quantile over a trailing ``max(5m, step)`` rate window, so
    long ranges (1h–12h steps) summarize their whole step instead of sampling its
    last 5 minutes — deliberately unlike the gateway's fixed 5m window, because
    DB query traffic is far sparser than HTTP traffic. Steps without a successful
    query read null, not 0 ms. The three quantiles share one pinned (start, end)
    so their timestamps line up (the UI merges them by timestamp). The histogram
    is in seconds; the ``* 1000`` is applied in PromQL so values are milliseconds.
    """
    consumer = _scope_consumer(scope, None, consumer)
    _validate_database(database)
    start, end = tw.start, tw.end
    if start is None or end is None:
        end = time.time()
        start = end - _seconds(tw.promql_window)
    rate_window = f"{max(_LATENCY_MIN_RATE_WINDOW_S, _seconds(tw.step))}s"
    try:
        p50, p95, p99 = await asyncio.gather(*(
            prometheus_client.range_query(
                _quantile_ms_expr(quantile, consumer, database, rate_window),
                duration=tw.promql_window, step=tw.step, start=start, end=end,
            )
            for quantile in (0.5, 0.95, 0.99)
        ))
    except Exception as exc:
        raise _prometheus_error(exc)
    return {
        "p50": _nullable_timeseries(p50),
        "p95": _nullable_timeseries(p95),
        "p99": _nullable_timeseries(p99),
    }


@router.get("/databases-comparison")
async def databases_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(
        None, description="Filter by API key (APISIX consumer), or __ui__ for UI queries"
    ),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-database comparison: queries, share, error rate, latency, rows.

    Every database with traffic in the window is returned (registered databases
    are bounded, unlike keys). ``db_type`` comes from the series labels, so it
    stays right for databases deleted since.
    """
    consumer = _scope_consumer(scope, None, consumer)
    w = tw.promql_window
    try:
        (
            queries_res,
            errors_res,
            latency_res,
            p95_res,
            rows_res,
            total_res,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                _count_expr(consumer, None, w, group_by="db_alias, db_type"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _count_expr(consumer, None, w, _NOT_SUCCESS, group_by="db_alias"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _avg_latency_ms_expr(consumer, None, w, group_by="db_alias"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _quantile_ms_expr(0.95, consumer, None, w, group_by="db_alias"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _avg_rows_expr(consumer, None, w, group_by="db_alias"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _count_expr(consumer, None, w), eval_time=tw.eval_time
            ),
        )
    except Exception as exc:
        raise _prometheus_error(exc)

    # A re-registered alias can carry two db_type series: sum them, and report
    # the type that saw the most queries.
    queries_map: dict[str, float] = {}
    type_counts: dict[str, dict[str, float]] = {}
    for r in queries_res or []:
        metric = r.get("metric", {})
        alias = metric.get("db_alias")
        value = r.get("value")
        if not alias or not value:
            continue
        try:
            count = float(value[1])
        except (IndexError, ValueError, TypeError):
            continue
        queries_map[alias] = queries_map.get(alias, 0.0) + count
        if metric.get("db_type"):
            per_type = type_counts.setdefault(alias, {})
            per_type[metric["db_type"]] = per_type.get(metric["db_type"], 0.0) + count

    errors_map = _map_by_label(errors_res, "db_alias")
    latency_map = _map_by_label(latency_res, "db_alias")
    p95_map = _map_by_label(p95_res, "db_alias")
    rows_map = _map_by_label(rows_res, "db_alias")
    total = _extract_scalar(total_res)

    databases: list[dict[str, Any]] = []
    for alias, queries in queries_map.items():
        if round(queries) <= 0:
            continue
        types = type_counts.get(alias)
        databases.append({
            "database": alias,
            "db_type": max(types, key=types.__getitem__) if types else None,
            **_comparison_row(
                queries,
                total,
                errors_map.get(alias, 0.0),
                latency_map.get(alias),
                p95_map.get(alias),
                rows_map.get(alias),
            ),
        })

    databases.sort(key=lambda d: d["queries"], reverse=True)
    return {"total_queries": round(total), "databases": databases}


@router.get("/consumers-comparison")
async def consumers_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    database: str | None = Query(None, description="Filter by database alias"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-API-key comparison (top 20 by queries): share, error rate, latency, rows.

    UI queries appear under ``__ui__``; series from before the consumer label
    existed under ``(untracked)``. Self-scoped callers see only their own key.
    """
    forced = _scope_consumer(scope, None, None)
    _validate_database(database)
    w = tw.promql_window
    try:
        (
            queries_res,
            errors_res,
            latency_res,
            p95_res,
            rows_res,
            total_res,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk({_CONSUMER_TOPK}, "
                f"{_count_expr(forced, database, w, group_by='consumer')})",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _count_expr(forced, database, w, _NOT_SUCCESS, group_by="consumer"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _avg_latency_ms_expr(forced, database, w, group_by="consumer"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _quantile_ms_expr(0.95, forced, database, w, group_by="consumer"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _avg_rows_expr(forced, database, w, group_by="consumer"),
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _count_expr(forced, database, w), eval_time=tw.eval_time
            ),
        )
    except Exception as exc:
        raise _prometheus_error(exc)

    queries_map = _map_by_label(queries_res, "consumer", empty=UNTRACKED_CONSUMER)
    errors_map = _map_by_label(errors_res, "consumer", empty=UNTRACKED_CONSUMER)
    latency_map = _map_by_label(latency_res, "consumer", empty=UNTRACKED_CONSUMER)
    p95_map = _map_by_label(p95_res, "consumer", empty=UNTRACKED_CONSUMER)
    rows_map = _map_by_label(rows_res, "consumer", empty=UNTRACKED_CONSUMER)
    total = _extract_scalar(total_res)

    consumers: list[dict[str, Any]] = []
    for name, queries in queries_map.items():
        if round(queries) <= 0:
            continue
        consumers.append({
            "consumer": name,
            **_comparison_row(
                queries,
                total,
                errors_map.get(name, 0.0),
                latency_map.get(name),
                p95_map.get(name),
                rows_map.get(name),
            ),
        })

    consumers.sort(key=lambda c: c["queries"], reverse=True)
    return {"total_queries": round(total), "consumers": consumers}


@router.get("/databases-comparison-series")
async def databases_comparison_series(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(
        None, description="Filter by API key (APISIX consumer), or __ui__ for UI queries"
    ),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-database query count bucketed over time (stacked-bar breakdown)."""
    consumer = _scope_consumer(scope, None, consumer)
    try:
        return await _grouped_volume_series(
            lambda window: _count_expr(consumer, None, window, group_by="db_alias"),
            tw,
            ("db_alias",),
            "queries",
        )
    except Exception as exc:
        raise _prometheus_error(exc)


@router.get("/consumers-comparison-series")
async def consumers_comparison_series(
    tw: TimeWindow = Depends(resolve_time_window),
    database: str | None = Query(None, description="Filter by database alias"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-API-key query count bucketed over time.

    Self-scoped callers are forced to their own key; an empty consumer label
    (series from before per-key tracking) surfaces as ``(untracked)``.
    """
    forced = _scope_consumer(scope, None, None)
    _validate_database(database)
    try:
        return await _grouped_volume_series(
            lambda window: _label_untracked(
                _count_expr(forced, database, window, group_by="consumer")
            ),
            tw,
            ("consumer",),
            "queries",
        )
    except Exception as exc:
        raise _prometheus_error(exc)
