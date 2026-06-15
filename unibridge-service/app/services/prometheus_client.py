from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

PROM_TIMEOUT = 10.0

# Shared client so every monitoring query reuses pooled keep-alive connections
# instead of paying a fresh TCP (and TLS) handshake per query. A dashboard load
# fans out dozens of queries, so per-call client creation dominated latency.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=PROM_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
        )
    return _client


async def aclose() -> None:
    """Close the shared client. Call on app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _base_url() -> str:
    return settings.PROMETHEUS_URL.rstrip("/")


async def instant_query(query: str, eval_time: float | None = None) -> list[dict[str, Any]]:
    """Execute a Prometheus instant query. Returns list of result items.

    eval_time (epoch seconds) sets the evaluation timestamp; when omitted
    Prometheus evaluates at server "now".
    """
    url = f"{_base_url()}/api/v1/query"
    params: dict[str, str] = {"query": query}
    if eval_time is not None:
        params["time"] = str(eval_time)
    resp = await _get_client().get(url, params=params)
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
    start: float | None = None,
    end: float | None = None,
) -> list[dict[str, Any]]:
    """Execute a Prometheus range query.

    When start and end (epoch seconds) are both given they are used directly;
    otherwise the window is duration ending at now.
    """
    url = f"{_base_url()}/api/v1/query_range"
    if start is not None and end is not None:
        start_ts, end_ts = float(start), float(end)
    else:
        end_ts = time.time()
        start_ts = end_ts - _parse_duration(duration)

    resp = await _get_client().get(url, params={
        "query": query,
        "start": str(start_ts),
        "end": str(end_ts),
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
