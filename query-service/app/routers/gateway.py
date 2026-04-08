from __future__ import annotations

import asyncio
import logging
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from httpx import HTTPStatusError

from app.auth import CurrentUser, require_admin
from app.services import apisix_client
from app.services import prometheus_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/gateway", tags=["Gateway"])

MASK_KEEP = 4


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


def _inject_plugins(body: dict[str, Any], existing_plugins: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inject service_key and require_auth into APISIX plugins config, preserving others."""
    service_key = body.pop("service_key", None)
    require_auth = body.pop("require_auth", None)
    plugins = dict(existing_plugins or {})

    # Service key → proxy-rewrite
    if service_key and service_key.get("header_name") and service_key.get("header_value"):
        plugins["proxy-rewrite"] = {
            "headers": {
                "set": {
                    service_key["header_name"]: service_key["header_value"]
                }
            }
        }

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

    if exc.response.status_code == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{resource} not found")
    if exc.response.status_code in (400, 409):
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


@router.get("/routes")
async def list_routes(_admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    try:
        result = await apisix_client.list_resources("routes")
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Routes")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    for item in result.get("items", []):
        item["service_key"] = _extract_service_key(item)
        item["require_auth"] = "key-auth" in item.get("plugins", {})
    return result


@router.get("/routes/{route_id}")
async def get_route(route_id: str, _admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    try:
        route = await apisix_client.get_resource("routes", route_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    route["service_key"] = _extract_service_key(route)
    route["require_auth"] = "key-auth" in route.get("plugins", {})
    return route


@router.put("/routes/{route_id}")
async def save_route(route_id: str, body: dict[str, Any], _admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    # Reject inline upstream
    if "upstream" in body or "nodes" in body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Inline upstream not allowed. Use upstream_id.")

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
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    result["service_key"] = _extract_service_key(result)
    result["require_auth"] = "key-auth" in result.get("plugins", {})
    return result


@router.delete("/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_route(route_id: str, _admin: CurrentUser = Depends(require_admin)) -> None:
    try:
        await apisix_client.delete_resource("routes", route_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")


@router.get("/upstreams")
async def list_upstreams(_admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await apisix_client.list_resources("upstreams")
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstreams")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    return {"items": [], "total": 0}  # unreachable, satisfies type checker


@router.get("/upstreams/{upstream_id}")
async def get_upstream(upstream_id: str, _admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await apisix_client.get_resource("upstreams", upstream_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    return {}  # unreachable, satisfies type checker


@router.put("/upstreams/{upstream_id}")
async def save_upstream(upstream_id: str, body: dict[str, Any], _admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await apisix_client.put_resource("upstreams", upstream_id, body)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    return {}  # unreachable, satisfies type checker


@router.delete("/upstreams/{upstream_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_upstream(upstream_id: str, _admin: CurrentUser = Depends(require_admin)) -> None:
    try:
        await apisix_client.delete_resource("upstreams", upstream_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")


# ── Consumers ───────────────────────────────────────────────────────────────


def _extract_api_key(consumer: dict[str, Any], mask: bool = True) -> str | None:
    """Extract API key from consumer's key-auth plugin config."""
    plugins = consumer.get("plugins", {})
    key_auth = plugins.get("key-auth", {})
    key = key_auth.get("key")
    if not key:
        return None
    if mask:
        return _mask_value(key)
    return key


def _inject_consumer_key(body: dict[str, Any], existing_plugins: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convert api_key field to key-auth plugin config, preserving existing plugins."""
    api_key = body.pop("api_key", None)
    plugins = dict(existing_plugins or {})
    if api_key:
        plugins["key-auth"] = {"key": api_key}
    if plugins:
        body["plugins"] = plugins
    return body


def _strip_consumer_secrets(consumer: dict[str, Any]) -> None:
    """Remove raw key from plugins to prevent leakage via plugins field."""
    plugins = consumer.get("plugins", {})
    key_auth = plugins.get("key-auth", {})
    key_auth.pop("key", None)


@router.get("/consumers")
async def list_consumers(_admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    try:
        result = await apisix_client.list_resources("consumers")
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Consumers")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    for item in result.get("items", []):
        item["api_key"] = _extract_api_key(item, mask=True)
        _strip_consumer_secrets(item)
    return result


@router.get("/consumers/{username}")
async def get_consumer(username: str, _admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    try:
        consumer = await apisix_client.get_resource("consumers", username)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Consumer")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    consumer["api_key"] = _extract_api_key(consumer, mask=True)
    _strip_consumer_secrets(consumer)
    return consumer


@router.put("/consumers/{username}")
async def save_consumer(username: str, body: dict[str, Any], _admin: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    # Check if this is a new consumer and fetch existing plugins
    is_new = True
    existing_plugins: dict[str, Any] | None = None
    try:
        existing = await apisix_client.get_resource("consumers", username)
        is_new = False
        existing_plugins = existing.get("plugins")
    except HTTPStatusError:
        pass
    except Exception:
        pass

    body["username"] = username
    has_new_key = bool(body.get("api_key"))
    body = _inject_consumer_key(body, existing_plugins)

    try:
        result = await apisix_client.put_resource("consumers", username, body)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Consumer")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")

    # Return unmasked key only on creation or when key was changed
    show_key = is_new or has_new_key
    result["api_key"] = _extract_api_key(result, mask=not show_key)
    result["key_created"] = show_key
    _strip_consumer_secrets(result)
    return result


@router.delete("/consumers/{username}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_consumer(username: str, _admin: CurrentUser = Depends(require_admin)) -> None:
    try:
        await apisix_client.delete_resource("consumers", username)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Consumer")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")


# ── Metrics ─────────────────────────────────────────────────────────────────

RANGE_STEPS = {"15m": "15s", "1h": "60s", "6h": "300s", "24h": "600s"}
VALID_RANGES = set(RANGE_STEPS.keys())


def _get_step(time_range: str) -> str:
    return RANGE_STEPS.get(time_range, "60s")


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
    time_range: str = Query("1h", alias="range", description="Time range: 15m, 1h, 6h, 24h"),
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        total_results, error_rate_results, latency_results = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum(increase(apisix_http_status[{time_range}]))"
            ),
            prometheus_client.instant_query(
                'sum(rate(apisix_http_status{code=~"5.."}[5m])) / sum(rate(apisix_http_status[5m])) * 100'
            ),
            prometheus_client.instant_query(
                "sum(rate(apisix_http_latency_sum[5m])) / sum(rate(apisix_http_latency_count[5m]))"
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}")

    return {
        "total_requests": round(_extract_scalar(total_results)),
        "error_rate": round(_extract_scalar(error_rate_results), 2),
        "avg_latency_ms": round(_extract_scalar(latency_results), 2),
    }


@router.get("/metrics/requests")
async def metrics_requests(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        results = await prometheus_client.range_query(
            "sum(rate(apisix_http_status[5m]))",
            duration=time_range,
            step=_get_step(time_range),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}")
    return _extract_timeseries(results)


@router.get("/metrics/status-codes")
async def metrics_status_codes(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        results = await prometheus_client.instant_query(
            f"sum by (code) (increase(apisix_http_status[{time_range}]))"
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}")

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
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, list[dict[str, Any]]]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    step = _get_step(time_range)
    try:
        p50, p95, p99 = await asyncio.gather(
            prometheus_client.range_query(
                "histogram_quantile(0.5, sum(rate(apisix_http_latency_bucket[5m])) by (le))",
                duration=time_range, step=step,
            ),
            prometheus_client.range_query(
                "histogram_quantile(0.95, sum(rate(apisix_http_latency_bucket[5m])) by (le))",
                duration=time_range, step=step,
            ),
            prometheus_client.range_query(
                "histogram_quantile(0.99, sum(rate(apisix_http_latency_bucket[5m])) by (le))",
                duration=time_range, step=step,
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}")

    return {
        "p50": _extract_timeseries(p50),
        "p95": _extract_timeseries(p95),
        "p99": _extract_timeseries(p99),
    }


@router.get("/metrics/top-routes")
async def metrics_top_routes(
    time_range: str = Query("1h", alias="range", description="Time range"),
    _admin: CurrentUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    if time_range not in VALID_RANGES:
        time_range = "1h"
    try:
        results = await prometheus_client.instant_query(
            f"topk(10, sum by (route) (increase(apisix_http_status[{time_range}])))"
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}")

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
