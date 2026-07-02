from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
import httpx
from httpx import HTTPStatusError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, get_current_user, get_role_permissions, require_permission
from app.config import settings
from app.database import get_db
from app.models import ApiKeyAccess, QueryTemplate
from app.routers.api_keys import apply_master_consumer_restriction, list_master_consumer_names
from app.services import apisix_client
from app.services import openapi_export
from app.services import prometheus_client
from app.services.alert_state import delete_alert_state
from app.services.audit import log_admin_action
from app.services.settings_manager import settings_manager
from app.services.apisix_system_resources import PROTECTED_ROUTE_IDS, PROTECTED_UPSTREAM_IDS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/gateway", tags=["Gateway"])

MASK_KEEP = 4

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


# Marks a route whose timeout was set explicitly per-route (vs. inheriting the
# global gateway default). Stored as an APISIX route label so a later change to
# the global default skips overridden routes.
_TIMEOUT_OVERRIDE_LABEL = "ub_route_timeout"


def _extract_route_timeout(route: dict[str, Any]) -> int | None:
    """Return the route's read timeout in whole seconds, or None if unset."""
    timeout = route.get("timeout")
    if not isinstance(timeout, dict):
        return None
    read = timeout.get("read")
    if isinstance(read, (int, float)):
        return int(read)
    return None


def _is_timeout_override(route: dict[str, Any]) -> bool:
    labels = route.get("labels")
    return isinstance(labels, dict) and labels.get(_TIMEOUT_OVERRIDE_LABEL) == "1"


def _attach_timeout_fields(route: dict[str, Any]) -> None:
    """Surface timeout state to the UI: effective seconds + whether it's an override."""
    route["timeout_seconds"] = _extract_route_timeout(route)
    route["timeout_override"] = _is_timeout_override(route)


def _apply_route_timeout(
    body: dict[str, Any], existing_route: dict[str, Any] | None
) -> None:
    """Translate the UI ``timeout`` field (seconds or null) into APISIX config.

    A positive integer is an explicit per-route override (flagged via label); a
    null/absent/zero value means inherit the global ``gateway_route_timeout``
    default (and clears the override flag). Existing labels are preserved.
    """
    override = body.pop("timeout", None)
    labels = dict(existing_route.get("labels") or {}) if existing_route else {}

    if isinstance(override, (int, float)) and override > 0:
        seconds = int(override)
        labels[_TIMEOUT_OVERRIDE_LABEL] = "1"
    else:
        seconds = settings_manager.gateway_route_timeout
        labels.pop(_TIMEOUT_OVERRIDE_LABEL, None)

    body["timeout"] = {
        "connect": settings.APISIX_GATEWAY_ROUTE_CONNECT_TIMEOUT,
        "send": seconds,
        "read": seconds,
    }
    if labels:
        body["labels"] = labels
    else:
        body.pop("labels", None)


async def sync_default_route_timeout(seconds: int) -> int:
    """Re-apply the global default timeout to existing gateway routes.

    Skips system/protected routes and routes carrying a per-route override. Best
    effort and idempotent; returns the number of routes patched. Raises only if
    listing routes fails (so the caller can surface a hard APISIX outage).
    """
    listing = await apisix_client.list_resources("routes")
    timeout = {
        "connect": settings.APISIX_GATEWAY_ROUTE_CONNECT_TIMEOUT,
        "send": seconds,
        "read": seconds,
    }
    patched = 0
    for route in listing.get("items", []):
        route_id = route.get("id")
        if not route_id or route_id in PROTECTED_ROUTE_IDS or _is_timeout_override(route):
            continue
        if _extract_route_timeout(route) == seconds:
            continue
        try:
            await apisix_client.patch_resource("routes", str(route_id), {"timeout": timeout})
            patched += 1
        except Exception:
            logger.warning("Failed to apply default timeout to route %s", route_id, exc_info=True)
    return patched


def _health_path_for_route(route: dict[str, Any]) -> str:
    route_id = route.get("id")
    upstream_id = route.get("upstream_id")
    if route_id in {"llm-proxy", "llm-admin"} or upstream_id == "litellm":
        return "/health/liveliness"
    return "/health"


def _http_scheme_for_upstream(upstream: dict[str, Any]) -> str:
    scheme = upstream.get("scheme")
    return scheme if scheme in {"http", "https"} else "http"


def _node_host(node_addr: str) -> str:
    host, _sep, _port = node_addr.rpartition(":")
    return host.strip("[]") if host else node_addr.strip("[]")


def _host_header_for_upstream(upstream: dict[str, Any], node_addr: str) -> str:
    pass_host = upstream.get("pass_host", "pass")
    if pass_host == "node":
        return _node_host(node_addr)
    if pass_host == "rewrite" and isinstance(upstream.get("upstream_host"), str):
        return upstream["upstream_host"]
    return settings.HOST_IP


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
        _attach_timeout_fields(item)
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
    _attach_timeout_fields(route)
    return route


@router.put("/routes/{route_id}")
async def save_route(
    route_id: str,
    body: dict[str, Any],
    _admin: CurrentUser = Depends(require_permission("gateway.routes.write")),
    db: AsyncSession = Depends(get_db),
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
    existing_route: dict[str, Any] | None = None
    try:
        existing_route = await apisix_client.get_resource("routes", route_id)
        existing_plugins = existing_route.get("plugins")
    except HTTPStatusError as exc:
        if exc.response.status_code != 404:
            _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to look up existing route: {exc}",
        )

    body = _inject_plugins(body, existing_plugins)
    _apply_route_timeout(body, existing_route)
    plugins = body.get("plugins")
    if isinstance(plugins, dict) and "key-auth" in plugins:
        body = apply_master_consumer_restriction(
            body,
            await list_master_consumer_names(db),
        )

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
    await log_admin_action(
        db,
        actor=_admin.username,
        action="update" if existing_route else "create",
        resource_type="route",
        resource_id=route_id,
        summary=uri,
        before=existing_route,
        after=result,
    )
    _attach_service_key_fields(result)
    result["require_auth"] = "key-auth" in result.get("plugins", {})
    result["strip_prefix"] = _extract_strip_prefix(result)
    _attach_timeout_fields(result)
    return result


