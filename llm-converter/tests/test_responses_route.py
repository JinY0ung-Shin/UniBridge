"""Route-level tests for POST /v1/responses."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as converter_main
from app.responses_state import conversation_store


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LITELLM_URL", "http://upstream.test")
    monkeypatch.setenv("CONVERTER_TLS_VERIFY", "false")
    conversation_store.clear()
    yield
    conversation_store.clear()


def _make_app(handler):
    transport = httpx.MockTransport(handler)

    def _factory(timeout):  # noqa: ARG001
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    converter_main._make_client = _factory  # type: ignore[assignment]
    return converter_main.app


def _chat_json(content="Hi there", tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1741569952, "model": "m",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].lstrip()))
    return events


def test_non_streaming_translates_to_responses_object():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(_chat_json()).encode())

    client = TestClient(_make_app(handler))
    resp = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer master-key"},
        json={"model": "m", "instructions": "be terse", "input": "hi"},
    )
    assert resp.status_code == 200
    assert captured["url"] == "http://upstream.test/v1/chat/completions"
    assert captured["auth"] == "Bearer master-key"
    # request was translated: instructions -> system message
    assert captured["body"]["messages"][0] == {"role": "system", "content": "be terse"}

    data = resp.json()
    assert data["object"] == "response"
    assert data["status"] == "completed"
    assert data["output"][0]["content"][0]["text"] == "Hi there"
    assert data["id"].startswith("resp_")


def test_previous_response_id_chaining_prepends_history():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        # First reply has content "first answer"; second is "second answer".
        n = len(bodies)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(_chat_json(content=f"answer {n}")).encode())

    client = TestClient(_make_app(handler))

    r1 = client.post("/v1/responses", json={"model": "m", "instructions": "sys", "input": "q1"})
    rid = r1.json()["id"]
    assert len(bodies[0]["messages"]) == 2  # system + user

    r2 = client.post("/v1/responses",
                     json={"model": "m", "input": "q2", "previous_response_id": rid})
    assert r2.status_code == 200
    # Second upstream call must carry the full prior transcript + new input.
    msgs = bodies[1]["messages"]
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[1] == {"role": "user", "content": "q1"}
    assert msgs[2] == {"role": "assistant", "content": "answer 1"}
    assert msgs[3] == {"role": "user", "content": "q2"}


def test_unknown_previous_response_id_returns_400():
    client = TestClient(_make_app(lambda r: httpx.Response(200)))
    resp = client.post("/v1/responses",
                       json={"model": "m", "input": "hi", "previous_response_id": "resp_nope"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "previous_response_not_found"


def test_empty_previous_response_id_returns_400():
    client = TestClient(_make_app(lambda r: httpx.Response(200)))
    resp = client.post("/v1/responses",
                       json={"model": "m", "input": "hi", "previous_response_id": ""})
    assert resp.status_code == 400


def test_store_false_is_not_persisted():
    client = TestClient(_make_app(
        lambda r: httpx.Response(200, headers={"content-type": "application/json"},
                                 content=json.dumps(_chat_json()).encode())))
    r1 = client.post("/v1/responses", json={"model": "m", "input": "q1", "store": False})
    rid = r1.json()["id"]
    # Chaining off a non-stored response must fail.
    r2 = client.post("/v1/responses",
                     json={"model": "m", "input": "q2", "previous_response_id": rid})
    assert r2.status_code == 400


def test_streaming_emits_responses_events():
    sse = (
        "data: " + json.dumps({"choices": [{"delta": {"content": "Hello"}}]}) + "\n\n"
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}) + "\n\n"
        "data: [DONE]\n\n"
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    client = TestClient(_make_app(handler))
    resp = client.post("/v1/responses", json={"model": "m", "stream": True, "input": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "response.output_text.delta" in types
    final = events[-1]["response"]
    assert final["output"][0]["content"][0]["text"] == "Hello"
