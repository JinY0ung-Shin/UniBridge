"""``GET /v1/models`` against Bifrost, and the listing cache in both modes.

Bifrost lists ``<provider>/<id>`` per provider key and adds ``key_statuses`` /
``extra_fields`` (key ids, failing backends' raw errors, routing details) at the
top level; the converter rebuilds the listing from ``data`` under the ids
clients call. Bifrost also asks every provider key live on each listing with no
deadline, so the converter caches the listing, shares one fetch among
concurrent callers, and falls back to the last good listing when upstream is
slow or failing.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as converter_main

BIFROST_LISTING = {
    "data": [
        {
            "id": "vllm-qwen/qwen3.5-32b",
            "created": 1_700_000_000,
            "owned_by": "vllm",
            "alias": "Qwen/Qwen3.5-32B",
            "context_length": 32768,
        },
        {"id": "smg/Qwen/Qwen3-8B"},
        # The same public name on a second provider: listed once.
        {"id": "sglang-a/qwen3.5-32b"},
    ],
    "extra_fields": {"routing_info": {"provider": "vllm-qwen", "key": "prod-key-a"}},
    "key_statuses": [
        {"key_id": "c6e46715-0000-0000-0000-000000000000", "status": "success", "provider": "vllm-qwen"},
        {
            "key_id": "8f311230-0000-0000-0000-000000000000",
            "status": "list_models_failed",
            "provider": "sglang-b",
            "error": {"status_code": 502, "error": {"error": "lookup sglang-b.internal on 127.0.0.11:53: no such host"}},
        },
    ],
}


def _json(status: int, body: object, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json", **(headers or {})},
        content=json.dumps(body).encode("utf-8"),
    )


def _litellm_listing(*ids: str) -> httpx.Response:
    return _json(200, {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]})


def _ids(resp: httpx.Response) -> list[str]:
    return [entry["id"] for entry in resp.json()["data"]]


@pytest.fixture
def upstream(monkeypatch):
    """A mock upstream answering from a list of handlers, one per call (the last
    repeats); records every request it receives."""
    calls: list[httpx.Request] = []
    plan: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        step = plan[min(len(calls), len(plan)) - 1]
        return step(request) if callable(step) else step

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        converter_main,
        "_make_client",
        lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )

    def install(*steps):
        plan[:] = steps
        return calls

    return install


@pytest.fixture
def clock(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(converter_main, "_clock", lambda: now[0])
    return now


@pytest.fixture
def bifrost_mode(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY", "bifrost")
    monkeypatch.setenv("CONVERTER_BIFROST_URL", "http://bifrost.test")
    monkeypatch.delenv("LITELLM_URL", raising=False)


@pytest.fixture
def litellm_mode(monkeypatch):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test")
    monkeypatch.setenv("CONVERTER_TLS_VERIFY", "false")


def _get(path: str = "/v1/models") -> httpx.Response:
    return TestClient(converter_main.app).get(path)


# --- Bifrost listing -------------------------------------------------------------
@pytest.mark.usefixtures("bifrost_mode")
def test_bifrost_ids_lose_the_provider_segment_and_duplicates(upstream):
    calls = upstream(_json(200, BIFROST_LISTING, {"x-bifrost-provider": "vllm-qwen"}))

    resp = _get()

    assert resp.status_code == 200
    assert str(calls[0].url) == "http://bifrost.test/v1/models"
    assert _ids(resp) == [
        "qwen3.5-32b",
        "claude/qwen3.5-32b",
        "Qwen/Qwen3-8B",
        "claude/Qwen/Qwen3-8B",
    ]
    assert resp.json()["first_id"] == "qwen3.5-32b"
    assert resp.json()["last_id"] == "claude/Qwen/Qwen3-8B"


@pytest.mark.usefixtures("bifrost_mode")
def test_bifrost_key_details_never_reach_the_client(upstream):
    upstream(_json(200, BIFROST_LISTING, {"x-bifrost-routing-info-key": "prod-key-a"}))

    resp = _get()

    body = resp.json()
    assert set(body) == {"object", "data", "has_more", "first_id", "last_id"}
    for leaked in ("key_statuses", "extra_fields", "c6e46715", "sglang-b.internal", "prod-key-a"):
        assert leaked not in resp.text, leaked
    assert not [name for name in resp.headers if name.lower().startswith("x-bifrost-")]


@pytest.mark.usefixtures("bifrost_mode")
def test_bifrost_entry_fields_are_kept_and_owner_defaults_to_the_gateway(upstream):
    upstream(_json(200, BIFROST_LISTING))

    entries = {entry["id"]: entry for entry in _get().json()["data"]}

    first = entries["qwen3.5-32b"]
    assert first["owned_by"] == "vllm"  # upstream wins
    assert first["alias"] == "Qwen/Qwen3.5-32B"
    assert first["context_length"] == 32768
    assert first["display_name"] == "qwen3.5-32b"
    assert entries["Qwen/Qwen3-8B"]["owned_by"] == "bifrost"


@pytest.mark.usefixtures("bifrost_mode")
def test_bifrost_error_body_loses_its_key_details_but_keeps_its_status(upstream):
    upstream(
        _json(
            500,
            {
                "is_bifrost_error": False,
                "error": {"message": "all providers failed"},
                "extra_fields": {"key_statuses": [{"key_id": "c6e46715"}]},
            },
        )
    )

    resp = _get()

    assert resp.status_code == 500
    assert resp.json() == {"is_bifrost_error": False, "error": {"message": "all providers failed"}}


@pytest.mark.usefixtures("litellm_mode")
def test_litellm_listing_is_rebuilt_from_data_with_the_litellm_owner(upstream):
    upstream(
        _json(200, {"object": "list", "data": [{"id": "qwen3.5-32b"}], "key_statuses": [{"key_id": "x"}]})
    )

    body = _get().json()

    assert "key_statuses" not in body
    assert [entry["id"] for entry in body["data"]] == ["qwen3.5-32b", "claude/qwen3.5-32b"]
    assert body["data"][0]["owned_by"] == "litellm"


@pytest.mark.usefixtures("litellm_mode")
def test_litellm_ids_with_a_slash_are_left_alone(upstream):
    upstream(_litellm_listing("hosted_vllm/qwen3.5-32b"))

    assert _ids(_get())[0] == "hosted_vllm/qwen3.5-32b"


# --- The shared fetch carries the gateway credential and nothing else --------------
CALLER_HEADERS = {
    "Authorization": "Bearer sk-litellm-master",
    "x-litellm-end-user-id": "team-a-key",
    "x-litellm-api-key": "sk-a-narrower-litellm-key",
    "x-bf-vk": "sk-bf-gatewaykey00000000000",
    "x-bf-dim-consumer": "team-a-key",
    "x-bf-lh-consumer": "team-a-key",
    "anthropic-version": "2023-06-01",
    "user-agent": "claude-cli/2.1.300",
}


@pytest.mark.parametrize(
    ("gateway", "credential"),
    [("litellm", "authorization"), ("bifrost", "x-bf-vk")],
)
def test_listing_fetch_sends_only_the_gateway_credential(upstream, monkeypatch, gateway, credential):
    monkeypatch.setenv("LLM_GATEWAY", gateway)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test")
    monkeypatch.setenv("CONVERTER_BIFROST_URL", "http://bifrost.test")
    calls = upstream(_json(200, {"data": [{"id": "p/a"}]}))

    TestClient(converter_main.app).get("/v1/models", headers=CALLER_HEADERS)

    sent = {name.lower(): value for name, value in calls[0].headers.items()}
    expected = {name.lower(): value for name, value in CALLER_HEADERS.items()}[credential]
    assert sent[credential] == expected
    for caller_only in (set(name.lower() for name in CALLER_HEADERS) - {credential}) - {"user-agent"}:
        assert caller_only not in sent, caller_only
    assert sent["user-agent"] != "claude-cli/2.1.300"  # httpx's own, not the caller's


APISIX_ONLY = {"Authorization": "Bearer sk-litellm-master"}


@pytest.mark.usefixtures("litellm_mode")
def test_one_callers_litellm_key_cannot_shape_the_shared_listing(upstream):
    """LiteLLM prefers ``x-litellm-api-key`` to ``Authorization``: forwarded, a
    narrower key in the first caller's request would give every joined caller
    its listing or its 401."""

    def litellm(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-litellm-api-key"):
            return _json(401, {"error": {"message": "key not allowed to list models"}})
        return _litellm_listing("a")

    upstream(litellm)
    client = TestClient(converter_main.app)

    first = client.get("/v1/models", headers={"x-litellm-api-key": "sk-narrow", **APISIX_ONLY})
    second = client.get("/v1/models", headers=APISIX_ONLY)

    assert first.status_code == 200
    assert _ids(first) == _ids(second) == ["a", "claude/a"]


# --- Cache ------------------------------------------------------------------------
@pytest.mark.usefixtures("litellm_mode")
def test_listing_is_served_from_cache_until_the_ttl_runs_out(upstream, clock):
    calls = upstream(_litellm_listing("a"), _litellm_listing("b"))

    assert _ids(_get())[0] == "a"
    clock[0] += 29
    assert _ids(_get())[0] == "a"
    assert len(calls) == 1

    clock[0] += 2  # 31s after the fetch
    assert _ids(_get())[0] == "b"
    assert len(calls) == 2


@pytest.mark.usefixtures("litellm_mode")
def test_zero_ttl_asks_upstream_every_time(upstream, monkeypatch):
    monkeypatch.setenv("CONVERTER_MODELS_CACHE_TTL", "0")
    calls = upstream(_litellm_listing("a"), _litellm_listing("b"))

    assert _ids(_get())[0] == "a"
    assert _ids(_get())[0] == "b"
    assert len(calls) == 2


@pytest.mark.usefixtures("litellm_mode")
def test_the_alias_prefix_is_applied_per_request_over_the_cache(upstream, monkeypatch):
    calls = upstream(_litellm_listing("a"))

    assert _ids(_get()) == ["a", "claude/a"]
    monkeypatch.setenv("CONVERTER_MODEL_ALIAS_PREFIX", "anthropic/")
    assert _ids(_get()) == ["a", "anthropic/a"]
    assert len(calls) == 1


def test_switching_the_gateway_does_not_serve_the_other_gateways_listing(upstream, monkeypatch):
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test")
    calls = upstream(_litellm_listing("from-litellm"), _json(200, {"data": [{"id": "p/from-bifrost"}]}))
    assert _ids(_get())[0] == "from-litellm"

    monkeypatch.setenv("LLM_GATEWAY", "bifrost")
    monkeypatch.setenv("CONVERTER_BIFROST_URL", "http://bifrost.test")
    assert _ids(_get())[0] == "from-bifrost"
    assert str(calls[1].url) == "http://bifrost.test/v1/models"


# --- Stale fallback -----------------------------------------------------------------
def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.usefixtures("litellm_mode")
@pytest.mark.parametrize(
    "failure",
    [
        _json(503, {"error": {"message": "all backends down"}}),
        _unreachable,
    ],
    ids=["5xx", "unreachable"],
)
def test_a_failing_upstream_is_answered_with_the_last_listing(upstream, clock, failure):
    upstream(_litellm_listing("a"), failure)
    assert _ids(_get())[0] == "a"

    clock[0] += 300  # past the TTL, inside the 600s stale window
    resp = _get()

    assert resp.status_code == 200
    assert _ids(resp)[0] == "a"


@pytest.mark.usefixtures("litellm_mode")
def test_a_listing_older_than_the_stale_window_is_not_served(upstream, clock):
    upstream(_litellm_listing("a"), _json(503, {"error": {"message": "down"}}))
    _get()

    clock[0] += 601
    resp = _get()

    assert resp.status_code == 503
    assert resp.json() == {"error": {"message": "down"}}


@pytest.mark.usefixtures("litellm_mode")
def test_stale_fallback_can_be_switched_off(upstream, clock, monkeypatch):
    monkeypatch.setenv("CONVERTER_MODELS_STALE_MAX", "0")
    upstream(_litellm_listing("a"), _json(503, {"error": {"message": "down"}}))
    _get()

    clock[0] += 31
    assert _get().status_code == 503


@pytest.mark.usefixtures("litellm_mode")
def test_a_client_error_is_not_masked_by_the_last_listing(upstream, clock):
    upstream(_litellm_listing("a"), _json(401, {"error": {"message": "bad key"}}))
    _get()

    clock[0] += 31
    resp = _get()

    assert resp.status_code == 401
    assert resp.json() == {"error": {"message": "bad key"}}


@pytest.mark.usefixtures("litellm_mode")
def test_unreachable_upstream_without_a_listing_is_a_502(upstream):
    upstream(_unreachable)

    resp = _get()

    assert resp.status_code == 502
    assert resp.json()["type"] == "error"
    assert resp.json()["error"]["type"] == "api_error"


# --- One fetch for concurrent callers, and callers that stop waiting ----------------
@pytest.fixture
def slow_upstream(monkeypatch):
    """A mock upstream whose listing takes ``delay`` seconds (async, same loop)."""
    state = {"calls": 0, "delay": 0.0, "gate": None, "ids": ["a"]}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["gate"] is not None:
            await state["gate"].wait()
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        return _litellm_listing(*state["ids"])

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        converter_main,
        "_make_client",
        lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    return state


def _converter_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=converter_main.app), base_url="http://converter"
    )


@pytest.mark.usefixtures("litellm_mode")
async def test_concurrent_misses_share_one_upstream_fetch(slow_upstream):
    slow_upstream["gate"] = asyncio.Event()
    async with _converter_client() as client:
        pending = asyncio.gather(*(client.get("/v1/models") for _ in range(5)))
        await asyncio.sleep(0.05)
        slow_upstream["gate"].set()
        responses = await pending

    assert [r.status_code for r in responses] == [200] * 5
    assert {tuple(_ids(r)) for r in responses} == {("a", "claude/a")}
    assert slow_upstream["calls"] == 1


@pytest.mark.usefixtures("litellm_mode")
async def test_a_caller_stops_waiting_but_the_fetch_fills_the_cache(slow_upstream, monkeypatch):
    monkeypatch.setenv("CONVERTER_MODELS_TIMEOUT", "0.05")
    slow_upstream["delay"] = 0.3
    async with _converter_client() as client:
        first = await client.get("/v1/models")
        assert first.status_code == 504
        assert first.json()["error"]["type"] == "timeout"

        await asyncio.sleep(0.4)  # the shielded fetch finishes in the background
        second = await client.get("/v1/models")

    assert second.status_code == 200
    assert _ids(second) == ["a", "claude/a"]
    assert slow_upstream["calls"] == 1


@pytest.mark.usefixtures("litellm_mode")
async def test_a_slow_refresh_answers_with_the_last_listing(slow_upstream, monkeypatch, clock):
    async with _converter_client() as client:
        assert _ids(await client.get("/v1/models"))[0] == "a"

        clock[0] += 31
        monkeypatch.setenv("CONVERTER_MODELS_TIMEOUT", "0.05")
        slow_upstream["delay"] = 0.3
        slow_upstream["ids"] = ["b"]
        stale = await client.get("/v1/models")
        assert stale.status_code == 200
        assert _ids(stale)[0] == "a"

        await asyncio.sleep(0.4)
        fresh = await client.get("/v1/models")

    assert _ids(fresh)[0] == "b"
    assert slow_upstream["calls"] == 2
