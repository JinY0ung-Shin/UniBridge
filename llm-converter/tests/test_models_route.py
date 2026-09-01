"""Tests for ``GET /v1/models`` and the inbound alias-prefix strip.

The listing advertises every upstream model twice — under its own id and under
``CONVERTER_MODEL_ALIAS_PREFIX`` — so Claude Code can auto-detect a model
through the gateway; the strip on ``/v1/messages`` / ``/v1/responses`` is what
makes the advertised alias callable. The two halves are tested together because
one is useless without the other.
"""

from __future__ import annotations

import json

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


def _listing(*entries: dict) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps({"object": "list", "data": list(entries)}).encode("utf-8"),
    )


def _ids(body: dict) -> list[str]:
    return [entry["id"] for entry in body["data"]]


# --- Listing augmentation ----------------------------------------------------
def test_originals_kept_and_aliases_appended():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://upstream.test/v1/models"
        return _listing(
            {"id": "qwen3.5-32b", "object": "model", "created": 1_700_000_000},
            {"id": "gpt-5.4-mini", "object": "model", "created": 1_700_000_001},
        )

    client = TestClient(_make_app(handler))
    resp = client.get("/v1/models")

    assert resp.status_code == 200
    assert _ids(resp.json()) == [
        "qwen3.5-32b",
        "claude/qwen3.5-32b",
        "gpt-5.4-mini",
        "claude/gpt-5.4-mini",
    ]


def test_model_already_named_like_the_vendor_gets_no_alias():
    """A real Anthropic model proxied by LiteLLM must not become
    ``claude/claude-sonnet-4`` — it is already selectable as-is."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _listing(
            {"id": "claude-sonnet-4", "object": "model"},
            {"id": "CLAUDE-OPUS-4", "object": "model"},  # case-insensitive
            {"id": "claude/pre-aliased", "object": "model"},
            {"id": "qwen3.5-32b", "object": "model"},
        )

    client = TestClient(_make_app(handler))
    resp = client.get("/v1/models")

    assert _ids(resp.json()) == [
        "claude-sonnet-4",
        "CLAUDE-OPUS-4",
        "claude/pre-aliased",
        "qwen3.5-32b",
        "claude/qwen3.5-32b",
    ]


def test_every_entry_carries_both_schemas():
    def handler(request: httpx.Request) -> httpx.Response:
        return _listing({"id": "qwen3.5-32b", "created": 1_700_000_000})

    client = TestClient(_make_app(handler))
    body = client.get("/v1/models").json()

    for entry in body["data"]:
        # OpenAI's shape
        assert entry["object"] == "model"
        assert entry["created"] == 1_700_000_000
        assert entry["owned_by"]
        # Anthropic's shape
        assert entry["type"] == "model"
        assert entry["display_name"] == entry["id"]
        assert entry["created_at"] == "2023-11-14T22:13:20+00:00"

    # The alias's display_name is the aliased id, not the original.
    assert body["data"][1]["display_name"] == "claude/qwen3.5-32b"


def test_upstream_fields_are_never_overwritten():
    def handler(request: httpx.Request) -> httpx.Response:
        return _listing(
            {
                "id": "qwen3.5-32b",
                "object": "custom-object",
                "owned_by": "acme",
                "display_name": "Upstream Chose This",
                "created": 1_700_000_000,
                "created_at": "1999-01-01T00:00:00+00:00",
                "extra": {"kept": True},
            }
        )

    client = TestClient(_make_app(handler))
    entry = client.get("/v1/models").json()["data"][0]

    assert entry["object"] == "custom-object"
    assert entry["owned_by"] == "acme"
    assert entry["display_name"] == "Upstream Chose This"
    assert entry["created_at"] == "1999-01-01T00:00:00+00:00"
    assert entry["extra"] == {"kept": True}


def test_missing_created_leaves_both_timestamps_absent():
    """A fabricated date would be worse than a missing optional field."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _listing({"id": "qwen3.5-32b", "object": "model"})

    client = TestClient(_make_app(handler))
    entry = client.get("/v1/models").json()["data"][0]

    assert "created" not in entry
    assert "created_at" not in entry


def test_top_level_carries_both_pagination_shapes():
    def handler(request: httpx.Request) -> httpx.Response:
        return _listing({"id": "a", "object": "model"}, {"id": "b", "object": "model"})

    client = TestClient(_make_app(handler))
    body = client.get("/v1/models").json()

    assert body["object"] == "list"
    assert body["has_more"] is False
    assert body["first_id"] == "a"
    assert body["last_id"] == "claude/b"


