from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

PROM_TIMEOUT = 10.0


def _base_url() -> str:
    return settings.PROMETHEUS_URL.rstrip("/")


async def instant_query(query: str) -> list[dict[str, Any]]:
    """Execute a Prometheus instant query. Returns list of result items."""
    url = f"{_base_url()}/api/v1/query"
    async with httpx.AsyncClient(timeout=PROM_TIMEOUT) as client:
        resp = await client.get(url, params={"query": query})
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != "success":
        logger.warning("Prometheus query failed: %s", data)
        return []
    return data.get("data", {}).get("result", [])


async def range_query(
    query: str,
    duration: str = "1h",
    step: str = "60s",
) -> list[dict[str, Any]]:
    """Execute a Prometheus range query over the given duration ending at now."""
    url = f"{_base_url()}/api/v1/query_range"
    end = time.time()
    duration_seconds = _parse_duration(duration)
    start = end - duration_seconds

    async with httpx.AsyncClient(timeout=PROM_TIMEOUT) as client:
        resp = await client.get(url, params={
            "query": query,
            "start": str(start),
            "end": str(end),
            "step": step,
        })
        resp.raise_for_status()
        data = resp.json()
    if data.get("status") != "success":
        logger.warning("Prometheus range query failed: %s", data)
        return []
    return data.get("data", {}).get("result", [])


def _parse_duration(d: str) -> int:
    """Parse duration string like '15m', '1h', '6h', '24h' to seconds."""
    d = d.strip()
    if d.endswith("m"):
        return int(d[:-1]) * 60
    if d.endswith("h"):
        return int(d[:-1]) * 3600
    if d.endswith("d"):
        return int(d[:-1]) * 86400
    return int(d)
