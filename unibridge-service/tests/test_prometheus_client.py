from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
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


async def test_instant_query_omits_time_by_default():
    captured: dict = {}
    with patch("app.services.prometheus_client._get_client", return_value=_fake_client(captured)):
        await prometheus_client.instant_query("up")
    assert captured["params"] == {"query": "up"}


async def test_instant_query_includes_eval_time():
    captured: dict = {}
    with patch("app.services.prometheus_client._get_client", return_value=_fake_client(captured)):
        await prometheus_client.instant_query("up", eval_time=1717000000.0)
    assert captured["params"] == {"query": "up", "time": "1717000000.0"}


async def test_range_query_uses_explicit_start_end():
    captured: dict = {}
    with patch("app.services.prometheus_client._get_client", return_value=_fake_client(captured)):
        await prometheus_client.range_query("up", step="60s", start=100.0, end=700.0)
    assert captured["params"]["start"] == "100.0"
    assert captured["params"]["end"] == "700.0"
    assert captured["params"]["step"] == "60s"


async def test_range_query_falls_back_to_duration_when_no_start_end():
    captured: dict = {}
    with patch("app.services.prometheus_client._get_client", return_value=_fake_client(captured)):
        await prometheus_client.range_query("up", duration="1h", step="60s")
    start = float(captured["params"]["start"])
    end = float(captured["params"]["end"])
    assert round(end - start) == 3600


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


# ── Responses, through a real httpx client on a mock transport ─────────────


def _transport_client(requests: list[httpx.Request], *, status: int = 200, payload=None):
    """A real AsyncClient that answers every request locally (never the network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=payload if payload is not None else {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_instant_query_returns_the_result_list_from_the_query_endpoint(monkeypatch):
    monkeypatch.setattr(prometheus_client.settings, "PROMETHEUS_URL", "http://prom:9090/")
    requests: list[httpx.Request] = []
    result_items = [{"metric": {"__name__": "up"}, "value": [1234567890, "1"]}]
    client = _transport_client(
        requests, payload={"status": "success", "data": {"result": result_items}}
    )
    with patch("app.services.prometheus_client._get_client", return_value=client):
        result = await prometheus_client.instant_query("rate(x[5m])")

    assert result == result_items
    # The trailing slash of PROMETHEUS_URL is not doubled.
    assert str(requests[0].url.copy_with(query=None)) == "http://prom:9090/api/v1/query"
    assert requests[0].url.params["query"] == "rate(x[5m])"


async def test_range_query_uses_the_range_endpoint_and_defaults_to_1h_at_60s():
    requests: list[httpx.Request] = []
    client = _transport_client(requests, payload={"status": "success", "data": {"result": []}})
    with patch("app.services.prometheus_client._get_client", return_value=client):
        assert await prometheus_client.range_query("up") == []

    params = requests[0].url.params
    assert requests[0].url.path == "/api/v1/query_range"
    assert params["query"] == "up"
    assert params["step"] == "60s"
    assert round(float(params["end"]) - float(params["start"])) == 3600


@pytest.mark.parametrize("query_fn", [prometheus_client.instant_query, prometheus_client.range_query])
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "error", "errorType": "bad_data", "error": "parse error"},
        {"status": "success"},
    ],
)
async def test_failed_or_empty_answers_give_no_results(query_fn, payload):
    client = _transport_client([], payload=payload)
    with patch("app.services.prometheus_client._get_client", return_value=client):
        assert await query_fn("up") == []


@pytest.mark.parametrize("query_fn", [prometheus_client.instant_query, prometheus_client.range_query])
async def test_http_errors_propagate(query_fn):
    client = _transport_client([], status=502, payload={"error": "bad gateway"})
    with patch("app.services.prometheus_client._get_client", return_value=client):
        with pytest.raises(httpx.HTTPStatusError):
            await query_fn("up")
