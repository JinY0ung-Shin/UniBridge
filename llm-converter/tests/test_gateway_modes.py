"""``LLM_GATEWAY``: what each mode sends upstream and hands back to the client.

APISIX sets BOTH gateways' credentials on the converter routes (a deploy
re-points the routes before the color promotion flips the converter's
upstream), so the converter alone decides which reach the gateway: LiteLLM gets
its master key and ``x-litellm-*`` but no ``x-bf-*``; Bifrost gets the three
``x-bf-*`` headers APISIX sets and nothing else that could steer it.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as converter_main
from app.sse import forward_request_headers, forward_response_headers

# What APISIX sets on llm-messages / llm-responses / llm-models in every mode.
APISIX_HEADERS = {
    "Authorization": "Bearer sk-litellm-master",
    "x-litellm-end-user-id": "team-a-key",
    "x-bf-vk": "sk-bf-gatewaykey00000000000",
    "x-bf-dim-consumer": "team-a-key",
    "x-bf-lh-consumer": "team-a-key",
}
# What a client could add of its own.
CLIENT_HEADERS = {
    "x-bf-eh-x-forwarded-for": "1.2.3.4",
    "x-bf-passthrough-extra-params": "true",
    "x-bf-api-key": "a-stored-provider-key",
    "x-bf-send-back-raw-response": "true",
    "x-litellm-tags": "client-tag",
    "x-api-key": "client-unibridge-key",
    "anthropic-version": "2023-06-01",
    "user-agent": "claude-cli/2.1.300",
    "accept-encoding": "gzip",
}


def _lower(headers: dict) -> dict:
    return {key.lower(): value for key, value in headers.items()}


# --- forward_request_headers / forward_response_headers ----------------------
def test_litellm_mode_forwards_its_credentials_and_no_bifrost_header():
    out = _lower(forward_request_headers({**APISIX_HEADERS, **CLIENT_HEADERS}.items(), "litellm"))

    assert out["authorization"] == "Bearer sk-litellm-master"
    assert out["x-litellm-end-user-id"] == "team-a-key"
    assert out["x-litellm-tags"] == "client-tag"
    assert out["x-api-key"] == "client-unibridge-key"
    assert out["anthropic-version"] == "2023-06-01"
    assert out["user-agent"] == "claude-cli/2.1.300"
    # Not even the virtual key APISIX set: it must not land in LiteLLM's logs.
    assert not [name for name in out if name.startswith("x-bf-")]
    assert "accept-encoding" not in out


def test_bifrost_mode_forwards_only_the_three_gateway_headers():
    out = _lower(forward_request_headers({**APISIX_HEADERS, **CLIENT_HEADERS}.items(), "bifrost"))

    assert out["x-bf-vk"] == "sk-bf-gatewaykey00000000000"
    assert out["x-bf-dim-consumer"] == "team-a-key"
    assert out["x-bf-lh-consumer"] == "team-a-key"
    assert sorted(name for name in out if name.startswith("x-bf-")) == [
        "x-bf-dim-consumer",
        "x-bf-lh-consumer",
        "x-bf-vk",
    ]
    for dropped in ("authorization", "x-litellm-end-user-id", "x-litellm-tags", "x-api-key"):
        assert dropped not in out, dropped
    assert out["anthropic-version"] == "2023-06-01"
    assert out["user-agent"] == "claude-cli/2.1.300"
    assert "accept-encoding" not in out


@pytest.mark.parametrize("name", ["X-BF-VK", "X-Bf-Dim-Consumer"])
def test_bifrost_gateway_headers_match_case_insensitively(name):
    assert name in forward_request_headers([(name, "v")], "bifrost")
    assert name not in forward_request_headers([(name, "v")], "litellm")


@pytest.mark.parametrize("name", ["api-key", "x-goog-api-key", "AUTHORIZATION", "X-LiteLLM-End-User-Id"])
def test_bifrost_mode_drops_credentials_bifrost_would_read(name):
    assert forward_request_headers([(name, "sk-bf-sneaky0000000000")], "bifrost") == {}


def test_response_drops_bifrost_routing_headers_in_every_mode():
    upstream = {
        "x-bifrost-provider": "vllm-qwen",
        "X-Bifrost-Routing-Info-Key": "prod-key-a",
        "x-bifrost-upstream-latency-ms": "12",
        "content-encoding": "gzip",
        "x-request-id": "req-1",
    }
    assert forward_response_headers(upstream.items()) == {"x-request-id": "req-1"}


# --- Routes -------------------------------------------------------------------
@pytest.fixture
def capture(monkeypatch):
    """Point the converter at a mock upstream that records what it receives."""
    seen: dict = {}

    def install(response: httpx.Response):
        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content) if request.content else None
            return response

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            converter_main,
            "_make_client",
            lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
        )
        return seen

    return install


def _chat_json(headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", **(headers or {})},
        content=json.dumps(
            {
                "id": "chatcmpl-1",
                "model": "qwen3.5-32b",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        ).encode("utf-8"),
    )


def _chat_sse(headers: dict | None = None) -> httpx.Response:
    chunk = {
        "id": "chatcmpl-1",
        "model": "qwen3.5-32b",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    }
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", **(headers or {})},
        content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8"),
    )


MESSAGES_BODY = {
    "model": "claude/qwen3.5-32b",
    "max_tokens": 64,
    "output_config": {"effort": "max"},
    "messages": [{"role": "user", "content": "hi"}],
}
RESPONSES_BODY = {
    "model": "qwen3.5-32b",
    "input": "hi",
    "reasoning": {"effort": "xhigh"},
    "store": False,
}


@pytest.fixture
def bifrost_mode(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY", "bifrost")
    monkeypatch.setenv("CONVERTER_BIFROST_URL", "http://bifrost.test")
    # The converter in front of Bifrost needs no LiteLLM address at all.
    monkeypatch.delenv("LITELLM_URL", raising=False)


@pytest.fixture
def litellm_mode(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY", "litellm")
    monkeypatch.setenv("LITELLM_URL", "https://litellm.test")
    monkeypatch.setenv("CONVERTER_TLS_VERIFY", "false")


def _post(path: str, body: dict) -> httpx.Response:
    return TestClient(converter_main.app).post(
        path, json=body, headers={**APISIX_HEADERS, **CLIENT_HEADERS}
    )


@pytest.mark.usefixtures("bifrost_mode")
def test_messages_go_to_bifrost_with_only_its_headers_and_no_litellm_field(capture):
    seen = capture(_chat_json({"x-bifrost-routing-info-key": "prod-key-a"}))

    resp = _post("/v1/messages", MESSAGES_BODY)

    assert resp.status_code == 200, resp.content
    assert seen["url"] == "http://bifrost.test/v1/chat/completions"
    sent = _lower(seen["headers"])
    assert sent["x-bf-vk"] == APISIX_HEADERS["x-bf-vk"]
    assert "authorization" not in sent
    assert "x-litellm-end-user-id" not in sent
    assert "x-bf-eh-x-forwarded-for" not in sent
    assert "x-bf-passthrough-extra-params" not in sent
    # Effort still clamped (``max`` -> ``high``) and sent, without LiteLLM's hatch.
    assert seen["body"]["reasoning_effort"] == "high"
    assert "allowed_openai_params" not in seen["body"]
    assert seen["body"]["model"] == "qwen3.5-32b"
    assert "x-bifrost-routing-info-key" not in resp.headers


@pytest.mark.usefixtures("litellm_mode")
def test_messages_go_to_litellm_with_its_credentials_and_the_hatch(capture):
    seen = capture(_chat_json())

    resp = _post("/v1/messages", MESSAGES_BODY)

    assert resp.status_code == 200, resp.content
    assert seen["url"] == "https://litellm.test/v1/chat/completions"
    sent = _lower(seen["headers"])
    assert sent["authorization"] == "Bearer sk-litellm-master"
    assert sent["x-litellm-end-user-id"] == "team-a-key"
    assert not [name for name in sent if name.startswith("x-bf-")]
    assert seen["body"]["reasoning_effort"] == "high"
    assert seen["body"]["allowed_openai_params"] == ["reasoning_effort"]


@pytest.mark.usefixtures("bifrost_mode")
def test_responses_go_to_bifrost_without_the_litellm_field(capture):
    seen = capture(_chat_json({"x-bifrost-provider": "vllm-qwen"}))

    resp = _post("/v1/responses", RESPONSES_BODY)

    assert resp.status_code == 200, resp.content
    assert seen["url"] == "http://bifrost.test/v1/chat/completions"
    sent = _lower(seen["headers"])
    assert sent["x-bf-vk"] == APISIX_HEADERS["x-bf-vk"]
    assert "authorization" not in sent
    assert seen["body"]["reasoning_effort"] == "high"
    assert "allowed_openai_params" not in seen["body"]
    assert "x-bifrost-provider" not in resp.headers


@pytest.mark.usefixtures("litellm_mode")
def test_responses_go_to_litellm_with_the_hatch(capture):
    seen = capture(_chat_json())

    resp = _post("/v1/responses", RESPONSES_BODY)

    assert resp.status_code == 200, resp.content
    assert seen["url"] == "https://litellm.test/v1/chat/completions"
    assert seen["body"]["allowed_openai_params"] == ["reasoning_effort"]


# --- Upstream error bodies -----------------------------------------------------------
BIFROST_ERROR = {
    "is_bifrost_error": False,
    "status_code": 400,
    "error": {"message": "context length exceeded", "type": "invalid_request_error"},
    "extra_fields": {
        "provider": "vllm-qwen",
        "routing_info": {"provider": "vllm-qwen", "key": "prod-key-a", "model": "Qwen/Qwen3.5-32B"},
    },
}


@pytest.mark.parametrize("gateway", ["litellm", "bifrost"])
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/responses"])
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
def test_error_bodies_lose_the_gateway_routing_details(capture, monkeypatch, gateway, path, stream):
    monkeypatch.setenv("LLM_GATEWAY", gateway)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.test")
    monkeypatch.setenv("CONVERTER_BIFROST_URL", "http://bifrost.test")
    capture(httpx.Response(400, headers={"content-type": "application/json"}, content=json.dumps(BIFROST_ERROR).encode()))

    body = {**(MESSAGES_BODY if path == "/v1/messages" else RESPONSES_BODY), "stream": stream}
    resp = _post(path, body)

    assert resp.status_code == 400
    assert resp.json() == {
        "is_bifrost_error": False,
        "status_code": 400,
        "error": {"message": "context length exceeded", "type": "invalid_request_error"},
    }
    assert "prod-key-a" not in resp.text


@pytest.mark.usefixtures("litellm_mode")
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/responses"])
@pytest.mark.parametrize("stream", [False, True], ids=["non-stream", "stream"])
def test_non_json_error_bodies_pass_unchanged(capture, path, stream):
    capture(httpx.Response(502, headers={"content-type": "text/plain"}, content=b"bad gateway: extra_fields"))

    body = {**(MESSAGES_BODY if path == "/v1/messages" else RESPONSES_BODY), "stream": stream}
    resp = _post(path, body)

    assert resp.status_code == 502
    assert resp.content == b"bad gateway: extra_fields"


@pytest.mark.usefixtures("bifrost_mode")
def test_an_error_body_without_gateway_details_is_forwarded_byte_for_byte(capture):
    raw = b'{"error":  {"message": "rate limited"}}'
    capture(httpx.Response(429, headers={"content-type": "application/json"}, content=raw))

    resp = _post("/v1/messages", MESSAGES_BODY)

    assert resp.status_code == 429
    assert resp.content == raw


@pytest.mark.usefixtures("bifrost_mode")
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/responses"])
def test_streams_drop_bifrost_routing_headers(capture, path):
    capture(_chat_sse({"x-bifrost-routing-info-key": "prod-key-a", "x-request-id": "req-1"}))

    body = {**(MESSAGES_BODY if path == "/v1/messages" else RESPONSES_BODY), "stream": True}
    resp = _post(path, body)

    assert resp.status_code == 200
    assert "x-bifrost-routing-info-key" not in resp.headers
    assert resp.headers["x-request-id"] == "req-1"
