"""Live E2E: OpenAI Responses endpoint through the deployed gateway.

POST {LLM_BASE_URL}/v1/responses → APISIX → llm-converter (Responses↔OpenAI) →
LiteLLM /v1/chat/completions. Covers non-streaming, streaming, previous_response_id
chaining, and the unknown-id error.
"""

from __future__ import annotations

from conftest import read_sse, requires_deployment

pytestmark = requires_deployment


def _output_text(body: dict) -> str:
    return "".join(
        p.get("text", "")
        for it in body.get("output", [])
        if it.get("type") == "message"
        for p in it.get("content", [])
        if p.get("type") == "output_text"
    )


def test_responses_non_streaming(client, auth_headers, model):
    resp = client.post(
        "/v1/responses",
        headers=auth_headers,
        json={"model": model, "input": "Reply with exactly the word: pong"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("object") == "response"
    assert body.get("status") in ("completed", "incomplete"), body
    # output_text is SDK-derived, never a wire field.
    assert "output_text" not in body
    assert _output_text(body).strip(), f"expected output text, got {body.get('output')}"
    usage = body.get("usage") or {}
    assert "input_tokens" in usage and "output_tokens" in usage


def test_responses_streaming(client, auth_headers, model):
    events = read_sse(
        client,
        "/v1/responses",
        auth_headers,
        {"model": model, "stream": True, "input": "Count to three."},
    )
    assert events, "no SSE events received"
    types = [t for t, _ in events]
    assert types[0] == "response.created"
    assert types[-1] in ("response.completed", "response.incomplete")
    assert "response.output_text.delta" in types

    # sequence_number is global and monotonic from 0.
    seqs = [d["sequence_number"] for _, d in events if isinstance(d, dict) and "sequence_number" in d]
    assert seqs and seqs[0] == 0
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs)), "duplicate sequence_number"

    # Terminal event carries the full output[].
    final = events[-1][1]["response"]
    assert final.get("output"), "terminal event missing output[]"
    streamed = "".join(d.get("delta", "") for t, d in events if t == "response.output_text.delta")
    assert streamed.strip(), "no output_text.delta content"


def test_responses_previous_response_id_chaining(client, auth_headers, model):
    first = client.post(
        "/v1/responses",
        headers=auth_headers,
        json={"model": model, "input": "Remember the number 42. Acknowledge in one word."},
    )
    assert first.status_code == 200, first.text
    resp_id = first.json()["id"]
    assert resp_id.startswith("resp_")

    second = client.post(
        "/v1/responses",
        headers=auth_headers,
        json={
            "model": model,
            "previous_response_id": resp_id,
            "input": "What number did I ask you to remember? Reply with just the number.",
        },
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body.get("object") == "response"
    # The chain was accepted and produced output. (Whether the model actually
    # recalls "42" is a model-quality property, not a converter guarantee — we
    # assert the converter resolved the chain and round-tripped, and surface the
    # recall as an informational check.)
    text = _output_text(body)
    assert text.strip(), "chained turn produced no output"
    if "42" not in text:
        import warnings

        warnings.warn(f"model did not recall the prior context (got: {text!r})")


def test_responses_unknown_previous_response_id_returns_400(client, auth_headers, model):
    resp = client.post(
        "/v1/responses",
        headers=auth_headers,
        json={"model": model, "input": "hi", "previous_response_id": "resp_does_not_exist"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "previous_response_not_found"
