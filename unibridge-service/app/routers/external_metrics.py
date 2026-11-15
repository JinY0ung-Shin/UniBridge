"""Traffic-stat metrics for externally-monitored API services.

Read-only dashboards over the ``external-services`` Prometheus scrape job — the
services registered in :class:`~app.models.MonitoredService` that expose RED
metrics (``http_requests_total`` + ``http_request_duration_seconds``) without
routing through the gateway. Shapes mirror the gateway metrics endpoints so the
same frontend charting can drive both, and time semantics are inherited wholesale
from the gateway helpers (full-window ``increase()`` at ``eval_time`` +
deterministic KST calendar bucket axis) by importing them directly.

Because a Spring/Micrometer service exposes only the duration histogram (relabeled
to ``http_request_duration_seconds`` — see ``prometheus/prometheus.yml``) and not a
dedicated request counter, every request-count expression is built with an ``or``
fallback from ``http_requests_total`` to ``http_request_duration_seconds_count`` via
:func:`_count_expr`, so both FastAPI-style and Spring-style services are counted.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_permission
from app.config import settings
from app.database import get_db
from app.models import MonitoredService
from app.routers.gateway import (
    TimeWindow,
    _comparison_table,
    _extract_scalar,
    _extract_timeseries,
    _grouped_volume_series,
    _quantile_series,
    _rate_window,
    _status_code_rows,
    _volume_series,
    resolve_time_window,
)
from app.services import prometheus_client

router = APIRouter(prefix="/admin/external/metrics", tags=["External services"])

# Service names double as PromQL ``service`` label values, so restrict the query
# param to the same slug charset the registry enforces (letters/digits/._-) to
# keep it injection-safe inside a selector.
_SAFE_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_\-.]+$")


def _validate_service(service: str | None) -> None:
    if service and not _SAFE_SERVICE_RE.match(service):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid service name"
        )


def _sel(service: str | None, *extra: str) -> str:
    """Inner PromQL label selector; always scopes to the external-services job.

    ``service`` filters to one registered service; ``extra`` adds further label
    matchers (e.g. ``status=~"5.."``). Returns the comma-joined body without the
    surrounding braces so callers can drop it into any metric name.
    """
    parts = [f'job="{settings.EXTERNAL_SERVICES_JOB}"']
    if service:
        parts.append(f'service="{service}"')
    parts.extend(extra)
    return ",".join(parts)


def _count_expr(sel_inner: str, window: str, group_by: str | None = None) -> str:
    """Request-count PromQL with the counter→histogram-count ``or`` fallback.

    Yields ``sum[ by (grp)] (increase(http_requests_total{sel}[w]))`` OR the same
    over ``http_request_duration_seconds_count``. FastAPI-style services satisfy
    the left operand and the right is ignored per label set; Spring-style services
    (histogram only) fall through to the right. Any status/label matcher folded
    into ``sel_inner`` applies to both operands, so 5xx error counts work for both.
    """
    by = f" by ({group_by})" if group_by else ""
    return (
        f"sum{by} (increase(http_requests_total{{{sel_inner}}}[{window}])) "
        f"or sum{by} (increase(http_request_duration_seconds_count{{{sel_inner}}}[{window}]))"
    )


def _avg_latency_expr(sel_inner: str, window: str) -> str:
    return (
        f"sum(increase(http_request_duration_seconds_sum{{{sel_inner}}}[{window}])) "
        f"/ sum(increase(http_request_duration_seconds_count{{{sel_inner}}}[{window}])) * 1000"
    )


def _quantile_expr(quantile: float, sel_inner: str, window: str, group_by: str | None = None) -> str:
    if group_by:
        return (
            f"histogram_quantile({quantile}, sum by ({group_by}, le) "
            f"(rate(http_request_duration_seconds_bucket{{{sel_inner}}}[{window}])))"
        )
    return (
        f"histogram_quantile({quantile}, "
        f"sum(rate(http_request_duration_seconds_bucket{{{sel_inner}}}[{window}])) by (le))"
    )


async def _comparison(
    tw: TimeWindow, service: str | None, label: str
) -> tuple[float, list[dict[str, Any]]]:
    """(grand total, rows) of a comparison table grouped by ``label``: the
    top 10 by requests, with share, 5xx error rate and p50/p95 latency (ms).

    Series without the label are skipped (the convention requires it;
    unlabeled series can't be attributed).
    """
    sel = _sel(service)
    sel5 = _sel(service, 'status=~"5.."')
    w = tw.promql_window
    try:
        results = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, {_count_expr(sel, w, group_by=label)})",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _count_expr(sel5, w, group_by=label), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _quantile_expr(0.5, sel, w, group_by=label), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _quantile_expr(0.95, sel, w, group_by=label), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(_count_expr(sel, w), eval_time=tw.eval_time),
        )
    except Exception as exc:
        raise prometheus_client.http_error(exc)
    # The histogram is in seconds.
    return _comparison_table(results, label, latency_scale=1000)


@router.get("/summary")
async def summary(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str | None = Query(None, description="Filter by service name"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Aggregate request count, 5xx error rate, and average latency for the window.

    All three are computed over the full resolved window (``promql_window``) and
    evaluated at the window end, matching the gateway summary semantics.
    """
    _validate_service(service)
    sel = _sel(service)
    sel5 = _sel(service, 'status=~"5.."')
    w = tw.promql_window
    total_expr = _count_expr(sel, w)
    # Parenthesize each ``or`` group: ``or`` has the lowest PromQL precedence, so
    # without the parens ``A or B / C or D`` would misparse.
    error_expr = f"({_count_expr(sel5, w)}) / ({_count_expr(sel, w)}) * 100"
    try:
        total_results, error_results, latency_results = await asyncio.gather(
            prometheus_client.instant_query(total_expr, eval_time=tw.eval_time),
            prometheus_client.instant_query(error_expr, eval_time=tw.eval_time),
            prometheus_client.instant_query(_avg_latency_expr(sel, w), eval_time=tw.eval_time),
        )
    except Exception as exc:
        raise prometheus_client.http_error(exc)
    return {
        "total_requests": round(_extract_scalar(total_results)),
        "error_rate": round(_extract_scalar(error_results), 2),
        "avg_latency_ms": round(_extract_scalar(latency_results), 2),
    }


@router.get("/requests")
async def requests(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str | None = Query(None, description="Filter by service name"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Request rate (req/s) over time; the rate window spans at least one step
    so no traffic falls between samples (mirrors gateway, see ``_rate_window``)."""
    _validate_service(service)
    sel = _sel(service)
    window = _rate_window(tw.step)
    expr = (
        f"sum(rate(http_requests_total{{{sel}}}[{window}])) "
        f"or sum(rate(http_request_duration_seconds_count{{{sel}}}[{window}]))"
    )
    try:
        results = await prometheus_client.range_query(
            expr, duration=tw.promql_window, step=tw.step, start=tw.start, end=tw.end
        )
    except Exception as exc:
        raise prometheus_client.http_error(exc)
    return _extract_timeseries(results)


@router.get("/requests-total")
async def requests_total(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str | None = Query(None, description="Filter by service name"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Request volume per time bucket (total count, not rate)."""
    _validate_service(service)
    sel = _sel(service)
    try:
        return await _volume_series(lambda window: _count_expr(sel, window), tw)
    except Exception as exc:
        raise prometheus_client.http_error(exc)


@router.get("/status-codes")
async def status_codes(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str | None = Query(None, description="Filter by service name"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """HTTP status-code distribution over the window (sum by status, desc)."""
    _validate_service(service)
    sel = _sel(service)
    try:
        results = await prometheus_client.instant_query(
            _count_expr(sel, tw.promql_window, group_by="status"),
            eval_time=tw.eval_time,
        )
    except Exception as exc:
        raise prometheus_client.http_error(exc)
    return _status_code_rows(results, "status")


@router.get("/latency")
async def latency(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str | None = Query(None, description="Filter by service name"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    """p50/p95/p99 latency (ms) series, via a bucket rate over at least one step
    (see ``_rate_window``).

    The histogram is in seconds; the ``* 1000`` is applied in PromQL so the
    returned values are milliseconds (matching ``avg_latency_ms`` and the gateway
    latency endpoint, whose apisix histogram is already ms).
    """
    _validate_service(service)
    sel = _sel(service)
    window = _rate_window(tw.step)
    return await _quantile_series(
        lambda quantile: f"({_quantile_expr(quantile, sel, window)}) * 1000", tw
    )


@router.get("/services")
async def registered_services(
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Names of every registered service, for the dashboard's service filter.

    The comparison query is a ``topk(10)`` by traffic, so building the filter from
    it would hide the 11th-busiest service and every service without traffic.
    Only names: the registry itself (addresses etc.) stays behind servers.read.
    """
    result = await db.execute(select(MonitoredService.name).order_by(MonitoredService.name))
    return [name for name in result.scalars().all() if _SAFE_SERVICE_RE.match(name)]


@router.get("/services-comparison")
async def services_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str | None = Query(None, description="Filter by service name"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-service comparison: requests, share, error_rate, p50/p95 latency.

    Whole-window values; ``share`` uses the grand total across all services as the
    denominator (not just the top-10 rows returned), mirroring routes-comparison.
    """
    _validate_service(service)
    total, services = await _comparison(tw, service, "service")
    return {"total_requests": round(total), "services": services}


@router.get("/handlers-comparison")
async def handlers_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str = Query(..., description="Service name (required)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-endpoint (``handler``) comparison within one service.

    The external analogue of the gateway's per-route drill-down — since these
    services aren't authenticated by UniBridge there is no per-API-key axis, so
    the breakdown axis is the RED convention's ``handler`` (route pattern)
    label instead. Whole-window values; ``share`` uses the service's grand
    total as denominator (not just the top-10 rows returned). Series without a
    ``handler`` label are skipped (the convention requires it).
    """
    _validate_service(service)
    total, handlers = await _comparison(tw, service, "handler")
    return {"total_requests": round(total), "handlers": handlers}


@router.get("/services-comparison-series")
async def services_comparison_series(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str | None = Query(None, description="Filter by service name"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-service request volume bucketed over time (stacked-bar breakdown)."""
    _validate_service(service)
    sel = _sel(service)
    try:
        return await _grouped_volume_series(
            lambda window: _count_expr(sel, window, group_by="service"),
            tw,
            ("service",),
            "requests",
        )
    except Exception as exc:
        raise prometheus_client.http_error(exc)
