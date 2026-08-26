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
    if not _SAFE_ID_RE.match(resource_id):
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


def _headers() -> dict[str, str]:
    return {"X-API-KEY": settings.APISIX_ADMIN_KEY}


def _base_url() -> str:
    return settings.APISIX_ADMIN_URL.rstrip("/")


async def list_resources(resource: str) -> dict[str, Any]:
    """List APISIX resources (routes, upstreams, etc.).

    Returns {"items": [...], "total": N} with flattened values.
    """
    _validate_resource_type(resource)
    url = f"{_base_url()}/apisix/admin/{resource}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    raw_list = data.get("list") or []
    items = [entry["value"] for entry in raw_list if "value" in entry]
    logger.debug("APISIX list %s: %d items", resource, len(items))
    return {"items": items, "total": data.get("total", len(items))}


async def get_resource(resource: str, resource_id: str) -> dict[str, Any]:
    """Get a single APISIX resource by ID."""
    _validate_resource_type(resource)
    _validate_resource_id(resource_id)
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    return data.get("value", data)


async def put_resource(resource: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Create or update an APISIX resource via PUT."""
    _validate_resource_type(resource)
    _validate_resource_id(resource_id)
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.put(url, json=body, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    logger.info("APISIX PUT %s/%s: status=%d", resource, resource_id, resp.status_code)
    return data.get("value", data)


async def patch_resource(resource: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Partially update an APISIX resource via PATCH.

    APISIX merges the given fields into the existing resource, so this preserves
    everything not named in ``body`` (e.g. consumer-restriction whitelists).
    """
    _validate_resource_type(resource)
    _validate_resource_id(resource_id)
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.patch(url, json=body, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    logger.info("APISIX PATCH %s/%s: status=%d", resource, resource_id, resp.status_code)
    return data.get("value", data)


async def delete_resource(resource: str, resource_id: str) -> None:
    """Delete an APISIX resource."""
    _validate_resource_type(resource)
    _validate_resource_id(resource_id)
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.delete(url, headers=_headers())
        resp.raise_for_status()
    logger.info("APISIX DELETE %s/%s: status=%d", resource, resource_id, resp.status_code)
