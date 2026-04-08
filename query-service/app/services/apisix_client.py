from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

APISIX_TIMEOUT = 10.0


def _headers() -> dict[str, str]:
    return {"X-API-KEY": settings.APISIX_ADMIN_KEY}


def _base_url() -> str:
    return settings.APISIX_ADMIN_URL.rstrip("/")


async def list_resources(resource: str) -> dict[str, Any]:
    """List APISIX resources (routes, upstreams, etc.).

    Returns {"items": [...], "total": N} with flattened values.
    """
    url = f"{_base_url()}/apisix/admin/{resource}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    raw_list = data.get("list") or []
    items = [entry["value"] for entry in raw_list if "value" in entry]
    return {"items": items, "total": data.get("total", len(items))}


async def get_resource(resource: str, resource_id: str) -> dict[str, Any]:
    """Get a single APISIX resource by ID."""
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    return data.get("value", data)


async def put_resource(resource: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Create or update an APISIX resource via PUT."""
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.put(url, json=body, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    return data.get("value", data)


async def delete_resource(resource: str, resource_id: str) -> None:
    """Delete an APISIX resource."""
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.delete(url, headers=_headers())
        resp.raise_for_status()
