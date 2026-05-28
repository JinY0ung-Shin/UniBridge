from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
import httpx
from httpx import HTTPStatusError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_permission
from app.config import settings
from app.database import get_db
from app.services import apisix_client
from app.services import prometheus_client
from app.services.alert_state import delete_alert_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/gateway", tags=["Gateway"])

MASK_KEEP = 4

# System-managed resources — cannot be deleted or edited via API
PROTECTED_ROUTE_IDS = {"query-api", "llm-proxy", "llm-admin", "s3-api"}
PROTECTED_UPSTREAM_IDS = {"unibridge-service", "litellm"}


def _mask_value(value: str) -> str:
    if len(value) <= MASK_KEEP:
        return "***"
    return "***" + value[-MASK_KEEP:]


def _headers_set_for_route(route: dict[str, Any]) -> dict[str, str]:
    plugins = route.get("plugins") or {}
    if not isinstance(plugins, dict):
        return {}
    pr = plugins.get("proxy-rewrite") or {}
    if not isinstance(pr, dict):
        return {}
    headers = pr.get("headers") or {}
    if not isinstance(headers, dict):
        return {}
    headers_set = headers.get("set") or {}
    if not isinstance(headers_set, dict):
        return {}
    return {
        name: value
        for name, value in headers_set.items()
        if isinstance(name, str) and isinstance(value, str)
    }


def _extract_service_keys(route: dict[str, Any]) -> list[dict[str, str]]:
    headers_set = _headers_set_for_route(route)
    return [
        {"header_name": name, "header_value": _mask_value(value)}
        for name, value in headers_set.items()
    ]


def _extract_service_key(route: dict[str, Any]) -> dict[str, str] | None:
    keys = _extract_service_keys(route)
    return keys[0] if keys else None


def _service_headers_for_route(route: dict[str, Any]) -> dict[str, str]:
    return _headers_set_for_route(route)


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
    """Inject service_keys, strip_prefix, and require_auth into APISIX plugins config, preserving others."""
    legacy_service_key = body.pop("service_key", None)
    service_keys = body.pop("service_keys", None)
    if service_keys is None and legacy_service_key is not None:
        service_keys = [legacy_service_key]
    require_auth = body.pop("require_auth", None)
    strip_prefix = body.pop("strip_prefix", None)
    plugins = dict(existing_plugins or {})

    # Build proxy-rewrite from existing config
    existing_pr = plugins.get("proxy-rewrite")
    pr_config = dict(existing_pr) if isinstance(existing_pr, dict) else {}

    # Service keys → proxy-rewrite headers.set (other header ops like
    # headers.add / headers.remove are preserved).
    # - None: preserve existing headers entirely
    # - list: replace `set` with the provided entries. For each entry, an
    #   empty/missing header_value means "preserve existing value for this
    #   header_name" (lets the UI edit other fields without retyping secrets).
    if service_keys is not None:
        existing_raw_headers = pr_config.get("headers")
        existing_headers = (
            dict(existing_raw_headers) if isinstance(existing_raw_headers, dict) else {}
        )
        existing_raw_set = existing_headers.get("set")
        existing_set = dict(existing_raw_set) if isinstance(existing_raw_set, dict) else {}
        new_set: dict[str, str] = {}
        for sk in service_keys:
            if not isinstance(sk, dict):
                continue
            name = (sk.get("header_name") or "").strip()
            if not name:
                continue
            value = sk.get("header_value")
            if value in (None, ""):
                if name in existing_set:
                    new_set[name] = existing_set[name]
                continue
            new_set[name] = value
        if new_set:
            existing_headers["set"] = new_set
        else:
            existing_headers.pop("set", None)
        if existing_headers:
            pr_config["headers"] = existing_headers
        else:
            pr_config.pop("headers", None)

    # Strip prefix → proxy-rewrite regex_uri
    if strip_prefix is True:
        uri = body.get("uri", "")
        prefix = uri.rstrip("*").rstrip("/")
        if prefix:
            pr_config["regex_uri"] = [f"^{prefix}(.*)", "$1"]
            # APISIX regex_uri rewrites decoded ctx.var.uri by default. Preserve the
            # raw request URI so encoded upstream IDs, e.g. DataHub URNs, stay encoded.
            pr_config["use_real_request_uri_unsafe"] = True
    elif strip_prefix is False:
        pr_config.pop("regex_uri", None)
        pr_config.pop("use_real_request_uri_unsafe", None)
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


