from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

APISIX_TIMEOUT = 10.0

# Only allow safe characters in resource types and IDs
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# Whitelist of valid APISIX admin resource types
_VALID_RESOURCE_TYPES = {"routes", "upstreams", "consumers", "services", "ssl", "global_rules", "plugins"}


def _validate_resource_type(resource: str) -> None:
    """Validate resource type against known APISIX admin resources."""
    if resource not in _VALID_RESOURCE_TYPES:
        raise ValueError(f"Invalid APISIX resource type: {resource!r}")


def _validate_resource_id(resource_id: str) -> None:
    """Validate resource ID to prevent path traversal."""
    if not _SAFE_ID_RE.fullmatch(resource_id):
        raise ValueError(f"Invalid resource ID: {resource_id!r}")


def _is_positive_weight(weight: Any) -> bool:
    return isinstance(weight, (int, float)) and not isinstance(weight, bool) and weight > 0


def upstream_node_addresses(nodes: Any) -> set[str]:
    """Normalize APISIX upstream ``nodes`` to a set of ``host:port`` strings.

    APISIX stores nodes either as ``{"host:port": weight}`` or as
    ``[{"host": ..., "port": ..., "weight": ...}]`` depending on how the
    upstream was written; zero/negative/malformed weights are dropped. This is
    the one shared normalizer — use it instead of assuming the dict form.
    """
    if isinstance(nodes, dict):
        return {str(addr) for addr, weight in nodes.items() if _is_positive_weight(weight)}
    if isinstance(nodes, list):
        addresses: set[str] = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if not _is_positive_weight(node.get("weight", 1)):
                continue
            host = node.get("host")
            if host is None:
                continue
            port = node.get("port")
            addresses.add(f"{host}:{port}" if port is not None else str(host))
        return addresses
    return set()


def route_paths(route: dict[str, Any]) -> list[str]:
    """Every path a route serves: its ``uri`` and, for a route with several, ``uris``."""
    raw_uris = route.get("uris")
    paths = [route.get("uri"), *(raw_uris if isinstance(raw_uris, list) else [])]
    return [path for path in paths if isinstance(path, str)]


# ── Probing an upstream's nodes the way APISIX reaches them ─────────────────


def upstream_scheme(upstream: dict[str, Any]) -> str:
    """The HTTP scheme to probe an upstream's nodes with: its own, or http."""
    scheme = upstream.get("scheme")
    return scheme if scheme in {"http", "https"} else "http"


def node_host(node_addr: str) -> str:
    """The host of an upstream node address (``host:port`` / ``[v6]:port``)."""
    host, _sep, _port = node_addr.rpartition(":")
    return host.strip("[]") if host else node_addr.strip("[]")


def upstream_host_header(upstream: dict[str, Any], node_addr: str) -> str:
    """The Host header APISIX sends a node, after the upstream's ``pass_host``."""
    pass_host = upstream.get("pass_host", "pass")
    if pass_host == "node":
        return node_host(node_addr)
    if pass_host == "rewrite" and isinstance(upstream.get("upstream_host"), str):
        return upstream["upstream_host"]
    return settings.HOST_IP


def split_host_port(address: str | None) -> tuple[str, int | None] | None:
    """``host:port`` / ``[v6]:port`` / ``host`` → (host, port or None)."""
    if not address:
        return None
    address = address.strip()
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif address.count(":") == 1:
        host, _, port = address.partition(":")
    else:
        host, port = address, ""
    if not host:
        return None
    return host, int(port) if port.isdigit() else None


# ── Admin API ───────────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """The admin-API client every call shares, so connections are reused."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=APISIX_TIMEOUT)
    return _client


async def aclose() -> None:
    """Close the shared client. Call on app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _headers() -> dict[str, str]:
    return {"X-API-KEY": settings.APISIX_ADMIN_KEY}


def _base_url() -> str:
    return settings.APISIX_ADMIN_URL.rstrip("/")


async def _request(
    method: str,
    resource: str,
    resource_id: str | None = None,
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send one admin-API request; a non-2xx answer raises ``httpx.HTTPStatusError``."""
    _validate_resource_type(resource)
    path = resource
    if resource_id is not None:
        _validate_resource_id(resource_id)
        path = f"{resource}/{resource_id}"
    send = getattr(_get_client(), method.lower())
    kwargs: dict[str, Any] = {"headers": _headers()}
    if body is not None:
        kwargs["json"] = body
    resp = await send(f"{_base_url()}/apisix/admin/{path}", **kwargs)
    resp.raise_for_status()
    return resp


def _value(resp: httpx.Response) -> dict[str, Any]:
    data = resp.json()
    return data.get("value", data)


async def list_resources(resource: str) -> dict[str, Any]:
    """List APISIX resources (routes, upstreams, etc.).

    Returns {"items": [...], "total": N} with flattened values.
    """
    data = (await _request("GET", resource)).json()
    raw_list = data.get("list") or []
    items = [entry["value"] for entry in raw_list if "value" in entry]
    logger.debug("APISIX list %s: %d items", resource, len(items))
    return {"items": items, "total": data.get("total", len(items))}


async def get_resource(resource: str, resource_id: str) -> dict[str, Any]:
    """Get a single APISIX resource by ID."""
    return _value(await _request("GET", resource, resource_id))


def is_not_found(exc: BaseException) -> bool:
    """Whether a failed admin-API call means "this resource does not exist".

    A status error answers by its status alone, so another failure is never
    taken for a missing resource; any other error by its message.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 404
    message = str(exc).lower()
    return "404" in message or "not found" in message


async def get_resource_or_none(resource: str, resource_id: str) -> dict[str, Any] | None:
    """``get_resource``, or None when the resource does not exist; other failures raise."""
    try:
        return await get_resource(resource, resource_id)
    except Exception as exc:
        if is_not_found(exc):
            return None
        raise


async def put_resource(resource: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Create or update an APISIX resource via PUT."""
    resp = await _request("PUT", resource, resource_id, body)
    value = _value(resp)
    logger.info("APISIX PUT %s/%s: status=%d", resource, resource_id, resp.status_code)
    return value


async def patch_resource(resource: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Partially update an APISIX resource via PATCH.

    APISIX merges the given fields into the existing resource, so this preserves
    everything not named in ``body`` (e.g. consumer-restriction whitelists).
    """
    resp = await _request("PATCH", resource, resource_id, body)
    value = _value(resp)
    logger.info("APISIX PATCH %s/%s: status=%d", resource, resource_id, resp.status_code)
    return value


async def delete_resource(resource: str, resource_id: str) -> None:
    """Delete an APISIX resource."""
    resp = await _request("DELETE", resource, resource_id)
    logger.info("APISIX DELETE %s/%s: status=%d", resource, resource_id, resp.status_code)