@router.delete(
    "/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_route(
    route_id: str,
    _admin: CurrentUser = Depends(require_permission("gateway.routes.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    if route_id in PROTECTED_ROUTE_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="System-managed route cannot be deleted",
        )
    # Best-effort snapshot of the route before it's gone, for the audit trail.
    before_route: dict[str, Any] | None = None
    try:
        before_route = await apisix_client.get_resource("routes", route_id)
    except Exception:
        before_route = None
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
    await log_admin_action(
        db,
        actor=_admin.username,
        action="delete",
        resource_type="route",
        resource_id=route_id,
        summary=(before_route or {}).get("uri"),
        before=before_route,
        after=None,
    )


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
    scheme = _http_scheme_for_upstream(upstream)
    url = f"{scheme}://{first_addr}{_health_path_for_route(route)}"
    start = time.monotonic()
    try:
        ssl_verify: str | bool = settings.SSL_CA_CERT_PATH or settings.SSL_VERIFY
        headers = _service_headers_for_route(route)
        headers.setdefault("Host", _host_header_for_upstream(upstream, first_addr))
        async with httpx.AsyncClient(timeout=5.0, verify=ssl_verify) as client:
            resp = await client.get(url, headers=headers)
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


@router.get("/openapi.json")
async def gateway_openapi(
    format: str = Query("json", description="Output format (only 'json' is supported)"),
    _admin: CurrentUser = Depends(require_permission("gateway.routes.read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Publish an OpenAPI 3.0 spec of the gateway surface (routes + query templates)."""
    if format != "json":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only format=json is supported",
        )
    try:
        result = await apisix_client.list_resources("routes")
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Routes")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to APISIX: {exc}",
        )
    templates_result = await db.execute(
        select(QueryTemplate).order_by(QueryTemplate.path.asc())
    )
    return openapi_export.build_openapi_spec(
        result.get("items", []),  # type: ignore[possibly-undefined]
        list(templates_result.scalars().all()),
        server_url=f"https://{settings.HOST_IP}:{settings.UNIBRIDGE_UI_PORT}",
        version=settings.APP_VERSION,
    )


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
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    existing_upstream: dict[str, Any] | None = None
    try:
        existing_upstream = await apisix_client.get_resource("upstreams", upstream_id)
    except Exception:
        existing_upstream = None
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
    await log_admin_action(
        db,
        actor=_admin.username,
        action="update" if existing_upstream else "create",
        resource_type="upstream",
        resource_id=upstream_id,
        summary=body.get("name") or (existing_upstream or {}).get("name"),
        before=existing_upstream,
        after=result,  # type: ignore[possibly-undefined]
    )
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
    before_upstream: dict[str, Any] | None = None
    try:
        before_upstream = await apisix_client.get_resource("upstreams", upstream_id)
    except Exception:
        before_upstream = None
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
    await log_admin_action(
        db,
        actor=_admin.username,
        action="delete",
        resource_type="upstream",
        resource_id=upstream_id,
        summary=(before_upstream or {}).get("name"),
        before=before_upstream,
        after=None,
    )


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

# Calendar-bucket granularity (overrides auto stepping when requested).
# Bars are aligned to KST (UTC+9, no DST) calendar boundaries; weeks start Monday.
_KST_OFFSET = 9 * 3600
BUCKET_SECONDS = {"hour": 3600, "day": 86400, "week": 604800}
BUCKET_WINDOW = {"hour": "1h", "day": "1d", "week": "7d"}
VALID_BUCKETS = set(BUCKET_SECONDS.keys())


def _align_down_kst(epoch: float, bucket: str) -> int:
    """Floor an epoch (seconds) to the start of its KST calendar bucket."""
    bsec = BUCKET_SECONDS[bucket]
    kst = int(epoch) + _KST_OFFSET
    if bucket == "week":
        # 1970-01-01 (epoch day 0) is a Thursday; Monday is day index ≡ 4 (mod 7).
        day = kst // 86400
        monday = ((day - 4) // 7) * 7 + 4
        aligned_kst = monday * 86400
    else:
        aligned_kst = (kst // bsec) * bsec
    return aligned_kst - _KST_OFFSET


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
    bucket: str = "auto"      # calendar bucket for volume series: auto|hour|day|week


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


def _bucketed_window(raw_start: float, raw_end: float, bucket: str) -> TimeWindow:
    """Build a TimeWindow whose volume series snaps to KST calendar buckets.

    The query window is widened so it starts at the bucket containing raw_start.
    The raw end stays at "now" or the requested custom end so Prometheus never
    evaluates a range query at a future bucket boundary.
    """
    bsec = BUCKET_SECONDS[bucket]
    aligned_start = _align_down_kst(raw_start, bucket)
    bstep = f"{bsec}s"
    return TimeWindow(
        promql_window=f"{int(raw_end - aligned_start)}s",
        step=bstep,
        volume_step=bstep,
        volume_window=BUCKET_WINDOW[bucket],
        eval_time=float(raw_end),
        start=float(aligned_start),
        end=float(raw_end),
        is_custom=True,
        bucket=bucket,
    )


def resolve_time_window(
    time_range: str = Query(
        "1h", alias="range", description="Preset range: 15m, 1h, 6h, 24h, 7d, 30d, 60d"
    ),
    start: int | None = Query(None, description="Custom range start (epoch seconds)"),
    end: int | None = Query(None, description="Custom range end (epoch seconds)"),
    bucket: str = Query(
        "auto",
        description="Calendar bucket for volume series: auto, hour, day, week",
    ),
) -> TimeWindow:
    if start is not None or end is not None:
        if start is None or end is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="both start and end are required for a custom range",
            )
        _validate_custom_range(start, end)
        if bucket in VALID_BUCKETS:
            return _bucketed_window(float(start), float(end), bucket)
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
    if bucket in VALID_BUCKETS:
        now = time.time()
        return _bucketed_window(now - _TIER_SECONDS[time_range], now, bucket)
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
_SAFE_CONSUMER_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _get_step(time_range: str) -> str:
    return RANGE_STEPS.get(time_range, "60s")


def _validate_route(route: str | None) -> None:
    if route and not _SAFE_ROUTE_RE.match(route):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid route ID"
        )


def _validate_consumer(consumer: str | None) -> None:
    if consumer and not _SAFE_CONSUMER_RE.match(consumer):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid consumer name"
        )


@dataclass
class _MonitoringScope:
    """Result of gateway-monitoring authorization + consumer scoping."""

    forced_consumer: str | None  # when restricted, the consumer to force-filter on
    restricted: bool             # True when the caller may only see their own traffic


# Sentinel consumer that matches no Prometheus series — used when a self-scoped
# caller has no API key yet, so they see zero data instead of everyone's. Wrapped
# in "__" so api_keys.create_api_key rejects any real key with this name (a real
# key named this would otherwise leak its traffic to every keyless self caller).
_SELF_NO_KEY = "__no_self_api_key__"


async def _monitoring_scope_for(user: CurrentUser, db: AsyncSession) -> _MonitoringScope:
    """Authorize gateway monitoring and decide consumer scoping.

    - ``gateway.monitoring.read`` → full access (no forced consumer filter).
    - only ``gateway.monitoring.self`` → force-scope to the caller's own API-key
      consumer (or a no-match sentinel if they have none).
    - neither → 403.
    """
    perms = await get_role_permissions(db, user.role)
    if "gateway.monitoring.read" in perms:
        return _MonitoringScope(forced_consumer=None, restricted=False)
    if "gateway.monitoring.self" in perms:
        owned = (
            await db.execute(
                select(ApiKeyAccess.consumer_name).where(ApiKeyAccess.owner == user.sub)
            )
        ).scalars().first()
        return _MonitoringScope(forced_consumer=owned or _SELF_NO_KEY, restricted=True)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Required permission: gateway.monitoring.read or gateway.monitoring.self",
    )


async def _gateway_monitoring_scope(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> _MonitoringScope:
    """Depends wrapper around :func:`_monitoring_scope_for` for JWT callers."""
    return await _monitoring_scope_for(user, db)


def _scope_consumer(scope: _MonitoringScope, route: str | None, consumer: str | None) -> str | None:
    """Validate the requested consumer, or override it for self-scoped callers.

    Self-scoped callers cannot widen their view: their consumer filter is forced
    to their own key regardless of any ``consumer`` query param they send, and
    LLM-proxy traffic is hidden from them entirely (LLM monitoring is admin-only).
    """
    if scope.restricted:
        if route in {"llm-proxy", "llm-messages", "llm-responses"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="LLM metrics are not available",
            )
        return scope.forced_consumer
    _validate_consumer(consumer)
    return consumer


def _labels(route: str | None, consumer: str | None, *extra: str) -> str:
    """Build PromQL label selector.

    Defaults exclude the ``llm-proxy`` and ``llm-messages`` routes so the
    gateway monitoring page omits LLM traffic (shown separately on the LLM
    monitoring page). When ``route`` is explicitly set, that filter replaces
    the default exclusion.
    """
    parts = list(extra)
    if route:
        parts.append(f'route="{route}"')
    else:
        parts.append('route!="llm-proxy"')
        parts.append('route!="llm-messages"')
        parts.append('route!="llm-responses"')
    if consumer:
        parts.append(f'consumer="{consumer}"')
    return "{" + ",".join(parts) + "}" if parts else ""


def _llm_labels(*extra: str) -> str:
    """PromQL selector for LLM-proxy traffic across the three LLM routes.

    LLM requests pass through APISIX on the ``llm-proxy``/``llm-messages``/
    ``llm-responses`` routes, so ``apisix_http_status`` carries their real HTTP
    status codes — including gateway-layer errors (401/403/429) that never reach
    LiteLLM and so are invisible to the ``litellm_*`` counters.
    """
    parts = list(extra)
    parts.append('route=~"llm-proxy|llm-messages|llm-responses"')
    return "{" + ",".join(parts) + "}"


def _llm_key_selector(api_key: str | None) -> str:
    """PromQL selector scoping ``litellm_*`` counters to one API key.

    The proxy route stamps each LLM request with the UniBridge API-key name as
    the LiteLLM ``end_user`` (``x-litellm-end-user-id: $consumer_name`` on the
    ``llm-proxy`` route), so filtering on ``end_user`` scopes every litellm
    metric to a single key. Returns an empty string (no selector) when unscoped.
    """
    return f'{{end_user="{api_key}"}}' if api_key else ""


def _llm_consumer_extra(api_key: str | None) -> tuple[str, ...]:
    """APISIX ``consumer`` label extras for ``_llm_labels``, scoped to one key.

    ``apisix_http_status`` carries the API-key name as ``consumer`` (the same
    value the litellm ``end_user`` holds), so status/error series can be scoped
    to the same key the litellm counters are filtered by.
    """
    return (f'consumer="{api_key}"',) if api_key else ()


def _metric_label(row: dict[str, Any], *names: str) -> str:
    metric = row.get("metric", {})
    if not isinstance(metric, dict):
        return "unknown"
    for name in names:
        value = metric.get(name)
        if isinstance(value, str) and value:
            return value
    return "unknown"


async def _route_name_map() -> dict[str, str]:
    """Route id → friendly name from APISIX. Failure here is non-fatal: callers
    still return Prometheus data, just without the friendly name."""
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
    return name_map


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


def _bucket_points(
    points: list[dict[str, Any]], tw: TimeWindow
) -> list[dict[str, Any]]:
    """Re-label calendar-bucketed volume points to the bucket's start time.

    Prometheus samples increase(metric[bucket]) at each bucket boundary, so a
    sample at time T covers (T-bucket, T]. We shift the timestamp back by one
    bucket so it marks the period start, and drop the leading sample whose
    window falls entirely before the requested range.
    """
    if tw.bucket not in VALID_BUCKETS or tw.start is None:
        return points
    bsec = BUCKET_SECONDS[tw.bucket]
    start = int(tw.start)
    out = []
    for p in points:
        bucket_start = int(p["timestamp"]) - bsec
        if bucket_start < start:
            continue
        out.append({"timestamp": bucket_start, "value": p["value"]})
    return out


async def _volume_series(
    query_for_window: Callable[[str], str],
    tw: TimeWindow,
) -> list[dict[str, Any]]:
    if tw.bucket not in VALID_BUCKETS or tw.start is None or tw.end is None:
        results = await prometheus_client.range_query(
            query_for_window(tw.volume_window),
            duration=tw.promql_window,
            step=tw.volume_step,
            start=tw.start,
            end=tw.end,
        )
        return _extract_timeseries(results)

    bsec = BUCKET_SECONDS[tw.bucket]
    raw_end = float(tw.eval_time if tw.eval_time is not None else tw.end)
    current_start = float(_align_down_kst(raw_end, tw.bucket))
    points: list[dict[str, Any]] = []
    if current_start > float(tw.start):
        completed_results = await prometheus_client.range_query(
            query_for_window(tw.volume_window),
            duration=tw.promql_window,
            step=tw.volume_step,
            start=tw.start,
            end=current_start,
        )
        points = _bucket_points(_extract_timeseries(completed_results), tw)

    elapsed = int(raw_end - current_start)
    if elapsed > 0:
        partial_window = f"{min(max(elapsed, 1), bsec)}s"
        partial_results = await prometheus_client.instant_query(
            query_for_window(partial_window),
            eval_time=raw_end,
        )
        points.append(
            {
                "timestamp": int(current_start),
                "value": round(_extract_scalar(partial_results), 4),
            }
        )
    return points


# Top-N cap for bucketed breakdowns: keep at most this many series; the rest
# collapse into a single "(others)" series.
_GROUPED_TOPN = 12
_OTHERS_KEY = "(others)"


def _grouped_extract_timeseries(
    results: list[dict[str, Any]], label_names: tuple[str, ...]
) -> dict[str, dict[int, float]]:
    """Group a Prometheus range-query result by label(s).

    Returns {label_value: {timestamp: value}}. Mirrors _extract_timeseries but
    keeps every result item (one per label combination) instead of just the
    first, and indexes points by timestamp so callers can align them onto a
    shared bucket axis.
    """
    out: dict[str, dict[int, float]] = {}
    for r in results or []:
        key = _metric_label(r, *label_names)
        series = out.setdefault(key, {})
        for ts, val in r.get("values", []):
            try:
                v = float(val)
                if v != v:  # NaN
                    v = 0.0
            except (ValueError, TypeError):
                v = 0.0
            ts_i = int(ts)
            series[ts_i] = series.get(ts_i, 0.0) + v
    return out


def _grouped_instant(
    results: list[dict[str, Any]], label_names: tuple[str, ...]
) -> dict[str, float]:
    """Group a Prometheus instant-query result by label(s) → {key: value}."""
    out: dict[str, float] = {}
    for r in results or []:
        key = _metric_label(r, *label_names)
        value = r.get("value")
        if not value:
            continue
        try:
            v = float(value[1])
            if v != v:  # NaN
                v = 0.0
        except (IndexError, ValueError, TypeError):
            continue
        out[key] = out.get(key, 0.0) + v
    return out


def _assemble_grouped_breakdown(
    per_key_points: dict[str, dict[int, float]],
    buckets: list[int],
    unit: str,
) -> dict[str, Any]:
    """Build the {buckets, series, unit} payload with top-N + "(others)".

    ``per_key_points`` maps label value → {bucket_start: value}. ``buckets`` is
    the shared ascending bucket axis; each series' points[] is aligned to it
    (missing buckets filled with 0). Series whose total is 0 are dropped; the
    series beyond the top-12 by total collapse into a single "(others)" series.
    """
    rounder = round if unit == "requests" else (lambda v: round(v, 4))

    raw_series: list[dict[str, Any]] = []
    for key, ts_map in per_key_points.items():
        points = [rounder(ts_map.get(b, 0.0)) for b in buckets]
        total = rounder(sum(ts_map.get(b, 0.0) for b in buckets))
        if total <= 0:
            continue
        raw_series.append({"key": key, "total": total, "points": points})

    raw_series.sort(key=lambda s: s["total"], reverse=True)
    top = raw_series[:_GROUPED_TOPN]
    rest = raw_series[_GROUPED_TOPN:]

    if rest:
        others_points = [
            rounder(sum(s["points"][i] for s in rest)) for i in range(len(buckets))
        ]
        others_total = rounder(sum(s["total"] for s in rest))
        if others_total > 0:
            top.append(
                {"key": _OTHERS_KEY, "total": others_total, "points": others_points}
            )

    return {"buckets": buckets, "series": top, "unit": unit}


async def _grouped_volume_series(
    query_for_window: Callable[[str], str],
    tw: TimeWindow,
    label_names: tuple[str, ...],
    unit: str,
) -> dict[str, Any]:
    """Bucketed per-dimension breakdown mirroring _volume_series.

    ``query_for_window(window)`` must yield a PromQL expression that groups by
    ``label_names`` (e.g. ``sum by (route) (increase(metric{...}[<window>]))``).
    Returns one points[] per label value, all aligned to a single shared bucket
    axis, with top-12 + "(others)" applied. See _assemble_grouped_breakdown.
    """
    # Non-calendar (auto) path: a plain range query over volume_step.
    if tw.bucket not in VALID_BUCKETS or tw.start is None or tw.end is None:
        results = await prometheus_client.range_query(
            query_for_window(tw.volume_window),
            duration=tw.promql_window,
            step=tw.volume_step,
            start=tw.start,
            end=tw.end,
        )
        grouped = _grouped_extract_timeseries(results, label_names)
        buckets = sorted({ts for series in grouped.values() for ts in series})
        return _assemble_grouped_breakdown(grouped, buckets, unit)

    bsec = BUCKET_SECONDS[tw.bucket]
    raw_end = float(tw.eval_time if tw.eval_time is not None else tw.end)
    current_start = int(_align_down_kst(raw_end, tw.bucket))
    start = int(tw.start)

    # Completed buckets: range query, then shift each sample back one bucket so
    # its timestamp marks the period start (mirrors _bucket_points).
    per_key_points: dict[str, dict[int, float]] = {}
    bucket_set: set[int] = set()
    if float(current_start) > float(tw.start):
        completed = await prometheus_client.range_query(
            query_for_window(tw.volume_window),
            duration=tw.promql_window,
            step=tw.volume_step,
            start=tw.start,
            end=float(current_start),
        )
        grouped = _grouped_extract_timeseries(completed, label_names)
        for key, ts_map in grouped.items():
            dest = per_key_points.setdefault(key, {})
            for ts, value in ts_map.items():
                bucket_start = ts - bsec
                if bucket_start < start:
                    continue
                dest[bucket_start] = dest.get(bucket_start, 0.0) + value
                bucket_set.add(bucket_start)

    # Partial current bucket: instant query over the elapsed window.
    elapsed = int(raw_end - current_start)
    if elapsed > 0:
        partial_window = f"{min(max(elapsed, 1), bsec)}s"
        partial = await prometheus_client.instant_query(
            query_for_window(partial_window),
            eval_time=raw_end,
        )
        for key, value in _grouped_instant(partial, label_names).items():
            dest = per_key_points.setdefault(key, {})
            dest[current_start] = dest.get(current_start, 0.0) + value
        bucket_set.add(current_start)

    buckets = sorted(bucket_set)
    return _assemble_grouped_breakdown(per_key_points, buckets, unit)


@router.get("/metrics/summary")
async def metrics_summary(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    _validate_route(route)
    consumer = _scope_consumer(scope, route, consumer)
    hs = _labels(route, consumer)
    hs5 = _labels(route, consumer, 'code=~"5.."')
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
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> list[dict[str, Any]]:
    _validate_route(route)
    consumer = _scope_consumer(scope, route, consumer)
    hs = _labels(route, consumer)
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
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> list[dict[str, Any]]:
    _validate_route(route)
    consumer = _scope_consumer(scope, route, consumer)
    hs = _labels(route, consumer)
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
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, list[dict[str, Any]]]:
    _validate_route(route)
    consumer = _scope_consumer(scope, route, consumer)
    hs = _labels(route, consumer)
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
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> list[dict[str, Any]]:
    # No per-route filter here (aggregates across routes; llm-proxy excluded by _labels default).
    consumer = _scope_consumer(scope, None, consumer)
    hs = _labels(None, consumer)
    try:
        results = await prometheus_client.instant_query(
            f"topk(10, sum by (route) (increase(apisix_http_status{hs}[{tw.promql_window}])))",
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


async def usages_payload(
    scope: _MonitoringScope,
    date: str | None,
    consumer: str | None,
    include_llm: bool,
) -> dict[str, Any]:
    """Per-route request counts for one KST calendar day (shared implementation).

    Backs both the JWT admin endpoint (``/admin/gateway/metrics/usages``) and
    the API-key-facing ``/usages`` route. Counts come from the same
    ``apisix_http_status`` counter the routes-comparison endpoint uses; they
    are ``increase()`` estimates from Prometheus (15s scrapes), not exact
    integers, and only dates within the Prometheus retention window (60d)
    return data — older dates yield zero rows.
    """
    if include_llm and scope.restricted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="LLM metrics are not available",
        )
    consumer = _scope_consumer(scope, None, consumer)

    now = time.time()
    if date is None:
        start = _align_down_kst(now, "day")
    else:
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid date format. Use YYYY-MM-DD.",
            )
        # Midnight KST for that calendar day (KST = UTC+9, no DST).
        start = int(parsed.timestamp()) - _KST_OFFSET
    if start >= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date must not be in the future",
        )
    end = min(start + 86400, now)
    span = max(int(end - start), 1)

    if include_llm:
        hs = f'{{consumer="{consumer}"}}' if consumer else ""
    else:
        hs = _labels(None, consumer)
    try:
        results = await prometheus_client.instant_query(
            f"sum by (route) (increase(apisix_http_status{hs}[{span}s]))",
            eval_time=end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    name_map = await _route_name_map()

    routes: list[dict[str, Any]] = []
    total = 0
    for r in results:
        route = r.get("metric", {}).get("route", "unknown")
        value = r.get("value", [0, "0"])
        try:
            requests = round(float(value[1]))
        except (IndexError, ValueError, TypeError):
            requests = 0
        if requests <= 0:
            continue
        total += requests
        routes.append({"route": route, "name": name_map.get(route), "requests": requests})
    routes.sort(key=lambda r: r["requests"], reverse=True)

    resolved_date = datetime.fromtimestamp(start + _KST_OFFSET, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )
    return {
        "date": resolved_date,
        "consumer": None if consumer == _SELF_NO_KEY else consumer,
        "total_requests": total,
        "routes": routes,
    }


@router.get("/metrics/usages")
async def metrics_usages(
    date: str | None = Query(
        None,
        description="KST calendar date (YYYY-MM-DD). Defaults to today (KST).",
    ),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    include_llm: bool = Query(
        False, description="Include llm-proxy/llm-messages/llm-responses routes"
    ),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-route request counts for one KST calendar day (JWT callers)."""
    return await usages_payload(scope, date=date, consumer=consumer, include_llm=include_llm)


@router.get("/metrics/routes-comparison")
async def metrics_routes_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-route comparison: requests, share, error_rate, p50/p95 latency in one payload."""
    consumer = _scope_consumer(scope, None, consumer)
    # Routes-comparison never targets a single route — it groups by route.
    # Default selector hides llm-proxy (LLM monitoring page covers that).
    hs = _labels(None, consumer)
    hs5 = _labels(None, consumer, 'code=~"5.."')
    try:
        requests_res, errors_res, p50_res, p95_res = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (route) (increase(apisix_http_status{hs}[{tw.promql_window}])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (route) (increase(apisix_http_status{hs5}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"histogram_quantile(0.5, sum by (route, le) (rate(apisix_http_latency_bucket{hs}[5m])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"histogram_quantile(0.95, sum by (route, le) (rate(apisix_http_latency_bucket{hs}[5m])))",
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

    name_map = await _route_name_map()

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


@router.get("/metrics/consumers-comparison")
async def metrics_consumers_comparison(
    tw: TimeWindow = Depends(resolve_time_window),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-API-key (consumer) comparison: requests, share, error_rate, p50/p95 latency.

    Self-scoped callers see only their own key (forced consumer); admins see all.
    LLM-proxy traffic is excluded by the default selector (covered on the LLM page).
    """
    # Comparison groups by consumer; restricted callers are forced to their own.
    forced = _scope_consumer(scope, None, None)
    hs = _labels(None, forced)
    hs5 = _labels(None, forced, 'code=~"5.."')
    try:
        requests_res, errors_res, p50_res, p95_res, total_res = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (consumer) (increase(apisix_http_status{hs}[{tw.promql_window}])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (consumer) (increase(apisix_http_status{hs5}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"histogram_quantile(0.5, sum by (consumer, le) (rate(apisix_http_latency_bucket{hs}[5m])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"histogram_quantile(0.95, sum by (consumer, le) (rate(apisix_http_latency_bucket{hs}[5m])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(apisix_http_status{hs}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    def _map_consumer_value(res: list[dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in res or []:
            # Requests with no API key carry an empty consumer label. The fallback
            # contains characters _SAFE_CONSUMER_RE rejects, so it can never collide
            # with a real key name.
            consumer = r.get("metric", {}).get("consumer") or "(no api key)"
            value = r.get("value")
            if not value:
                continue
            try:
                out[consumer] = float(value[1])
            except (IndexError, ValueError, TypeError):
                continue
        return out

    requests_map = _map_consumer_value(requests_res)
    errors_map = _map_consumer_value(errors_res)
    p50_map = _map_consumer_value(p50_res)
    p95_map = _map_consumer_value(p95_res)

    # Denominator for share is the true total across all consumers, not just the
    # top-10 rows, so shares stay accurate when more than 10 keys are active.
    total = _extract_scalar(total_res)
    consumers: list[dict[str, Any]] = []
    for consumer, req in requests_map.items():
        req_rounded = round(req)
        if req_rounded <= 0:
            continue
        share = (req / total * 100) if total > 0 else 0.0
        err = errors_map.get(consumer, 0.0)
        error_rate = (err / req * 100) if req > 0 else 0.0
        p50 = p50_map.get(consumer)
        p95 = p95_map.get(consumer)
        consumers.append({
            "consumer": consumer,
            "requests": req_rounded,
            "share": round(share, 2),
            "error_rate": round(error_rate, 2),
            "latency_p50_ms": round(p50, 2) if p50 is not None and not math.isnan(p50) else None,
            "latency_p95_ms": round(p95, 2) if p95 is not None and not math.isnan(p95) else None,
        })

    consumers.sort(key=lambda c: c["requests"], reverse=True)
    return {"total_requests": round(total), "consumers": consumers}


@router.get("/metrics/routes-comparison-series")
async def metrics_routes_comparison_series(
    tw: TimeWindow = Depends(resolve_time_window),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-route request volume bucketed over time (stacked-bar breakdown)."""
    consumer = _scope_consumer(scope, None, consumer)
    hs = _labels(None, consumer)
    try:
        return await _grouped_volume_series(
            lambda window: f"sum by (route) (increase(apisix_http_status{hs}[{window}]))",
            tw,
            ("route",),
            "requests",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )


@router.get("/metrics/consumers-comparison-series")
async def metrics_consumers_comparison_series(
    tw: TimeWindow = Depends(resolve_time_window),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> dict[str, Any]:
    """Per-API-key (consumer) request volume bucketed over time.

    Self-scoped callers are forced to their own key; an empty consumer label
    (requests with no API key) surfaces as "(no api key)".
    """
    forced = _scope_consumer(scope, None, None)
    hs = _labels(None, forced)
    try:
        breakdown = await _grouped_volume_series(
            lambda window: f"sum by (consumer) (increase(apisix_http_status{hs}[{window}]))",
            tw,
            ("consumer",),
            "requests",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
    for series in breakdown["series"]:
        if series["key"] == "unknown":
            series["key"] = "(no api key)"
    return breakdown


@router.get("/metrics/requests-total")
async def metrics_requests_total(
    tw: TimeWindow = Depends(resolve_time_window),
    route: str | None = Query(None, description="Filter by route ID"),
    consumer: str | None = Query(None, description="Filter by APISIX consumer name (API key)"),
    scope: _MonitoringScope = Depends(_gateway_monitoring_scope),
) -> list[dict[str, Any]]:
    """Request volume per time bucket (total count, not rate)."""
    _validate_route(route)
    consumer = _scope_consumer(scope, route, consumer)
    hs = _labels(route, consumer)
    try:
        return await _volume_series(
            lambda window: f"sum(increase(apisix_http_status{hs}[{window}]))",
            tw,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )


# ── LLM Metrics ────────────────────────────────────────────────────────────


@router.get("/metrics/llm/summary")
async def llm_metrics_summary(
    tw: TimeWindow = Depends(resolve_time_window),
    api_key: str | None = Query(None, description="Filter to one API key (LiteLLM end_user)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """LLM token usage summary: total tokens, cost, requests, latency."""
    sel = _llm_key_selector(api_key)
    try:
        (
            tokens,
            prompt,
            completion,
            spend,
            requests,
            latency_sum,
            latency_count,
            cached,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum(increase(litellm_total_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_input_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_output_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_spend_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_proxy_total_requests_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_request_total_latency_metric_sum{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_request_total_latency_metric_count{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum(increase(litellm_input_cached_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    latency_total = _extract_scalar(latency_sum)
    latency_cnt = _extract_scalar(latency_count)
    avg_latency = (latency_total / latency_cnt * 1000) if latency_cnt > 0 else 0.0

    prompt_val = _extract_scalar(prompt)
    cached_val = _extract_scalar(cached)

    return {
        "total_tokens": round(_extract_scalar(tokens)),
        "prompt_tokens": round(prompt_val),
        "completion_tokens": round(_extract_scalar(completion)),
        "estimated_cost": round(_extract_scalar(spend), 4),
        "total_requests": round(_extract_scalar(requests)),
        "avg_latency_ms": round(avg_latency, 2),
        "cached_tokens": round(cached_val),
        "cache_hit_rate": round(min(cached_val / prompt_val, 1.0), 4) if prompt_val > 0 else 0.0,
    }


@router.get("/metrics/llm/tokens")
async def llm_metrics_tokens(
    tw: TimeWindow = Depends(resolve_time_window),
    api_key: str | None = Query(None, description="Filter to one API key (LiteLLM end_user)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, list[dict[str, Any]]]:
    """Token usage trend: prompt and completion tokens over time."""
    sel = _llm_key_selector(api_key)
    try:
        prompt_points, completion_points, cached_points = await asyncio.gather(
            _volume_series(
                lambda window: f"sum(increase(litellm_input_tokens_metric_total{sel}[{window}]))",
                tw,
            ),
            _volume_series(
                lambda window: f"sum(increase(litellm_output_tokens_metric_total{sel}[{window}]))",
                tw,
            ),
            _volume_series(
                lambda window: f"sum(increase(litellm_input_cached_tokens_metric_total{sel}[{window}]))",
                tw,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

    return {
        "prompt": prompt_points,
        "completion": completion_points,
        "cached": cached_points,
    }


@router.get("/metrics/llm/by-model")
async def llm_metrics_by_model(
    tw: TimeWindow = Depends(resolve_time_window),
    api_key: str | None = Query(None, description="Filter to one API key (LiteLLM end_user)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Token usage, request count, and cost breakdown by model."""
    sel = _llm_key_selector(api_key)
    try:
        (
            token_results,
            input_token_results,
            output_token_results,
            cost_results,
            request_results,
            cached_token_results,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_total_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_input_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_output_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_spend_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_proxy_total_requests_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (requested_model, model) (increase(litellm_input_cached_tokens_metric_total{sel}[{tw.promql_window}]))",
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

    cached_map: dict[str, int] = {}
    for r in cached_token_results:
        model = _metric_label(r, "requested_model", "model")
        try:
            cached_map[model] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            cached_map[model] = 0

    models = []
    for model in (
        token_map.keys()
        | input_token_map.keys()
        | output_token_map.keys()
        | cost_map.keys()
        | request_map.keys()
        | cached_map.keys()
    ):
        tokens = token_map.get(model, 0)
        input_tokens = input_token_map.get(model, 0)
        output_tokens = output_token_map.get(model, 0)
        if tokens == 0 and (input_tokens > 0 or output_tokens > 0):
            tokens = input_tokens + output_tokens
        cost = cost_map.get(model, 0.0)
        requests = request_map.get(model, 0)
        cached_tokens = cached_map.get(model, 0)
        if (
            tokens > 0
            or input_tokens > 0
            or output_tokens > 0
            or cost > 0
            or requests > 0
            or cached_tokens > 0
        ):
            models.append(
                {
                    "model": model,
                    "tokens": tokens,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost": cost,
                    "requests": requests,
                    "cached_tokens": cached_tokens,
                }
            )
    models.sort(key=lambda x: (x["tokens"], x["requests"]), reverse=True)
    return models


@router.get("/metrics/llm/by-model-series")
async def llm_metrics_by_model_series(
    tw: TimeWindow = Depends(resolve_time_window),
    api_key: str | None = Query(None, description="Filter to one API key (LiteLLM end_user)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-model token usage bucketed over time (stacked-bar breakdown).

    Mirrors /metrics/llm/by-model: filters on ``end_user`` when scoped to one
    API key (matching the instant sibling exactly).
    """
    sel = _llm_key_selector(api_key)
    try:
        return await _grouped_volume_series(
            lambda window: (
                "sum by (requested_model, model) "
                f"(increase(litellm_total_tokens_metric_total{sel}[{window}]))"
            ),
            tw,
            ("requested_model", "model"),
            "tokens",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )


@router.get("/metrics/llm/top-keys")
async def llm_metrics_top_keys(
    tw: TimeWindow = Depends(resolve_time_window),
    api_key: str | None = Query(None, description="Filter to one API key (LiteLLM end_user)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """Top UniBridge API keys by token usage."""
    sel = _llm_key_selector(api_key)
    try:
        (
            token_results,
            input_token_results,
            output_token_results,
            req_results,
            cached_token_results,
        ) = await asyncio.gather(
            prometheus_client.instant_query(
                f"topk(10, sum by (end_user) (increase(litellm_total_tokens_metric_total{sel}[{tw.promql_window}])))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_input_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_output_tokens_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_proxy_total_requests_metric_total{sel}[{tw.promql_window}]))",
                eval_time=tw.eval_time,
            ),
            prometheus_client.instant_query(
                f"sum by (end_user) (increase(litellm_input_cached_tokens_metric_total{sel}[{tw.promql_window}]))",
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

    cached_map: dict[str, int] = {}
    for r in cached_token_results:
        key = _metric_label(r, "end_user")
        try:
            cached_map[key] = round(float(r["value"][1]))
        except (IndexError, ValueError, TypeError):
            cached_map[key] = 0

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
        cached_tokens = cached_map.get(key, 0)
        if tokens > 0 or input_tokens > 0 or output_tokens > 0 or requests > 0:
            keys.append(
                {
                    "api_key": key,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cached_tokens": cached_tokens,
                    "tokens": tokens,
                    "requests": requests,
                }
            )
    return keys


@router.get("/metrics/llm/top-keys-series")
async def llm_metrics_top_keys_series(
    tw: TimeWindow = Depends(resolve_time_window),
    api_key: str | None = Query(None, description="Filter to one API key (LiteLLM end_user)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> dict[str, Any]:
    """Per-API-key (end_user) token usage bucketed over time.

    Mirrors /metrics/llm/top-keys: filters on ``end_user`` when scoped to one
    API key (matching the instant sibling exactly).
    """
    sel = _llm_key_selector(api_key)
    try:
        return await _grouped_volume_series(
            lambda window: (
                "sum by (end_user) "
                f"(increase(litellm_total_tokens_metric_total{sel}[{window}]))"
            ),
            tw,
            ("end_user",),
            "tokens",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )


@router.get("/metrics/llm/status-codes")
async def llm_metrics_status_codes(
    tw: TimeWindow = Depends(resolve_time_window),
    api_key: str | None = Query(None, description="Filter to one API key (APISIX consumer)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """LLM HTTP status code distribution.

    Sourced from APISIX (``apisix_http_status``) rather than LiteLLM counters so
    every status code is broken out (200/400/429/500/…) and gateway-layer errors
    that never reach LiteLLM are still counted.
    """
    hs = _llm_labels(*_llm_consumer_extra(api_key))
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


@router.get("/metrics/llm/errors")
async def llm_metrics_errors(
    tw: TimeWindow = Depends(resolve_time_window),
    api_key: str | None = Query(None, description="Filter to one API key (APISIX consumer)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """LLM request success/error rate over time.

    Sourced from APISIX status codes (2xx/3xx = success, everything else = error)
    so gateway-layer failures (auth, rate-limit) are reflected, unlike the LiteLLM
    failed-request counter which only sees requests that reach the proxy. The error
    bucket is the complement of success (``code!~"2..|3.."``) so non-HTTP outcomes
    APISIX records — notably code 0 for client-aborted/timed-out streams — are not
    silently dropped.
    """
    consumer_extra = _llm_consumer_extra(api_key)
    hs_ok = _llm_labels('code=~"2..|3.."', *consumer_extra)
    hs_err = _llm_labels('code!~"2..|3.."', *consumer_extra)
    try:
        success_points, error_points = await asyncio.gather(
            _volume_series(
                lambda window: f"sum(increase(apisix_http_status{hs_ok}[{window}]))",
                tw,
            ),
            _volume_series(
                lambda window: f"sum(increase(apisix_http_status{hs_err}[{window}]))",
                tw,
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )

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
    api_key: str | None = Query(None, description="Filter to one API key (LiteLLM end_user)"),
    _admin: CurrentUser = Depends(require_permission("gateway.monitoring.read")),
) -> list[dict[str, Any]]:
    """LLM request volume per time bucket."""
    sel = _llm_key_selector(api_key)
    try:
        return await _volume_series(
            lambda window: f"sum(increase(litellm_proxy_total_requests_metric_total{sel}[{window}]))",
            tw,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Prometheus error: {exc}"
        )
