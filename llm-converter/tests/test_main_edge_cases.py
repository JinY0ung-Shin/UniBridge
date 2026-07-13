"""Route failure, cleanup, persistence, and opt-in tracing boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as converter_main
from app.responses_state import conversation_store


REAL_MAKE_CLIENT = converter_main._make_client


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    monkeypatch.setenv("LITELLM_URL", "http://upstream.test")
    monkeypatch.delenv("CONVERTER_TLS_CA", raising=False)
    monkeypatch.setenv("CONVERTER_TLS_VERIFY", "false")
    monkeypatch.setenv("CONVERTER_TRACE", "false")
    conversation_store.clear()
    yield
    conversation_store.clear()


def _install_factory(monkeypatch, handler) -> list[httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    clients: list[httpx.AsyncClient] = []

    def factory(timeout):
        client = httpx.AsyncClient(transport=transport, timeout=timeout)
        clients.append(client)
        return client

    monkeypatch.setattr(converter_main, "_make_client", factory)
    return clients


def _openai_sse(chunks: Iterable[dict]) -> bytes:
    frames = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode()


def _parse_sse(raw: str) -> list[dict]:
    return [
        json.loads(line[5:].lstrip())
        for line in raw.splitlines()
        if line.startswith("data:")
    ]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/messages", {"model": "m", "stream": True, "messages": []}),
        ("/v1/responses", {"model": "m", "stream": True, "input": "hi"}),
    ],
)
def test_streaming_non_sse_error_is_forwarded_and_resources_close(
    monkeypatch, path, body
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/problem+json", "x-upstream": "yes"},
            content=b'{"error":"limited"}',
        )

    clients = _install_factory(monkeypatch, handler)
    response = TestClient(converter_main.app).post(path, json=body)

    assert response.status_code == 429
    assert response.json() == {"error": "limited"}
    assert response.headers["x-upstream"] == "yes"
    assert clients[0].is_closed


class _SlowStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.sleep(1)
        yield b'{"late":true}'

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/messages", {"model": "m", "stream": True, "messages": []}),
        ("/v1/responses", {"model": "m", "stream": True, "input": "hi"}),
    ],
)
def test_streaming_non_sse_body_read_timeout_returns_504_and_closes(
    monkeypatch, path, body
):
    stream = _SlowStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            headers={"content-type": "application/json"},
            stream=stream,
        )

    monkeypatch.setattr(converter_main, "_ERROR_BODY_READ_TIMEOUT", 0.001)
    clients = _install_factory(monkeypatch, handler)
    response = TestClient(converter_main.app).post(path, json=body)

    assert response.status_code == 504
    assert response.json()["error"]["type"] == "timeout"
    assert stream.closed is True
    assert clients[0].is_closed


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/messages", {"model": "m", "stream": True, "messages": []}),
        ("/v1/responses", {"model": "m", "stream": True, "input": "hi"}),
    ],
)
def test_streaming_connect_failure_closes_client(monkeypatch, path, body):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot connect", request=request)

    clients = _install_factory(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError, match="cannot connect"):
        TestClient(converter_main.app).post(path, json=body)
    assert clients[0].is_closed


def test_responses_nonstream_timeout_returns_504_and_closes(monkeypatch):
    monkeypatch.setattr(
        type(converter_main.settings),
        "nonstream_timeout",
        property(lambda self: 0.001),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"choices": []})

    clients = _install_factory(monkeypatch, handler)
    response = TestClient(converter_main.app).post(
        "/v1/responses", json={"model": "m", "input": "hi"}
    )

    assert response.status_code == 504
    assert response.json()["error"]["type"] == "timeout"
    assert clients[0].is_closed


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/messages", {"model": "m", "messages": []}),
        ("/v1/responses", {"model": "m", "input": "hi"}),
    ],
)
def test_nonstream_transport_failure_still_closes_client(monkeypatch, path, body):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("upstream reset", request=request)

    clients = _install_factory(monkeypatch, handler)
    with pytest.raises(httpx.ReadError, match="upstream reset"):
        TestClient(converter_main.app).post(path, json=body)
    assert clients[0].is_closed


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/messages", {"model": "m", "messages": []}),
        ("/v1/responses", {"model": "m", "input": "hi"}),
    ],
)
def test_nonstream_invalid_json_from_successful_upstream_is_forwarded(
    monkeypatch, path, body
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{broken"
        )

    _install_factory(monkeypatch, handler)
    response = TestClient(converter_main.app).post(path, json=body)
    assert response.status_code == 200
    assert response.content == b"{broken"


def test_messages_structurally_invalid_success_body_is_forwarded(monkeypatch):
    raw = {"choices": {"not": "a list"}, "id": "odd"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, json=raw)

    _install_factory(monkeypatch, handler)
    response = TestClient(converter_main.app).post(
        "/v1/messages", json={"model": "m", "messages": []}
    )
    assert response.status_code == 200
    assert response.json() == raw


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/responses"])
def test_request_body_must_be_a_json_object(monkeypatch, path):
    _install_factory(monkeypatch, lambda request: httpx.Response(200))
    response = TestClient(converter_main.app).post(
        path, content=b"[]", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert "JSON object" in response.json()["error"]["message"]


def test_responses_invalid_json_returns_400(monkeypatch):
    _install_factory(monkeypatch, lambda request: httpx.Response(200))
    response = TestClient(converter_main.app).post(
        "/v1/responses",
        content=b"{bad",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert "not valid JSON" in response.json()["error"]["message"]


def test_streaming_completion_supersedes_parent_before_terminal_event(monkeypatch):
    parent = [{"role": "user", "content": "old"}]
    conversation_store.put("resp_parent", parent)
    monkeypatch.setattr(converter_main, "new_response_id", lambda: "resp_child")
    body = _openai_sse(
        [
            {"choices": [{"delta": {"content": "answer"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    _install_factory(monkeypatch, handler)
    response = TestClient(converter_main.app).post(
        "/v1/responses",
        json={
            "model": "m",
            "stream": True,
            "input": "new",
            "previous_response_id": "resp_parent",
        },
    )

    assert response.status_code == 200
    assert _parse_sse(response.text)[-1]["type"] == "response.completed"
    assert conversation_store.get("resp_parent") is None
    assert conversation_store.get("resp_child")[-1]["content"] == "answer"


def test_streaming_fallback_persists_complete_holder_and_supersedes_parent(monkeypatch):
    conversation_store.put("resp_parent", [{"role": "user", "content": "old"}])
    monkeypatch.setattr(converter_main, "new_response_id", lambda: "resp_fallback")

    async def fake_bridge(chunks, *, response_id, request_body, holder, emit_reasoning):
        holder["assistant_message"] = {"role": "assistant", "content": "partial"}
        yield {"type": "response.in_progress", "sequence_number": 0}

    monkeypatch.setattr(converter_main, "chat_stream_to_responses_events", fake_bridge)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: [DONE]\n\n",
        )

    _install_factory(monkeypatch, handler)
    response = TestClient(converter_main.app).post(
        "/v1/responses",
        json={
            "model": "m",
            "stream": True,
            "input": "new",
            "previous_response_id": "resp_parent",
        },
    )

    assert response.status_code == 200
    assert conversation_store.get("resp_parent") is None
    assert conversation_store.get("resp_fallback")[-1]["content"] == "partial"


async def _as_async(items) -> AsyncIterator[dict]:
    for item in items:
        yield item


async def test_real_client_factory_uses_runtime_tls_setting(monkeypatch):
    expected_verify = object()
    captured = {}

    class DummyClient:
        is_closed = False

        async def aclose(self):
            self.is_closed = True

    def fake_async_client(**kwargs):
        captured.update(kwargs)
        return DummyClient()

    monkeypatch.setattr(
        type(converter_main.settings),
        "tls_verify",
        property(lambda self: expected_verify),
    )
    monkeypatch.setattr(converter_main.httpx, "AsyncClient", fake_async_client)

    client = REAL_MAKE_CLIENT(timeout=None)
    assert captured == {"timeout": None, "verify": expected_verify}
    await client.aclose()
    assert client.is_closed


def test_trace_helpers_validate_json_and_summarize_tool_calls(monkeypatch):
    monkeypatch.setattr(converter_main, "_TRACE_ARGS_MAX", 3)
    assert converter_main._is_json(None) is False
    assert converter_main._is_json("not json") is False
    assert converter_main._is_json('{"ok": true}') is True
    assert converter_main._summarize_tool_calls("bad") is None
    assert converter_main._summarize_tool_calls([None]) is None
    assert converter_main._summarize_tool_calls(
        [
            {
                "index": 2,
                "id": "call_2",
                "function": {"name": "tool", "arguments": "abcdef"},
            },
            {"function": "invalid"},
        ]
    ) == [
        {"index": 2, "id": "call_2", "name": "tool", "args_len": 6, "args": "abc"},
        {"index": None, "id": None, "name": None, "args_len": 0, "args": None},
    ]


def test_incoming_trace_logs_shapes_caps_body_and_fails_soft(monkeypatch, caplog):
    monkeypatch.setenv("CONVERTER_TRACE", "true")
    monkeypatch.setattr(converter_main, "_TRACE_BODY_MAX", 30)
    caplog.set_level(logging.INFO, logger="app.main")

    converter_main._trace_incoming_messages_request(
        {
            "model": "m",
            "system": "system prompt",
            "tools": [{"name": "search"}, None],
            "messages": [{"role": "user", "content": "x" * 100}],
        }
    )
    converter_main._trace_incoming_messages_request({"system": [{"type": "text"}]})
    converter_main._trace_incoming_messages_request({"system": None})
    converter_main._trace_incoming_messages_request({"system": 123})

    class Broken(dict):
        def get(self, key, default=None):
            raise RuntimeError("inspect failed")

    converter_main._trace_incoming_messages_request(Broken())

    assert "tool_names=['search']" in caplog.text
    assert "chars]" in caplog.text
    assert "request inspect failed" in caplog.text


async def test_upstream_trace_is_passthrough_and_logs_decisive_shapes(caplog):
    caplog.set_level(logging.INFO, logger="app.main")
    broken = object()
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "content": "answer",
                        "reasoning_content": "thought",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "tool", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"error": {"message": "upstream failed"}},
        broken,
    ]
    result = [
        item
        async for item in converter_main._trace_upstream_chunks(
            _as_async(chunks), "model-a"
        )
    ]
    assert result == chunks

    no_tool = [
        {
            "choices": [
                {
                    "delta": {
                        "content": "<tool_call> as text",
                        "reasoning_content": "plain reasoning",
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    ]
    assert [
        item
        async for item in converter_main._trace_upstream_chunks(
            _as_async(no_tool), "model-b"
        )
    ] == no_tool
    assert "ERROR chunk" in caplog.text
    assert "upstream chunk inspect failed" in caplog.text
    assert "markers=['<tool_call>', 'tool_call']" in caplog.text


async def test_downstream_trace_reconstructs_tool_arguments_and_fails_soft(caplog):
    caplog.set_level(logging.INFO, logger="app.main")
    events = [
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "tu_1", "name": "tool"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"a":1}'},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "error", "error": {"type": "api_error"}},
        {"type": "content_block_start", "content_block": "invalid"},
    ]
    result = [
        event
        async for event in converter_main._trace_downstream_events(
            _as_async(events), "model"
        )
    ]
    assert result == events
    assert "args_valid_json=True" in caplog.text
    assert "stop_reason=tool_use" in caplog.text
    assert "ERROR event" in caplog.text
    assert "downstream event inspect failed" in caplog.text


def test_messages_route_enables_both_trace_wrappers(monkeypatch, caplog):
    monkeypatch.setenv("CONVERTER_TRACE", "true")
    caplog.set_level(logging.INFO, logger="app.main")
    body = _openai_sse(
        [
            {"choices": [{"delta": {"content": "answer"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    _install_factory(monkeypatch, handler)
    response = TestClient(converter_main.app).post(
        "/v1/messages",
        json={"model": "trace-model", "stream": True, "messages": []},
    )

    assert response.status_code == 200
    assert _parse_sse(response.text)[-1]["type"] == "message_stop"
    assert "upstream[trace-model] END" in caplog.text
    assert "downstream[trace-model]: message_delta" in caplog.text
