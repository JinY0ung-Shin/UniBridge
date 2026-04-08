from __future__ import annotations

import logging
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from httpx import HTTPStatusError

from app.auth import CurrentUser, require_admin
from app.services import apisix_client

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


def _inject_service_key(body: dict[str, Any], existing_plugins: dict[str, Any] | None = None) -> dict[str, Any]:
    service_key = body.pop("service_key", None)
    plugins = dict(existing_plugins or {})

    if service_key and service_key.get("header_name") and service_key.get("header_value"):
        plugins["proxy-rewrite"] = {
            "headers": {
                "set": {
                    service_key["header_name"]: service_key["header_value"]
                }
            }
        }

    if plugins:
        body["plugins"] = plugins
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

    body = _inject_service_key(body, existing_plugins)

    try:
        result = await apisix_client.put_resource("routes", route_id, body)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to connect to APISIX: {exc}")
    result["service_key"] = _extract_service_key(result)
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