def test_empty_listing_reports_null_page_bounds():
    def handler(request: httpx.Request) -> httpx.Response:
        return _listing()

    client = TestClient(_make_app(handler))
    body = client.get("/v1/models").json()

    assert body["data"] == []
    assert body["has_more"] is False
    assert body["first_id"] is None
    assert body["last_id"] is None


def test_entries_that_are_not_objects_are_forwarded_untouched():
    def handler(request: httpx.Request) -> httpx.Response:
        return _listing("not-an-object", {"id": "qwen3.5-32b"})  # type: ignore[arg-type]

    client = TestClient(_make_app(handler))
    body = client.get("/v1/models").json()

    assert body["data"][0] == "not-an-object"
    assert [e["id"] for e in body["data"][1:]] == ["qwen3.5-32b", "claude/qwen3.5-32b"]


def test_body_without_a_data_list_is_left_alone():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"object": "list", "data": "not a list"}).encode("utf-8"),
        )

    client = TestClient(_make_app(handler))
    body = client.get("/v1/models").json()

    assert body == {"object": "list", "data": "not a list"}


def test_every_entry_has_id_and_display_name_from_a_minimal_upstream():
    """Claude Code's discovery reads Anthropic-native ``{"id", "display_name"}``
    and ignores everything else, so those two are the hard requirement — even
    when upstream sends nothing but an id."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _listing({"id": "qwen3.5-32b"})

    client = TestClient(_make_app(handler))
    entries = client.get("/v1/models").json()["data"]

    assert [(e["id"], e["display_name"]) for e in entries] == [
        ("qwen3.5-32b", "qwen3.5-32b"),
        ("claude/qwen3.5-32b", "claude/qwen3.5-32b"),
    ]


def test_unknown_query_params_are_tolerated_and_not_forwarded():
    """Claude Code's discovery requests ``/v1/models?limit=1000``. That must not
    be an error, and the listing is one un-paginated page, so the param has
    nothing to do upstream."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return _listing({"id": "qwen3.5-32b", "object": "model"})

    client = TestClient(_make_app(handler))
    resp = client.get("/v1/models?limit=1000&unknown=x")

    assert resp.status_code == 200
    assert captured["url"] == "http://upstream.test/v1/models"
    assert _ids(resp.json()) == ["qwen3.5-32b", "claude/qwen3.5-32b"]


def test_discovery_without_an_anthropic_version_header_succeeds():
    """Claude Code omits ``anthropic-version`` on discovery (it sends it only on
    Messages calls), so nothing on this path may require it."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("anthropic-version") is None
        return _listing({"id": "qwen3.5-32b", "object": "model"})

    client = TestClient(_make_app(handler))
    assert client.get("/v1/models").status_code == 200


def test_authorization_header_is_forwarded():
    """APISIX injects the LiteLLM master key; the converter must pass it on."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return _listing({"id": "qwen3.5-32b"})

    client = TestClient(_make_app(handler))
    client.get("/v1/models", headers={"Authorization": "Bearer sk-master"})

    assert captured["auth"] == "Bearer sk-master"


# --- Upstream failures forwarded verbatim ------------------------------------
def test_upstream_error_status_is_forwarded_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "application/json"},
            content=b'{"error": {"message": "boom"}}',
        )

    client = TestClient(_make_app(handler))
    resp = client.get("/v1/models")

    assert resp.status_code == 500
    assert resp.json() == {"error": {"message": "boom"}}


def test_non_json_upstream_is_forwarded_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>nope</html>"
        )

    client = TestClient(_make_app(handler))
    resp = client.get("/v1/models")

    assert resp.status_code == 200
    assert resp.content == b"<html>nope</html>"
    assert resp.headers["content-type"].startswith("text/html")


def test_json_content_type_with_unparseable_body_is_forwarded_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{ not json"
        )

    client = TestClient(_make_app(handler))
    resp = client.get("/v1/models")

    assert resp.status_code == 200
    assert resp.content == b"{ not json"


def test_upstream_timeout_returns_504():
    async def _slow_send(self, *args, **kwargs):  # noqa: ANN001, ARG001
        raise TimeoutError

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return _listing()

    app = _make_app(handler)
    # asyncio.wait_for surfaces the deadline as asyncio.TimeoutError, which is
    # TimeoutError on 3.11+; raising it from send() takes the same branch.
    original = httpx.AsyncClient.send
    httpx.AsyncClient.send = _slow_send  # type: ignore[assignment]
    try:
        resp = TestClient(app).get("/v1/models")
    finally:
        httpx.AsyncClient.send = original  # type: ignore[assignment]

    assert resp.status_code == 504
    body = resp.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "timeout"