def _validate_service_keys(value: Any) -> None:
    """Validate service_keys payload shape; raise 400 if malformed."""
    if value is None:
        return
    if not isinstance(value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="service_keys must be a list",
        )
    seen: set[str] = set()
    for idx, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"service_keys[{idx}] must be an object",
            )
        name = entry.get("header_name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"service_keys[{idx}].header_name is required",
            )
        hv = entry.get("header_value")
        if hv is not None and not isinstance(hv, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"service_keys[{idx}].header_value must be a string",
            )
        normalized = name.strip().lower()
        if normalized in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate header name: {name}",
            )
        seen.add(normalized)


def _validate_service_keys_payload(body: dict[str, Any]) -> None:
    if "service_keys" in body:
        _validate_service_keys(body.get("service_keys"))
        return
    if "service_key" in body and body.get("service_key") is not None:
        _validate_service_keys([body.get("service_key")])


def _attach_service_key_fields(route: dict[str, Any]) -> None:
    service_keys = _extract_service_keys(route)
    route["service_keys"] = service_keys
    route["service_key"] = service_keys[0] if service_keys else None


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
        _attach_service_key_fields(item)
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
    _attach_service_key_fields(route)
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

    _validate_service_keys_payload(body)

    # Look up existing route so we can (a) preserve plugins we don't manage and
    # (b) honor "blank value = preserve existing secret" for service_keys. Only
    # APISIX 404 means "new route, proceed"; any other failure is fatal to avoid
    # silently losing previously stored headers.
    existing_plugins: dict[str, Any] | None = None
    try:
        existing = await apisix_client.get_resource("routes", route_id)
        existing_plugins = existing.get("plugins")
    except HTTPStatusError as exc:
        if exc.response.status_code != 404:
            _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to look up existing route: {exc}",
        )

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
    _attach_service_key_fields(result)
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
        ssl_verify: str | bool = settings.SSL_CA_CERT_PATH or settings.SSL_VERIFY
        async with httpx.AsyncClient(timeout=5.0, verify=ssl_verify) as client:
            resp = await client.get(url, headers=_service_headers_for_route(route))
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
    db: AsyncSession = Depends(get_db),
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

    await delete_alert_state(db, "upstream_health", upstream_id)
    await db.commit()

    from app.routers import alerts as alerts_router
    if alerts_router._alert_state is not None:
        alerts_router._alert_state.discard("upstream_health", upstream_id)

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

_TIER_ORDER = ["15m", "1h", "6h", "24h", "7d", "30d", "60d"]
_TIER_SECONDS = {
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
    "60d": 5184000,
}


def _tier_for_span(span: int) -> str:
    """Smallest preset tier whose duration covers the given span (seconds)."""
    for key in _TIER_ORDER:
        if span <= _TIER_SECONDS[key]:
            return key
    return "60d"


@dataclass
class TimeWindow:
    promql_window: str        # increase() window for summary-type instant queries
    step: str                 # range_query step for non-volume series
    volume_step: str          # range_query step for volume series
    volume_window: str        # increase() window for volume series
    eval_time: float | None   # custom → end epoch; preset → None (= now)
    start: float | None       # custom → start epoch; preset → None
    end: float | None
    is_custom: bool


def _validate_custom_range(start: int, end: int) -> None:
    now = time.time()
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start must be before end",
        )
    if end - start < 60:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="time range must span at least 60 seconds",
        )
    if end > now + 60:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end must not be in the future",
        )


