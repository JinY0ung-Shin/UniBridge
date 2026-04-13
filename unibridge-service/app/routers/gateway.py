from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
import httpx
from httpx import HTTPStatusError

from app.auth import CurrentUser, require_permission
from app.config import settings
from app.services import apisix_client
from app.services import prometheus_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/gateway", tags=["Gateway"])

MASK_KEEP = 4

# System-managed resources — cannot be deleted or edited via API
PROTECTED_ROUTE_IDS = {"query-api", "llm-proxy", "llm-admin"}
PROTECTED_UPSTREAM_IDS = {"unibridge-service", "litellm"}


def _mask_value(value: str) -> str:
    if len(value) <= MASK_KEEP:
        return "***"
    return "***" + value[-MASK_KEEP:]


def _extract_service_key(route: dict[str, Any]) -> dict[str, str] | None:
    plugins = route.get("plugins", {})
    pr = plugins.get("proxy-rewrite", {})
    headers_set = pr.get("headers", {}).get("set", {})
    if not headers_set:
        return None
    for name, value in headers_set.items():
        return {"header_name": name, "header_value": _mask_value(value)}
    return None


def _extract_strip_prefix(route: dict[str, Any]) -> bool:
    plugins = route.get("plugins", {})
    pr = plugins.get("proxy-rewrite", {})
    return "regex_uri" in pr


def _health_path_for_route(route: dict[str, Any]) -> str:
    route_id = route.get("id")
    upstream_id = route.get("upstream_id")
    if route_id in {"llm-proxy", "llm-admin"} or upstream_id == "litellm":
        return "/health/liveliness"
    return "/health"


