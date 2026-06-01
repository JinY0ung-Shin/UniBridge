"""Live E2E: Anthropic Messages endpoint through the deployed gateway.

POST {LLM_BASE_URL}/v1/messages → APISIX (key-auth + master-key inject) →
llm-converter (Anthropic→OpenAI) → LiteLLM /v1/chat/completions → back to
Anthropic shape.
"""

from __future__ import annotations

from conftest import MAX_TOKENS, read_sse, requires_deployment

pytestmark = requires_deployment


def test_messages_non_streaming(client, auth_headers, model):
    resp = client.post(
        "/v1/messages",
        headers=auth_headers,
        json={
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": "Reply with exactly the word: pong"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("type") == "message"
    assert body.get("role") == "assistant"
    assert isinstance(body.get("content"), list) and body["content"], body
    text = "".join(b.get("text", "") for b in body["content"] if b.get("type") == "text")
    assert text.strip(), f"expected non-empty text, got {body['content']}"
    assert "stop_reason" in body
    usage = body.get("usage") or {}
    assert "input_tokens" in usage and "output_tokens" in usage


def test_messages_streaming(client, auth_headers, model):
    events = read_sse(
        client,
        "/v1/messages",
        auth_headers,
        {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "stream": True,
            "messages": [{"role": "user", "content": "Count to three."}],
        },
    )
    assert events, "no SSE events received"
    types = [t for t, _ in events]
    # Structurally valid Anthropic Messages stream.
    assert types[0] == "message_start"
    assert "content_block_start" in types
    assert "content_block_delta" in types
    assert types[-1] == "message_stop"
    # Some real text streamed through.
    text = "".join(
        d.get("delta", {}).get("text", "")
        for t, d in events
        if t == "content_block_delta" and d.get("delta", {}).get("type") == "text_delta"
    )
    assert text.strip(), "no text_delta content in stream"


def test_messages_requires_api_key(client):
    """APISIX key-auth must reject an unauthenticated call before it reaches the converter."""
    resp = client.post(
        "/v1/messages",
        headers={"content-type": "application/json"},  # no apikey
        json={"model": "x", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code in (401, 403), resp.text