# --- Disabling the feature ---------------------------------------------------
def test_empty_prefix_disables_aliasing(monkeypatch):
    monkeypatch.setenv("CONVERTER_MODEL_ALIAS_PREFIX", "")

    def handler(request: httpx.Request) -> httpx.Response:
        return _listing({"id": "qwen3.5-32b", "object": "model"})

    client = TestClient(_make_app(handler))
    body = client.get("/v1/models").json()

    assert _ids(body) == ["qwen3.5-32b"]


def test_custom_prefix_is_honored(monkeypatch):
    monkeypatch.setenv("CONVERTER_MODEL_ALIAS_PREFIX", "anthropic/")

    def handler(request: httpx.Request) -> httpx.Response:
        return _listing({"id": "qwen3.5-32b", "object": "model"})

    client = TestClient(_make_app(handler))
    body = client.get("/v1/models").json()

    assert _ids(body) == ["qwen3.5-32b", "anthropic/qwen3.5-32b"]


# --- Inbound strip: /v1/messages --------------------------------------------
def _chat_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(
            {
                "id": "chatcmpl-1",
                "model": "qwen3.5-32b",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode("utf-8"),
    )


def _messages_body(model: object) -> dict:
    return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


def _capture_upstream_model(payload: dict, path: str) -> object:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _chat_response()

    client = TestClient(_make_app(handler))
    resp = client.post(path, json=payload)
    assert resp.status_code == 200, resp.content
    return captured["body"]["model"]


def test_messages_strips_the_alias_prefix():
    assert (
        _capture_upstream_model(_messages_body("claude/qwen3.5-32b"), "/v1/messages")
        == "qwen3.5-32b"
    )


def test_messages_leaves_an_unprefixed_model_alone():
    assert (
        _capture_upstream_model(_messages_body("qwen3.5-32b"), "/v1/messages")
        == "qwen3.5-32b"
    )


def test_messages_strip_is_disabled_by_an_empty_prefix(monkeypatch):
    monkeypatch.setenv("CONVERTER_MODEL_ALIAS_PREFIX", "")
    assert (
        _capture_upstream_model(_messages_body("claude/qwen3.5-32b"), "/v1/messages")
        == "claude/qwen3.5-32b"
    )


def test_messages_non_string_model_does_not_crash():
    """Every inbound field may be the wrong type; the strip must not be the
    thing that turns that into a 500."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _chat_response()

    client = TestClient(_make_app(handler))
    resp = client.post("/v1/messages", json=_messages_body({"not": "a string"}))

    assert resp.status_code == 200
    assert captured["body"]["model"] == {"not": "a string"}


def test_messages_streaming_also_strips_the_prefix():
    """The strip runs before the stream/non-stream branch, so SSE requests get
    the real model upstream — and the events echo it, matching what the
    non-streaming path already reports (it reads the model off the upstream
    response)."""
    captured = {}
    sse = (
        b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
        b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse
        )

    client = TestClient(_make_app(handler))
    body = _messages_body("claude/qwen3.5-32b")
    body["stream"] = True
    with client.stream("POST", "/v1/messages", json=body) as resp:
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    assert captured["body"]["model"] == "qwen3.5-32b"
    assert '"model":"qwen3.5-32b"' in raw.replace(" ", "")


def test_strip_only_removes_a_leading_prefix():
    """``some/claude/x`` is not an alias — the prefix must be a prefix."""
    assert (
        _capture_upstream_model(_messages_body("some/claude/x"), "/v1/messages")
        == "some/claude/x"
    )


# --- Inbound strip: /v1/responses -------------------------------------------
def _responses_body(model: object) -> dict:
    return {"model": model, "input": "hi", "store": False}


def test_responses_strips_the_alias_prefix():
    assert (
        _capture_upstream_model(_responses_body("claude/qwen3.5-32b"), "/v1/responses")
        == "qwen3.5-32b"
    )


def test_responses_leaves_an_unprefixed_model_alone():
    assert (
        _capture_upstream_model(_responses_body("qwen3.5-32b"), "/v1/responses")
        == "qwen3.5-32b"
    )


def test_responses_strip_is_disabled_by_an_empty_prefix(monkeypatch):
    monkeypatch.setenv("CONVERTER_MODEL_ALIAS_PREFIX", "")
    assert (
        _capture_upstream_model(_responses_body("claude/qwen3.5-32b"), "/v1/responses")
        == "claude/qwen3.5-32b"
    )
