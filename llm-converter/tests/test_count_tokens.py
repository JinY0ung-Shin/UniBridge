"""``POST /v1/messages/count_tokens``: a local, deliberately high estimate.

No gateway can count tokens for a self-hosted model, so the converter answers
from the request alone — never calling upstream — in both gateway modes.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as converter_main
from app import token_estimate
from app.token_estimate import (
    DOCUMENT_BLOCK_TOKENS,
    IMAGE_BLOCK_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    TOOL_OVERHEAD_TOKENS,
    InvalidCountRequest,
    estimate_input_tokens,
    text_tokens,
)


@pytest.fixture(autouse=True)
def _no_upstream(monkeypatch):
    """Any upstream call is a failure: the count is answered locally."""
    monkeypatch.setenv("LITELLM_URL", "http://upstream.test")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"count_tokens called upstream: {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        converter_main,
        "_make_client",
        lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )


def _count(body: object) -> httpx.Response:
    return TestClient(converter_main.app).post("/v1/messages/count_tokens", json=body)


def _user(content: object) -> dict:
    return {"messages": [{"role": "user", "content": content}]}


# --- The heuristic ------------------------------------------------------------------
def test_ascii_counts_about_three_characters_per_token():
    """Not BPE's English-prose ~4: code, JSON schemas and identifiers split finer,
    and 4 measured 0.77-0.87 of Qwen3's real count — the estimate must err high."""
    assert text_tokens("") == 0
    assert text_tokens("abc") == 1
    assert text_tokens("abcd") == 2  # rounds up
    assert text_tokens("x" * 300) == 100
    assert text_tokens("x" * 400) == 134


def test_non_ascii_counts_a_token_per_character():
    assert text_tokens("안녕하세요") == 5
    assert text_tokens("こんにちは") == 5
    # Mixed text adds both parts: 8 ASCII chars -> ceil(8/3) = 3, 2 Hangul -> 2.
    assert text_tokens("hello 세계!!") == 3 + 2


def test_korean_costs_more_than_the_same_number_of_ascii_characters():
    assert text_tokens("가" * 100) > text_tokens("a" * 100)


def test_system_messages_and_tools_all_count():
    body = {
        "system": "abc" * 10,
        "messages": [{"role": "user", "content": "abc" * 5}],
        "tools": [{"name": "abc", "description": "abc" * 2, "input_schema": {}}],
    }
    expected = (
        10
        + MESSAGE_OVERHEAD_TOKENS + 5
        + TOOL_OVERHEAD_TOKENS + 1 + 2 + text_tokens("{}")
    )
    assert estimate_input_tokens(body) == expected


def test_block_types():
    content = [
        {"type": "text", "text": "abc"},
        {"type": "thinking", "thinking": "abcabc", "signature": "x" * 4000},
        {"type": "redacted_thinking", "data": "y" * 4000},
        {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"p": "a"}},
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "abc"}]},
        {"type": "tool_result", "tool_use_id": "toolu_2"},  # no content: nothing to count
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 100_000}},
        {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "abc" * 3}},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "B" * 99}},
    ]
    expected = (
        MESSAGE_OVERHEAD_TOKENS
        + 1  # text
        + 2  # thinking text; the signature is not prompt text
        + 0  # redacted thinking
        + text_tokens("read") + text_tokens('{"p":"a"}')
        + 1  # tool_result text
        + 0  # tool_result without content
        + IMAGE_BLOCK_TOKENS  # base64 never counted as text
        + 3  # text document
        + DOCUMENT_BLOCK_TOKENS
    )
    assert estimate_input_tokens(_user(content)) == expected


def test_system_blocks_and_server_tools():
    body = {
        "system": [{"type": "text", "text": "abc"}, {"type": "text", "text": "abc"}],
        "messages": [],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
    }
    server_tool_json = token_estimate._json_tokens(body["tools"][0])
    assert estimate_input_tokens(body) == 2 + TOOL_OVERHEAD_TOKENS + server_tool_json


def test_estimate_is_never_zero():
    assert estimate_input_tokens({"messages": []}) == 1


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ({"model": "m"}, "messages"),
        ({"messages": "hi"}, "messages"),
        ({"messages": ["hi"]}, "messages.0"),
        ({"messages": [{"content": "hi"}]}, "messages.0.role"),
        ({"messages": [{"role": "user"}]}, "messages.0.content"),
        (_user(42), "messages.0.content"),
        (_user([{"type": "text", "text": 5}]), "messages.0.content.0.text"),
        (_user([{"text": "no type"}]), "messages.0.content.0.type"),
        (_user(["a bare string block"]), "messages.0.content.0"),
        (_user([{"type": "tool_use", "name": "read", "input": "not an object"}]), "messages.0.content.0.input"),
        (_user([{"type": "tool_result", "tool_use_id": "t", "content": 7}]), "messages.0.content.0.content"),
        (
            _user([{"type": "document", "source": {"type": "text", "data": 3}}]),
            "messages.0.content.0.source.data",
        ),
        ({"system": 5, "messages": []}, "system"),
        ({"system": [{"type": "text", "text": None}], "messages": []}, "system.0.text"),
        ({"messages": [], "tools": {"name": "read"}}, "tools"),
        ({"messages": [], "tools": ["read"]}, "tools.0"),
        ({"messages": [], "tools": [{"name": "read", "input_schema": "{}"}]}, "tools.0.input_schema"),
        ({"messages": [], "tools": [{"name": 1, "input_schema": {}}]}, "tools.0.name"),
    ],
)
def test_wrongly_typed_requests_are_rejected_with_where(body, where):
    with pytest.raises(InvalidCountRequest) as exc:
        estimate_input_tokens(body)
    assert str(exc.value).startswith(f"{where}:"), str(exc.value)


# --- The route ----------------------------------------------------------------------
@pytest.mark.parametrize("gateway", ["litellm", "bifrost"])
def test_route_answers_locally_in_both_modes(monkeypatch, gateway):
    monkeypatch.setenv("LLM_GATEWAY", gateway)

    resp = _count({"model": "qwen3.5-32b", **_user("abc" * 25)})

    assert resp.status_code == 200
    assert resp.json() == {"input_tokens": MESSAGE_OVERHEAD_TOKENS + 25}


def test_route_accepts_the_alias_prefixed_model():
    resp = _count({"model": "claude/qwen3.5-32b", **_user("hi")})

    assert resp.status_code == 200
    assert resp.json()["input_tokens"] > 0


@pytest.mark.parametrize(
    "body",
    [
        {"model": "qwen3.5-32b"},  # no messages
        {"model": "qwen3.5-32b", "messages": "hi"},  # not an array
        ["not", "an", "object"],
        # Wrong types deep inside answer 400 too, never 500.
        {"messages": ["hi"]},
        _user(42),
        {"messages": [], "tools": {"name": "read"}},
        _user([{"type": "text", "text": {"nested": True}}]),
    ],
)
def test_route_rejects_a_malformed_request_with_a_400(body):
    resp = _count(body)

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_route_rejects_invalid_json():
    resp = TestClient(converter_main.app).post(
        "/v1/messages/count_tokens",
        content=b"{ not json",
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"