def _inject_plugins(
    body: dict[str, Any], existing_plugins: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Inject service_key, strip_prefix, and require_auth into APISIX plugins config, preserving others."""
    service_key = body.pop("service_key", None)
    require_auth = body.pop("require_auth", None)
    strip_prefix = body.pop("strip_prefix", None)
    plugins = dict(existing_plugins or {})

    # Build proxy-rewrite from existing config
    pr_config = dict(plugins.get("proxy-rewrite", {}))

    # Service key → proxy-rewrite headers
    if (
        service_key
        and service_key.get("header_name")
        and service_key.get("header_value")
    ):
        pr_config["headers"] = {
            "set": {service_key["header_name"]: service_key["header_value"]}
        }

    # Strip prefix → proxy-rewrite regex_uri
    if strip_prefix is True:
        uri = body.get("uri", "")
        prefix = uri.rstrip("*").rstrip("/")
        if prefix:
            pr_config["regex_uri"] = [f"^{prefix}(.*)", "$1"]
    elif strip_prefix is False:
        pr_config.pop("regex_uri", None)
    # strip_prefix is None → preserve existing state

    if pr_config:
        plugins["proxy-rewrite"] = pr_config
    else:
        plugins.pop("proxy-rewrite", None)

    # Authentication toggle → key-auth
    if require_auth is True:
        plugins["key-auth"] = {}
    elif require_auth is False:
        plugins.pop("key-auth", None)
    # require_auth is None → preserve existing state

    if plugins:
        body["plugins"] = plugins
    elif "plugins" in body:
        del body["plugins"]
    return body


def _handle_apisix_error(exc: HTTPStatusError, resource: str) -> NoReturn:
    detail = f"APISIX error: {exc.response.text}"
    try:
        err_data = exc.response.json()
        detail = err_data.get("error_msg", detail)
    except Exception:
        pass

    logger.error(
        "APISIX %s error: status=%d detail=%s",
        resource,
        exc.response.status_code,
        detail,
    )

    if exc.response.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{resource} not found"
        )
    if exc.response.status_code in (400, 409):
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


@router.get("/routes")
async def list_routes(
    _admin: CurrentUser = Depends(require_permission("gateway.routes.read")),
) -> dict[str, Any]:
    try:
        result = await apisix_client.list_resources("routes")
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Routes")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    for item in result.get("items", []):
        item["service_key"] = _extract_service_key(item)
        item["require_auth"] = "key-auth" in item.get("plugins", {})
        item["strip_prefix"] = _extract_strip_prefix(item)
        item["system"] = item.get("id") in PROTECTED_ROUTE_IDS
    return result


@router.get("/routes/{route_id}")
async def get_route(
    route_id: str,
    _admin: CurrentUser = Depends(require_permission("gateway.routes.read")),
) -> dict[str, Any]:
    try:
        route = await apisix_client.get_resource("routes", route_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    route["service_key"] = _extract_service_key(route)
    route["require_auth"] = "key-auth" in route.get("plugins", {})
    route["strip_prefix"] = _extract_strip_prefix(route)
    return route


@router.put("/routes/{route_id}")
async def save_route(
    route_id: str,
    body: dict[str, Any],
    _admin: CurrentUser = Depends(require_permission("gateway.routes.write")),
) -> dict[str, Any]:
    # Reject inline upstream
    if "upstream" in body or "nodes" in body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inline upstream not allowed. Use upstream_id.",
        )
    # Enforce /api/ prefix — nginx only proxies /api/* to APISIX
    uri = body.get("uri", "")
    if not uri.startswith("/api/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URI must start with /api/ (e.g. /api/myservice/*)",
        )
    if not body.get("upstream_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="upstream_id is required."
        )

    existing_plugins: dict[str, Any] | None = None
    try:
        existing = await apisix_client.get_resource("routes", route_id)
        existing_plugins = existing.get("plugins")
    except HTTPStatusError:
        pass
    except Exception:
        pass  # New route, APISIX unreachable for existing check is non-fatal

    body = _inject_plugins(body, existing_plugins)

    try:
        result = await apisix_client.put_resource("routes", route_id, body)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    logger.info(
        "Route saved: id=%s uri=%s upstream=%s user=%s",
        route_id,
        uri,
        body.get("upstream_id"),
        _admin.username,
    )
    result["service_key"] = _extract_service_key(result)
    result["require_auth"] = "key-auth" in result.get("plugins", {})
    result["strip_prefix"] = _extract_strip_prefix(result)
    return result


@router.delete(
    "/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_route(
    route_id: str,
    _admin: CurrentUser = Depends(require_permission("gateway.routes.write")),
) -> None:
    if route_id in PROTECTED_ROUTE_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="System-managed route cannot be deleted",
        )
    try:
        await apisix_client.delete_resource("routes", route_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    logger.info("Route deleted: id=%s user=%s", route_id, _admin.username)


@router.post("/routes/{route_id}/test")
async def test_route(
    route_id: str,
    _admin: CurrentUser = Depends(require_permission("gateway.routes.read")),
) -> dict[str, Any]:
    """Test upstream connectivity by sending GET /health to the first node."""
    try:
        route = await apisix_client.get_resource("routes", route_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )

    upstream_id = route.get("upstream_id")
    if not upstream_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Route has no upstream_id"
        )

    try:
        upstream = await apisix_client.get_resource("upstreams", upstream_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to get upstream: {exc}",
        )

    nodes = upstream.get("nodes", {})
    if not nodes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Upstream has no nodes"
        )

    first_addr = next(iter(nodes))
    scheme = "https" if _health_path_for_route(route) == "/health/liveliness" else "http"
    url = f"{scheme}://{first_addr}{_health_path_for_route(route)}"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            resp = await client.get(url)
        elapsed_ms = round((time.monotonic() - start) * 1000)
        body: Any = None
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500] if resp.text else None
        logger.info(
            "Route test OK: route=%s node=%s status=%d elapsed=%dms",
            route_id,
            first_addr,
            resp.status_code,
            elapsed_ms,
        )
        return {
            "reachable": True,
            "status_code": resp.status_code,
            "response_time_ms": elapsed_ms,
            "body": body,
            "node": first_addr,
        }
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        logger.warning(
            "Route test FAIL: route=%s node=%s elapsed=%dms error=%s",
            route_id,
            first_addr,
            elapsed_ms,
            exc,
        )
        return {
            "reachable": False,
            "status_code": None,
            "response_time_ms": elapsed_ms,
            "body": None,
            "node": first_addr,
            "error": str(exc),
        }


@router.get("/routes/{route_id}/curl")
async def route_curl(
    route_id: str,
    _admin: CurrentUser = Depends(require_permission("gateway.routes.read")),
) -> dict[str, str]:
    """Generate a sample curl command for a route."""
    try:
        route = await apisix_client.get_resource("routes", route_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )

    uri = route.get("uri", "/")
    path = uri.rstrip("*").rstrip("/") or "/"

    methods = route.get("methods", ["GET"])
    method = methods[0] if methods else "GET"

    base_url = f"https://{settings.HOST_IP}:{settings.UNIBRIDGE_UI_PORT}{path}"

    parts = ["curl", "-k"]
    if method != "GET":
        parts.extend(["-X", method])

    plugins = route.get("plugins", {})
    if "key-auth" in plugins:
        parts.extend(["-H", "'apikey: <YOUR_API_KEY>'"])

    parts.append(f"'{base_url}'")

    return {"curl": " ".join(parts)}


@router.get("/upstreams")
async def list_upstreams(
    _admin: CurrentUser = Depends(require_permission("gateway.upstreams.read")),
) -> dict[str, Any]:
    try:
        result = await apisix_client.list_resources("upstreams")
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstreams")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    for item in result.get("items", []):
        item["system"] = item.get("id") in PROTECTED_UPSTREAM_IDS
    return result


@router.get("/upstreams/{upstream_id}")
async def get_upstream(
    upstream_id: str,
    _admin: CurrentUser = Depends(require_permission("gateway.upstreams.read")),
) -> dict[str, Any]:
    try:
        return await apisix_client.get_resource("upstreams", upstream_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    return {}  # unreachable, satisfies type checker


@router.put("/upstreams/{upstream_id}")
async def save_upstream(
    upstream_id: str,
    body: dict[str, Any],
    _admin: CurrentUser = Depends(require_permission("gateway.upstreams.write")),
) -> dict[str, Any]:
    try:
        result = await apisix_client.put_resource("upstreams", upstream_id, body)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    logger.info("Upstream saved: id=%s user=%s", upstream_id, _admin.username)
    return result  # type: ignore[possibly-undefined]


@router.delete(
    "/upstreams/{upstream_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_upstream(
    upstream_id: str,
    _admin: CurrentUser = Depends(require_permission("gateway.upstreams.write")),
) -> None:
    if upstream_id in PROTECTED_UPSTREAM_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="System-managed upstream cannot be deleted",
        )
    try:
        await apisix_client.delete_resource("upstreams", upstream_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    logger.info("Upstream deleted: id=%s user=%s", upstream_id, _admin.username)


# ── Metrics ─────────────────────────────────────────────────────────────────

RANGE_STEPS = {
    "15m": "15s",
    "1h": "60s",
    "6h": "300s",
    "24h": "600s",
    "7d": "3600s",
    "30d": "21600s",
    "60d": "43200s",
}
VALID_RANGES = set(RANGE_STEPS.keys())

# For request volume chart: (step, increase window) per range
RANGE_VOLUME = {
    "15m": ("60s", "1m"),
    "1h": ("300s", "5m"),
    "6h": ("1800s", "30m"),
    "24h": ("3600s", "1h"),
    "7d": ("3600s", "1h"),
    "30d": ("86400s", "1d"),
    "60d": ("86400s", "1d"),
}

_SAFE_ROUTE_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _get_step(time_range: str) -> str:
    return RANGE_STEPS.get(time_range, "60s")


def _validate_route(route: str | None) -> None:
    if route and not _SAFE_ROUTE_RE.match(route):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid route ID"
        )


def _labels(route: str | None, *extra: str) -> str:
    """Build PromQL label selector like {code=~"5..",route="x"} or empty."""
    parts = list(extra)
    if route:
        parts.append(f'route="{route}"')
    return "{" + ",".join(parts) + "}" if parts else ""


def _extract_scalar(results: list[dict[str, Any]]) -> float:
    """Extract single scalar value from Prometheus instant query result."""
    if not results:
        return 0.0
    value = results[0].get("value", [0, "0"])
    try:
        v = float(value[1])
        return 0.0 if v != v else v  # NaN check
    except (IndexError, ValueError, TypeError):
        return 0.0


def _extract_timeseries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract time series points from Prometheus range query result."""
    if not results:
        return []
    values = results[0].get("values", [])
    points = []
    for ts, val in values:
        try:
            v = float(val)
            if v != v:  # NaN
                v = 0.0
            points.append({"timestamp": int(ts), "value": round(v, 4)})
        except (ValueError, TypeError):
            points.append({"timestamp": int(ts), "value": 0.0})
    return points


@router.get("/metrics/summary")
async def metrics_summary(
    time_range: str = Query(
        "1h", alias="range", description="Time range: 15m, 1h, 6h, 24h"
    ),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    _validate_route(route)
    hs = _labels(route)
    hs5 = _labels(route, 'code=~"5.."')
    try:
        total_results, error_rate_results, latency_results = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum(increase(apisix_http_status{hs}[{time_range}]))"
            ),
            prometheus_client.instant_query(
                f"sum(rate(apisix_http_status{hs5}[5m])) / sum(rate(apisix_http_status{hs}[5m])) * 100"
            ),
            prometheus_client.instant_query(
                f"sum(rate(apisix_http_latency_sum{hs}[5m])) / sum(rate(apisix_http_latency_count{hs}[5m]))"
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    return {
        "total_requests": round(_extract_scalar(total_results)),
        "error_rate": round(_extract_scalar(error_rate_results), 2),
        "avg_latency_ms": round(_extract_scalar(latency_results), 2),
    }


@router.get("/metrics/requests")
async def metrics_requests(
    time_range: str = Query("1h", alias="range", description="Time range"),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    _validate_route(route)
    hs = _labels(route)
    try:
        results = await prometheus_client.range_query(
            f"sum(rate(apisix_http_status{hs}[5m]))",
            duration=time_range,
            step=_get_step(time_range),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)


@router.get("/metrics/status-codes")
async def metrics_status_codes(
    time_range: str = Query("1h", alias="range", description="Time range"),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    _validate_route(route)
    hs = _labels(route)
    try:
        results = await prometheus_client.instant_query(
            f"sum by (code) (increase(apisix_http_status{hs}[{time_range}]))"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    codes = []
    for r in results:
        code = r.get("metric", {}).get("code", "unknown")
        value = r.get("value", [0, "0"])
        try:
            count = round(float(value[1]))
        except (IndexError, ValueError, TypeError):
            count = 0
        if count > 0:
            codes.append({"code": code, "count": count})
    codes.sort(key=lambda x: x["count"], reverse=True)
    return codes


@router.get("/metrics/latency")
async def metrics_latency(
    time_range: str = Query("1h", alias="range", description="Time range"),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    _validate_route(route)
    hs = _labels(route)
    step = _get_step(time_range)
    try:
        p50, p95, p99 = await asyncio.gather(
            prometheus_client.range_query(
                f"histogram_quantile(0.5, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
                duration=time_range,
                step=step,
            ),
            prometheus_client.range_query(
                f"histogram_quantile(0.95, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
                duration=time_range,
                step=step,
            ),
            prometheus_client.range_query(
                f"histogram_quantile(0.99, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
                duration=time_range,
                step=step,
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


@router.get("/metrics/top-routes")
async def metrics_top_routes(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        results = await prometheus_client.instant_query(
            f"topk(10, sum by (route) (increase(apisix_http_status[{time_range}])))"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    routes = []
    for r in results:
        route = r.get("metric", {}).get("route", "unknown")
        value = r.get("value", [0, "0"])
        try:
            requests = round(float(value[1]))
        except (IndexError, ValueError, TypeError):
            requests = 0
        if requests > 0:
            routes.append({"route": route, "requests": requests})
    return routes


@router.get("/metrics/requests-total")
async def metrics_requests_total(
    time_range: str = Query("1h", alias="range", description="Time range"),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Request volume per time bucket (total count, not rate)."""
    if time_range not in VALID_RANGES:
        time_range = "1h"
    _validate_route(route)
    hs = _labels(route)
    step, window = RANGE_VOLUME.get(time_range, ("3600s", "1h"))
    try:
        results = await prometheus_client.range_query(
            f"sum(increase(apisix_http_status{hs}[{window}]))",
            duration=time_range,
            step=step,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)


# ── LLM Metrics ────────────────────────────────────────────────────────────


@router.get("/metrics/llm/summary")
async def llm_metrics_summary(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """LLM token usage summary: total tokens, cost, requests, latency."""
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        (
            tokens,
            prompt,
            completion,
            spend,
            requests,
            latency_sum,
            latency_count,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum(increase(litellm_total_tokens_metric_total[{time_range}]))"
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_input_tokens_metric_total[{time_range}]))"
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_output_tokens_metric_total[{time_range}]))"
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_spend_metric_total[{time_range}]))"
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_proxy_total_requests_metric_total[{time_range}]))"
            ),
            prometheus_client.instant_query(
                "sum(rate(litellm_request_total_latency_metric_sum[5m]))"
            ),
            prometheus_client.instant_query(
                "sum(rate(litellm_request_total_latency_metric_count[5m]))"
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    latency_rate = _extract_scalar(latency_sum)
    latency_cnt = _extract_scalar(latency_count)
    avg_latency = (latency_rate / latency_cnt * 1000) if latency_cnt > 0 else 0.0

    return {
        "total_tokens": round(_extract_scalar(tokens)),
        "prompt_tokens": round(_extract_scalar(prompt)),
        "completion_tokens": round(_extract_scalar(completion)),
        "estimated_cost": round(_extract_scalar(spend), 4),
        "total_requests": round(_extract_scalar(requests)),
        "avg_latency_ms": round(avg_latency, 2),
    }


@router.get("/metrics/llm/tokens")
async def llm_metrics_tokens(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    """Token usage trend: prompt and completion tokens over time."""
    if time_range not in VALID_RANGES:
        time_range = "1h"
    step, window = RANGE_VOLUME.get(time_range, ("3600s", "1h"))
    try:
        prompt_results, completion_results = await asyncio.gather(
            prometheus_client.range_query(
                f"sum(increase(litellm_input_tokens_metric_total[{window}]))",
                duration=time_range,
                step=step,
            ),
            prometheus_client.range_query(
                f"sum(increase(litellm_output_tokens_metric_total[{window}]))",
                duration=time_range,
                step=step,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    return {
        "prompt": _extract_timeseries(prompt_results),
        "completion": _extract_timeseries(completion_results),
    }


@router.get("/metrics/llm/by-model")
async def llm_metrics_by_model(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Token usage and cost breakdown by model."""
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        token_results, cost_results = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum by (model) (increase(litellm_total_tokens_metric_total[{time_range}]))"
            ),
            prometheus_client.instant_query(
                f"sum by (model) (increase(litellm_spend_metric_total[{time_range}]))"
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    cost_map: dict[str, float] = {}
    for r in cost_results:
        model = r.get("metric", {}).get("model", "unknown")
        try:
            cost_map[model] = round(float(r["value"][1]), 4)
        except (IndexError, ValueError, TypeError):
            cost_map[model] = 0.0

    models = []
    for r in token_results:
        model = r.get("metric", {}).get("model", "unknown")
        try:
            tokens = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            tokens = 0
        if tokens > 0:
            models.append(
                {
                    "model": model,
                    "tokens": tokens,
                    "cost": cost_map.get(model, 0.0),
                }
            )
    models.sort(key=lambda x: x["tokens"], reverse=True)
    return models


@router.get("/metrics/llm/top-keys")
async def llm_metrics_top_keys(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Top API keys by token usage."""
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        token_results, req_results = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (hashed_api_key) (increase(litellm_total_tokens_metric_total[{time_range}])))"
            ),
            prometheus_client.instant_query(
                f"sum by (hashed_api_key) (increase(litellm_proxy_total_requests_metric_total[{time_range}]))"
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    req_map: dict[str, int] = {}
    for r in req_results:
        key = r.get("metric", {}).get("hashed_api_key", "unknown")
        try:
            req_map[key] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            req_map[key] = 0

    keys = []
    for r in token_results:
        key = r.get("metric", {}).get("hashed_api_key", "unknown")
        try:
            tokens = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            tokens = 0
        if tokens > 0:
            keys.append(
                {
                    "api_key": key,
                    "tokens": tokens,
                    "requests": req_map.get(key, 0),
                }
            )
    return keys


@router.get("/metrics/llm/errors")
async def llm_metrics_errors(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """LLM request success/error rate over time."""
    if time_range not in VALID_RANGES:
        time_range = "1h"
    step, window = RANGE_VOLUME.get(time_range, ("3600s", "1h"))
    try:
        success_results, error_results = await asyncio.gather(
            prometheus_client.range_query(
                f"sum(increase(litellm_proxy_total_requests_metric_total[{window}])) - sum(increase(litellm_proxy_failed_requests_metric_total[{window}]))",
                duration=time_range,
                step=step,
            ),
            prometheus_client.range_query(
                f"sum(increase(litellm_proxy_failed_requests_metric_total[{window}]))",
                duration=time_range,
                step=step,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    success_points = _extract_timeseries(success_results)
    error_points = _extract_timeseries(error_results)

    error_map = {p["timestamp"]: p["value"] for p in error_points}
    combined = []
    for p in success_points:
        combined.append(
            {
                "timestamp": p["timestamp"],
                "success": round(p["value"]),
                "error": round(error_map.get(p["timestamp"], 0)),
            }
        )
    return combined


@router.get("/metrics/llm/requests-total")
async def llm_metrics_requests_total(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """LLM request volume per time bucket."""
    if time_range not in VALID_RANGES:
        time_range = "1h"
    step, window = RANGE_VOLUME.get(time_range, ("3600s", "1h"))
    try:
        results = await prometheus_client.range_query(
            f"sum(increase(litellm_proxy_total_requests_metric_total[{window}]))",
            duration=time_range,
            step=step,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)
