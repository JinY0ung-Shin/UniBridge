"""Route-level tests for the converter.

Verifies that ``POST /v1/messages`` translates the Anthropic request to an
OpenAI chat-completions body, targets ``{LITELLM_URL}/v1/chat/completions``,
forwards the APISIX-injected ``Authorization`` header, and translates the
response (streaming SSE and one-shot JSON) back to the Anthropic shape.
"""

from __future__ import annotations

import json
from typing import Iterable

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as converter_main


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LITELLM_URL", "http://upstream.test")
    monkeypatch.setenv("CONVERTER_TLS_VERIFY", "false")
    yield


def _make_app(handler):
    transport = httpx.MockTransport(handler)

    def _factory(timeout):  # noqa: ARG001
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    converter_main._make_client = _factory  # type: ignore[assignment]
    return converter_main.app


def _openai_sse(chunks: Iterable[dict]) -> bytes:
    parts = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


def _parse_sse(raw: str) -> list[dict]:
    events: list[dict] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].lstrip()))
    return events


def test_health():
    client = TestClient(_make_app(lambda r: httpx.Response(200)))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_streaming_translates_openai_to_anthropic():
    captured = {}

    chunks = [
        {"choices": [{"delta": {"reasoning_content": "Let me think."}}]},
        {"choices": [{"delta": {"content": "Hello!"}}]},
        {
            "choices": [
                {"delta": {"tool_calls": [
                    {"index": 0, "id": "call_1",
                     "function": {"name": "get_weather", "arguments": "{\"city\""}}
                ]}}
            ]
        },
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ":\"SF\"}"}}
        ]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    ]
    body = _openai_sse(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    client = TestClient(_make_app(handler))
    resp = client.post(
        "/v1/messages",
        headers={"Authorization": "Bearer master-key"},
        json={
            "model": "GLM-4.6",
            "stream": True,
            "max_tokens": 100,
            "system": "be terse",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    # Targeted the OpenAI chat route and forwarded the injected master key.
    assert captured["url"] == "http://upstream.test/v1/chat/completions"
    assert captured["auth"] == "Bearer master-key"
    # Request was translated to OpenAI shape (system → system message, tools nested).
    assert captured["body"]["messages"][0] == {"role": "system", "content": "be terse"}
    assert captured["body"]["tools"][0]["type"] == "function"

    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"

    starts = [e for e in events if e["type"] == "content_block_start"]
    kinds = [s["content_block"]["type"] for s in starts]
    assert "thinking" in kinds
    assert "text" in kinds
    assert "tool_use" in kinds

    tool_start = next(s for s in starts if s["content_block"]["type"] == "tool_use")
    assert tool_start["content_block"]["name"] == "get_weather"

    delta = next(e for e in events if e["type"] == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use"


def test_non_streaming_translates_openai_to_anthropic():
    openai_resp = {
        "id": "chatcmpl-1",
        "model": "GLM-4.6",
        "choices": [{"message": {"content": "Hi there"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200, headers={"content-type": "application/json"},
            content=json.dumps(openai_resp).encode(),
        )

    client = TestClient(_make_app(handler))
    resp = client.post(
        "/v1/messages",
        json={"model": "GLM-4.6", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "message"
    assert data["role"] == "assistant"
    assert data["content"] == [{"type": "text", "text": "Hi there"}]
    assert data["stop_reason"] == "end_turn"
    assert data["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_messages_route_forwards_anthropic_image_as_openai_image_url():
    captured = {}
    openai_resp = {
        "id": "chatcmpl-1",
        "model": "GLM-4.6",
        "choices": [{"message": {"content": "image seen"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(openai_resp).encode(),
        )

    client = TestClient(_make_app(handler))
    resp = client.post(
        "/v1/messages",
        json={
            "model": "GLM-4.6",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                }
            ],
        },
    )

    assert resp.status_code == 200
    assert captured["body"]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        }
    ]


def test_upstream_error_forwarded_verbatim():
    err = {"error": {"message": "rate limited", "type": "rate_limit_error"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"content-type": "application/json"},
            content=json.dumps(err).encode(),
        )

    client = TestClient(_make_app(handler))
    resp = client.post(
        "/v1/messages",
        json={"model": "GLM-4.6", "messages": [{"role": "user", "content": "hi"}]},
    )
    # Non-2xx upstream is passed through unchanged (not translated).
    assert resp.status_code == 429
    assert resp.json() == err


def test_streaming_bridge_error_emits_terminal_error_event():
    # A malformed chunk arriving mid-stream — after message_start has already
    # reached the client — must terminate with an Anthropic ``error`` event,
    # not propagate and leave the client hanging on a truncated message.
    chunks = [
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": {"0": {}}},  # choices as a dict → bridge raises on choices[0]
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_openai_sse(chunks)
        )

    client = TestClient(_make_app(handler))
    resp = client.post(
        "/v1/messages",
        json={
            "model": "m",
            "stream": True,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "message_start"
    assert types[-1] == "error"
    assert events[-1]["error"]["type"] == "api_error"


def test_streaming_upstream_error_chunk_becomes_error_event():
    # Upstream streams a normal chunk then an error object; the client must see
    # a terminal Anthropic ``error`` event, not an empty successful stream.
    body = _openai_sse(
        [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"error": {"type": "rate_limit_error", "message": "slow down"}},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    client = TestClient(_make_app(handler))
    resp = client.post(
        "/v1/messages",
        json={
            "model": "m",
            "stream": True,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["type"] == "rate_limit_error"
    assert not any(e["type"] == "message_stop" for e in events)


def test_invalid_json_body_returns_400():
    client = TestClient(_make_app(lambda r: httpx.Response(200)))
    resp = client.post(
        "/v1/messages",
        headers={"content-type": "application/json"},
        content=b"{not json",
    )
    assert resp.status_code == 400