def resolve_time_window(
    time_range: str = Query(
        "1h", alias="range", description="Preset range: 15m, 1h, 6h, 24h, 7d, 30d, 60d"
    ),
    start: int | None = Query(None, description="Custom range start (epoch seconds)"),
    end: int | None = Query(None, description="Custom range end (epoch seconds)"),
) -> TimeWindow:
    if start is not None or end is not None:
        if start is None or end is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="both start and end are required for a custom range",
            )
        _validate_custom_range(start, end)
        span = end - start
        tier = _tier_for_span(span)
        vstep, vwindow = RANGE_VOLUME[tier]
        return TimeWindow(
            promql_window=f"{span}s",
            step=RANGE_STEPS[tier],
            volume_step=vstep,
            volume_window=vwindow,
            eval_time=float(end),
            start=float(start),
            end=float(end),
            is_custom=True,
        )
    if time_range not in VALID_RANGES:
        time_range = "1h"
    vstep, vwindow = RANGE_VOLUME[time_range]
    return TimeWindow(
        promql_window=time_range,
        step=RANGE_STEPS[time_range],
        volume_step=vstep,
        volume_window=vwindow,
        eval_time=None,
        start=None,
        end=None,
        is_custom=False,
    )


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


def _metric_label(row: dict[str, Any], *names: str) -> str:
    metric = row.get("metric", {})
    if not isinstance(metric, dict):
        return "unknown"
    for name in names:
        value = metric.get(name)
        if isinstance(value, str) and value:
            return value
    return "unknown"


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
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    _validate_route(route)
    hs = _labels(route)
    hs5 = _labels(route, 'code=~"5.."')
    try:
        total_results, error_rate_results, latency_results = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum(increase(apisix_http_status{hs}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(rate(apisix_http_status{hs5}[5m])) / sum(rate(apisix_http_status{hs}[5m])) * 100",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(rate(apisix_http_latency_sum{hs}[5m])) / sum(rate(apisix_http_latency_count{hs}[5m]))",
                eval_time=tw.eval_time,
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
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    _validate_route(route)
    hs = _labels(route)
    try:
        results = await prometheus_client.range_query(
            f"sum(rate(apisix_http_status{hs}[5m]))",
            duration=tw.promql_window,
            step=tw.step,
            start=tw.start,
            end=tw.end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)


@router.get("/metrics/status-codes")
async def metrics_status_codes(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    _validate_route(route)
    hs = _labels(route)
    try:
        results = await prometheus_client.instant_query(
            f"sum by (code) (increase(apisix_http_status{hs}[{tw.promql_window}]))",
            eval_time=tw.eval_time,
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
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    _validate_route(route)
    hs = _labels(route)
    step = tw.step
    try:
        p50, p95, p99 = await asyncio.gather(
            prometheus_client.range_query(
                f"histogram_quantile(0.5, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
                duration=tw.promql_window, step=step, start=tw.start, end=tw.end,
            ),
            prometheus_client.range_query(
                f"histogram_quantile(0.95, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
                duration=tw.promql_window, step=step, start=tw.start, end=tw.end,
            ),
            prometheus_client.range_query(
                f"histogram_quantile(0.99, sum(rate(apisix_http_latency_bucket{hs}[5m])) by (le))",
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


@router.get("/metrics/top-routes")
async def metrics_top_routes(
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    try:
        results = await prometheus_client.instant_query(
            f"topk(10, sum by (route) (increase(apisix_http_status[{tw.promql_window}])))",
            eval_time=tw.eval_time,
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


@router.get("/metrics/routes-comparison")
async def metrics_routes_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-route comparison: requests, share, error_rate, p50/p95 latency in one payload."""
    try:
        requests_res, errors_res, p50_res, p95_res = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (route) (increase(apisix_http_status[{tw.promql_window}])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f'sum by (route) (increase(apisix_http_status{{code=~"5.."}}[{tw.promql_window}]))',
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                "histogram_quantile(0.5, sum by (route, le) (rate(apisix_http_latency_bucket[5m])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                "histogram_quantile(0.95, sum by (route, le) (rate(apisix_http_latency_bucket[5m])))",
                eval_time=tw.eval_time,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    def _map_route_value(res: list[dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in res or []:
            route = r.get("metric", {}).get("route")
            if not route:
                continue
            value = r.get("value")
            if not value:
                continue
            try:
                out[route] = float(value[1])
            except (IndexError, ValueError, TypeError):
                continue
        return out

    requests_map = _map_route_value(requests_res)
    errors_map = _map_route_value(errors_res)
    p50_map = _map_route_value(p50_res)
    p95_map = _map_route_value(p95_res)

    # Build id → name map from APISIX. Failure here is non-fatal: we still
    # return Prometheus data, just without the friendly name.
    name_map: dict[str, str] = {}
    try:
        routes_listing = await apisix_client.list_resources("routes")
        for item in routes_listing.get("items", []):
            rid = item.get("id")
            rname = item.get("name")
            if rid and rname:
                name_map[str(rid)] = rname
    except Exception as exc:
        logger.warning("Failed to fetch route names from APISIX: %s", exc)

    total = sum(requests_map.values())
    routes: list[dict[str, Any]] = []
    for route, req in requests_map.items():
        req_rounded = round(req)
        if req_rounded <= 0:
            continue
        share = (req / total * 100) if total > 0 else 0.0
        err = errors_map.get(route, 0.0)
        error_rate = (err / req * 100) if req > 0 else 0.0
        p50 = p50_map.get(route)
        p95 = p95_map.get(route)
        routes.append({
            "route": route,
            "name": name_map.get(route),
            "requests": req_rounded,
            "share": round(share, 2),
            "error_rate": round(error_rate, 2),
            "latency_p50_ms": round(p50, 2) if p50 is not None and not math.isnan(p50) else None,
            "latency_p95_ms": round(p95, 2) if p95 is not None and not math.isnan(p95) else None,
        })

    routes.sort(key=lambda r: r["requests"], reverse=True)
    return {"total_requests": round(total), "routes": routes}


@router.get("/metrics/requests-total")
async def metrics_requests_total(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Request volume per time bucket (total count, not rate)."""
    _validate_route(route)
    hs = _labels(route)
    try:
        results = await prometheus_client.range_query(
            f"sum(increase(apisix_http_status{hs}[{tw.volume_window}]))",
            duration=tw.promql_window,
            step=tw.volume_step,
            start=tw.start,
            end=tw.end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)


# ── LLM Metrics ────────────────────────────────────────────────────────────


@router.get("/metrics/llm/summary")
async def llm_metrics_summary(
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """LLM token usage summary: total tokens, cost, requests, latency."""
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
                f"sum(increase(litellm_total_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_input_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_output_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_spend_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_proxy_total_requests_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                "sum(rate(litellm_request_total_latency_metric_sum[5m]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                "sum(rate(litellm_request_total_latency_metric_count[5m]))",
                eval_time=tw.eval_time,
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
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    """Token usage trend: prompt and completion tokens over time."""
    try:
        prompt_results, completion_results = await asyncio.gather(
            prometheus_client.range_query(
                f"sum(increase(litellm_input_tokens_metric_total[{tw.volume_window}]))",
                duration=tw.promql_window,
                step=tw.volume_step,
                start=tw.start,
                end=tw.end,
            ),
            prometheus_client.range_query(
                f"sum(increase(litellm_output_tokens_metric_total[{tw.volume_window}]))",
                duration=tw.promql_window,
                step=tw.volume_step,
                start=tw.start,
                end=tw.end,
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
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Token usage, request count, and cost breakdown by model."""
    try:
        (
            token_results,
            input_token_results,
            output_token_results,
            cost_results,
            request_results,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_total_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_input_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_output_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_spend_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_proxy_total_requests_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    token_map: dict[str, int] = {}
    for r in token_results:
        model = _metric_label(r, "requested_model", "model")
        try:
            token_map[model] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            token_map[model] = 0

    input_token_map: dict[str, int] = {}
    for r in input_token_results:
        model = _metric_label(r, "requested_model", "model")
        try:
            input_token_map[model] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            input_token_map[model] = 0

    output_token_map: dict[str, int] = {}
    for r in output_token_results:
        model = _metric_label(r, "requested_model", "model")
        try:
            output_token_map[model] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            output_token_map[model] = 0

    cost_map: dict[str, float] = {}
    for r in cost_results:
        model = _metric_label(r, "requested_model", "model")
        try:
            cost_map[model] = round(float(r["value"][1]), 4)
        except (IndexError, ValueError, TypeError):
            cost_map[model] = 0.0

    request_map: dict[str, int] = {}
    for r in request_results:
        model = _metric_label(r, "requested_model", "model")
        try:
            request_map[model] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            request_map[model] = 0

    models = []
    for model in (
        token_map.keys()
        | input_token_map.keys()
        | output_token_map.keys()
        | cost_map.keys()
        | request_map.keys()
    ):
        tokens = token_map.get(model, 0)
        input_tokens = input_token_map.get(model, 0)
        output_tokens = output_token_map.get(model, 0)
        if tokens == 0 and (input_tokens > 0 or output_tokens > 0):
            tokens = input_tokens + output_tokens
        cost = cost_map.get(model, 0.0)
        requests = request_map.get(model, 0)
        if tokens > 0 or input_tokens > 0 or output_tokens > 0 or cost > 0 or requests > 0:
            models.append(
                {
                    "model": model,
                    "tokens": tokens,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost": cost,
                    "requests": requests,
                }
            )
    models.sort(key=lambda x: (x["tokens"], x["requests"]), reverse=True)
    return models


@router.get("/metrics/llm/top-keys")
async def llm_metrics_top_keys(
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Top UniBridge API keys by token usage."""
    try:
        (
            token_results,
            input_token_results,
            output_token_results,
            req_results,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (end_user) (increase(litellm_total_tokens_metric_total[{tw.promql_window}])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_input_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_output_tokens_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_proxy_total_requests_metric_total[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    input_token_map: dict[str, int] = {}
    for r in input_token_results:
        key = _metric_label(r, "end_user")
        try:
            input_token_map[key] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            input_token_map[key] = 0

    output_token_map: dict[str, int] = {}
    for r in output_token_results:
        key = _metric_label(r, "end_user")
        try:
            output_token_map[key] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            output_token_map[key] = 0

    req_map: dict[str, int] = {}
    for r in req_results:
        key = _metric_label(r, "end_user")
        try:
            req_map[key] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            req_map[key] = 0

    keys = []
    for r in token_results:
        key = _metric_label(r, "end_user")
        try:
            tokens = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            tokens = 0
        input_tokens = input_token_map.get(key, 0)
        output_tokens = output_token_map.get(key, 0)
        if tokens == 0 and (input_tokens > 0 or output_tokens > 0):
            tokens = input_tokens + output_tokens
        requests = req_map.get(key, 0)
        if tokens > 0 or input_tokens > 0 or output_tokens > 0 or requests > 0:
            keys.append(
                {
                    "api_key": key,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "tokens": tokens,
                    "requests": requests,
                }
            )
    return keys


@router.get("/metrics/llm/errors")
async def llm_metrics_errors(
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """LLM request success/error rate over time."""
    try:
        success_results, error_results = await asyncio.gather(
            prometheus_client.range_query(
                f"sum(increase(litellm_proxy_total_requests_metric_total[{tw.volume_window}])) - sum(increase(litellm_proxy_failed_requests_metric_total[{tw.volume_window}]))",
                duration=tw.promql_window,
                step=tw.volume_step,
                start=tw.start,
                end=tw.end,
            ),
            prometheus_client.range_query(
                f"sum(increase(litellm_proxy_failed_requests_metric_total[{tw.volume_window}]))",
                duration=tw.promql_window,
                step=tw.volume_step,
                start=tw.start,
                end=tw.end,
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
    tw: TimeWindow = Depends(resolve_time_window),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """LLM request volume per time bucket."""
    try:
        results = await prometheus_client.range_query(
            f"sum(increase(litellm_proxy_total_requests_metric_total[{tw.volume_window}]))",
            duration=tw.promql_window,
            step=tw.volume_step,
            start=tw.start,
            end=tw.end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    return _extract_timeseries(results)
