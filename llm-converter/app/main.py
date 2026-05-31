"""LLM endpoint converter.

Translates newer LLM API shapes that sglang/vLLM-backed LiteLLM models do not
serve reliably into the well-supported ``/v1/chat/completions`` shape, then
forwards to the upstream LiteLLM proxy.

Phase 1 implements ``POST /v1/messages`` (Anthropic Messages). The request is
translated to an OpenAI chat-completions body, sent to
``{LITELLM_URL}/v1/chat/completions``, and the response (streaming SSE or
one-shot JSON) is translated back to the Anthropic shape — bypassing LiteLLM's
own Anthropic adapter, which mis-serializes tool calls and reasoning content
for ``hosted_vllm``/``openai`` providers.

Authentication is handled upstream by APISIX (key-auth + master-key injection);
this service trusts its private network and forwards the ``Authorization`` and
``x-litellm-end-user-id`` headers APISIX set.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from app.config import settings
from app.messages_bridge import (
    anthropic_request_to_openai_body,
    openai_response_to_anthropic_body,
    openai_stream_to_anthropic_events,
)
from app.sse import (
    DROP_FROM_REQUEST,
    DROP_FROM_RESPONSE,
    filter_headers,
    format_sse,
    iter_openai_sse_chunks,
)
from app.stream_sanitizer import sanitize_events

logger = logging.getLogger(__name__)

# Upstream error/non-SSE bodies should be small and arrive promptly. Bound the
# read so a misbehaving upstream that opens a non-event-stream response and then
# trickles (or never finishes) the body cannot pin a worker forever — the
# client's read timeout is left unbounded for legitimately long completions.
_ERROR_BODY_READ_TIMEOUT = 30.0

app = FastAPI(title="UniBridge LLM Converter")


def _make_client(timeout: float | None) -> httpx.AsyncClient:
    """AsyncClient factory; tests monkeypatch this to inject a MockTransport."""
    return httpx.AsyncClient(timeout=timeout, verify=settings.tls_verify)


def _bad_request(message: str) -> Response:
    return Response(
        status_code=400,
        content=json.dumps({"error": {"type": "invalid_request_error", "message": message}}).encode(
            "utf-8"
        ),
        media_type="application/json",
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    """Translate an Anthropic Messages request through the OpenAI chat route."""
    raw = await request.body()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _bad_request("request body is not valid JSON")
    if not isinstance(parsed, dict):
        return _bad_request("request body must be a JSON object")

    is_stream = bool(parsed.get("stream", False))

    openai_body = anthropic_request_to_openai_body(parsed)
    openai_bytes = json.dumps(openai_body, ensure_ascii=False).encode("utf-8")

    fwd_headers = filter_headers(request.headers.items(), DROP_FROM_REQUEST)
    fwd_headers["content-type"] = "application/json"

    upstream_url = f"{settings.LITELLM_URL}/v1/chat/completions"
    logger.debug(
        "converter messages: upstream=%s stream=%s messages=%d tools=%d",
        upstream_url,
        bool(openai_body.get("stream")),
        len(openai_body.get("messages") or []),
        len(openai_body.get("tools") or []),
    )

    client = _make_client(settings.request_timeout)
    upstream_req = client.build_request(
        "POST", upstream_url, content=openai_bytes, headers=fwd_headers
    )

    if not is_stream:
        try:
            upstream = await client.send(upstream_req)
            resp_headers = filter_headers(upstream.headers.items(), DROP_FROM_RESPONSE)
            content = upstream.content
            media_type = upstream.headers.get("content-type")
            # Translate a successful OpenAI JSON body to Anthropic shape. Error
            # responses / non-JSON bodies are forwarded verbatim so the client
            # can see what really happened upstream.
            if (
                200 <= upstream.status_code < 300
                and (media_type or "").lower().startswith("application/json")
            ):
                try:
                    openai_resp = json.loads(content)
                    if isinstance(openai_resp, dict):
                        anthropic_resp = openai_response_to_anthropic_body(openai_resp)
                        content = json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8")
                        media_type = "application/json"
                        # ``content-length`` is invalidated by the rewrite; let
                        # Starlette recompute it.
                        resp_headers.pop("content-length", None)
                except json.JSONDecodeError:
                    pass
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=media_type,
            )
        finally:
            await client.aclose()

    try:
        upstream = await client.send(upstream_req, stream=True)
    except Exception:
        # send() can raise before returning a response (connect/TLS/timeout);
        # the non-stream branch above is guarded by try/finally, but this path
        # must close the client itself to avoid leaking it and its pool.
        await client.aclose()
        raise

    # The bridge only knows how to translate OpenAI chat-completions SSE. If
    # upstream returned a JSON/HTML error (or anything else), forward it
    # verbatim rather than feeding it to the SSE parser (which would silently
    # drop the body and leave the client with an empty stream).
    upstream_ctype = upstream.headers.get("content-type", "")
    if not upstream_ctype.lower().startswith("text/event-stream"):
        try:
            try:
                content = await asyncio.wait_for(
                    upstream.aread(), timeout=_ERROR_BODY_READ_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "converter timed out reading non-SSE upstream body (status=%s)",
                    upstream.status_code,
                )
                return Response(
                    status_code=504,
                    content=json.dumps(
                        {"error": {"type": "timeout", "message": "upstream read timed out"}}
                    ).encode("utf-8"),
                    media_type="application/json",
                )
            if upstream.status_code >= 400:
                logger.warning(
                    "converter upstream error %s ctype=%s body_bytes=%d",
                    upstream.status_code,
                    upstream_ctype,
                    len(content),
                )
            resp_headers = filter_headers(upstream.headers.items(), DROP_FROM_RESPONSE)
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=upstream_ctype or None,
            )
        finally:
            await upstream.aclose()
            await client.aclose()

    bridge_model = parsed.get("model") or openai_body.get("model") or ""

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            anthropic_events = openai_stream_to_anthropic_events(
                iter_openai_sse_chunks(upstream),
                model=str(bridge_model),
            )
            # Run the bridge output through ``sanitize_events`` too so any
            # invariant slip in the conversion still gets caught (empty-delta
            # drop, monotonic indices, dangling-block close).
            async for sanitized in sanitize_events(anthropic_events):
                yield format_sse(sanitized)
        finally:
            await upstream.aclose()
            await client.aclose()

    resp_headers = filter_headers(upstream.headers.items(), DROP_FROM_RESPONSE)
    resp_headers["Cache-Control"] = "no-cache"
    resp_headers["X-Accel-Buffering"] = "no"

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type="text/event-stream",
    )
