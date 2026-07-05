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
import math
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import CurrentUser, require_permission
from app.config import settings
from app.routers.gateway import (
    TimeWindow,
    _extract_scalar,
    _extract_timeseries,
    _grouped_volume_series,
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


def _map_by_label(results: list[dict[str, Any]], label_name: str) -> dict[str, float]:
    """Instant-query result → {label value: float}. Items missing the label are
    skipped (the convention requires it; unlabeled series can't be attributed)."""
    out: dict[str, float] = {}
    for r in results or []:
        key = r.get("metric", {}).get(label_name)
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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
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
    """Request rate (req/s) over time, via a trailing 5m rate (mirrors gateway)."""
    _validate_service(service)
    sel = _sel(service)
    expr = (
        f"sum(rate(http_requests_total{{{sel}}}[5m])) "
        f"or sum(rate(http_request_duration_seconds_count{{{sel}}}[5m]))"
    )
    try:
        results = await prometheus_client.range_query(
            expr, duration=tw.promql_window, step=tw.step, start=tw.start, end=tw.end
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )


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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    codes = []
    for r in results:
        code = r.get("metric", {}).get("status", "unknown")
        value = r.get("value", [0, "0"])
        try:
            count = round(float(value[1]))
        except (IndexError, ValueError, TypeError):
            count = 0
        if count > 0:
            codes.append({"code": code, "count": count})
    codes.sort(key=lambda x: x["count"], reverse=True)
    return codes


@router.get("/latency")
async def latency(
    tw: TimeWindow = Depends(resolve_time_window),
    service: str | None = Query(None, description="Filter by service name"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    """p50/p95/p99 latency (ms) series, via a trailing 5m bucket rate.

    The histogram is in seconds; the ``* 1000`` is applied in PromQL so the
    returned values are milliseconds (matching ``avg_latency_ms`` and the gateway
    latency endpoint, whose apisix histogram is already ms).
    """
    _validate_service(service)
    sel = _sel(service)
    step = tw.step
    try:
        p50, p95, p99 = await asyncio.gather(
            prometheus_client.range_query(
                f"({_quantile_expr(0.5, sel, '5m')}) * 1000",
                duration=tw.promql_window, step=step, start=tw.start, end=tw.end,
            ),
            prometheus_client.range_query(
                f"({_quantile_expr(0.95, sel, '5m')}) * 1000",
                duration=tw.promql_window, step=step, start=tw.start, end=tw.end,
            ),
            prometheus_client.range_query(
                f"({_quantile_expr(0.99, sel, '5m')}) * 1000",
                duration=tw.promql_window, step=step, start=tw.start, end=tw.end,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    return {
        "p50": _extract_timeseries(p50),
        "p95": _extract_timeseries(p95),
        "p99": _extract_timeseries(p99),
    }


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
    sel = _sel(service)
    sel5 = _sel(service, 'status=~"5.."')
    w = tw.promql_window
    try:
        requests_res, errors_res, p50_res, p95_res, total_res = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, {_count_expr(sel, w, group_by='service')})",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _count_expr(sel5, w, group_by="service"), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _quantile_expr(0.5, sel, w, group_by="service"), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _quantile_expr(0.95, sel, w, group_by="service"), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(_count_expr(sel, w), eval_time=tw.eval_time),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    requests_map = _map_by_label(requests_res, "service")
    errors_map = _map_by_label(errors_res, "service")
    p50_map = _map_by_label(p50_res, "service")
    p95_map = _map_by_label(p95_res, "service")
    total = _extract_scalar(total_res)

    services: list[dict[str, Any]] = []
    for name, req in requests_map.items():
        req_rounded = round(req)
        if req_rounded <= 0:
            continue
        share = (req / total * 100) if total > 0 else 0.0
        err = errors_map.get(name, 0.0)
        error_rate = (err / req * 100) if req > 0 else 0.0
        p50 = p50_map.get(name)
        p95 = p95_map.get(name)
        services.append({
            "service": name,
            "requests": req_rounded,
            "share": round(share, 2),
            "error_rate": round(error_rate, 2),
            "latency_p50_ms": round(p50 * 1000, 2) if p50 is not None and not math.isnan(p50) else None,
            "latency_p95_ms": round(p95 * 1000, 2) if p95 is not None and not math.isnan(p95) else None,
        })

    services.sort(key=lambda s: s["requests"], reverse=True)
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
    sel = _sel(service)
    sel5 = _sel(service, 'status=~"5.."')
    w = tw.promql_window
    try:
        requests_res, errors_res, p50_res, p95_res, total_res = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, {_count_expr(sel, w, group_by='handler')})",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                _count_expr(sel5, w, group_by="handler"), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _quantile_expr(0.5, sel, w, group_by="handler"), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(
                _quantile_expr(0.95, sel, w, group_by="handler"), eval_time=tw.eval_time
            ),
            prometheus_client.instant_query(_count_expr(sel, w), eval_time=tw.eval_time),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    requests_map = _map_by_label(requests_res, "handler")
    errors_map = _map_by_label(errors_res, "handler")
    p50_map = _map_by_label(p50_res, "handler")
    p95_map = _map_by_label(p95_res, "handler")
    total = _extract_scalar(total_res)

    handlers: list[dict[str, Any]] = []
    for name, req in requests_map.items():
        req_rounded = round(req)
        if req_rounded <= 0:
            continue
        share = (req / total * 100) if total > 0 else 0.0
        err = errors_map.get(name, 0.0)
        error_rate = (err / req * 100) if req > 0 else 0.0
        p50 = p50_map.get(name)
        p95 = p95_map.get(name)
        handlers.append({
            "handler": name,
            "requests": req_rounded,
            "share": round(share, 2),
            "error_rate": round(error_rate, 2),
            "latency_p50_ms": round(p50 * 1000, 2) if p50 is not None and not math.isnan(p50) else None,
            "latency_p95_ms": round(p95 * 1000, 2) if p95 is not None and not math.isnan(p95) else None,
        })

    handlers.sort(key=lambda h: h["requests"], reverse=True)
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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
