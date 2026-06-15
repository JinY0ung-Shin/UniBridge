from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import prometheus_client


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _fake_client(captured: dict):
    """Return a stub for the shared client whose .get records params."""
    async def fake_get(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp({"status": "success", "data": {"result": [{"value": [1, "5"]}]}})

    return AsyncMock(get=AsyncMock(side_effect=fake_get))


@pytest.mark.asyncio
async def test_instant_query_omits_time_by_default():
    captured: dict = {}
    with patch("app.services.prometheus_client._get_client", return_value=_fake_client(captured)):
        await prometheus_client.instant_query("up")
    assert captured["params"] == {"query": "up"}


@pytest.mark.asyncio
async def test_instant_query_includes_eval_time():
    captured: dict = {}
    with patch("app.services.prometheus_client._get_client", return_value=_fake_client(captured)):
        await prometheus_client.instant_query("up", eval_time=1717000000.0)
    assert captured["params"] == {"query": "up", "time": "1717000000.0"}


@pytest.mark.asyncio
async def test_range_query_uses_explicit_start_end():
    captured: dict = {}
    with patch("app.services.prometheus_client._get_client", return_value=_fake_client(captured)):
        await prometheus_client.range_query("up", step="60s", start=100.0, end=700.0)
    assert captured["params"]["start"] == "100.0"
    assert captured["params"]["end"] == "700.0"
    assert captured["params"]["step"] == "60s"


@pytest.mark.asyncio
async def test_range_query_falls_back_to_duration_when_no_start_end():
    captured: dict = {}
    with patch("app.services.prometheus_client._get_client", return_value=_fake_client(captured)):
        await prometheus_client.range_query("up", duration="1h", step="60s")
    start = float(captured["params"]["start"])
    end = float(captured["params"]["end"])
    assert round(end - start) == 3600


@pytest.mark.asyncio
async def test_get_client_reuses_singleton():
    await prometheus_client.aclose()
    try:
        c1 = prometheus_client._get_client()
        c2 = prometheus_client._get_client()
        assert c1 is c2
        assert not c1.is_closed
    finally:
        await prometheus_client.aclose()
        assert prometheus_client._client is None
