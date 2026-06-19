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
from app.responses_bridge import (
    assistant_message_from_chat,
    chat_response_to_responses_body,
    chat_stream_to_responses_events,
    new_response_id,
    previous_response_not_found_body,
    responses_request_to_chat_body,
)
from app.responses_state import conversation_store
from app.sse import (
    DROP_FROM_REQUEST,
    DROP_FROM_RESPONSE,
    filter_headers,
    format_sse,
    iter_openai_sse_chunks,
    with_heartbeat,
)
from app.stream_sanitizer import sanitize_events

logger = logging.getLogger(__name__)

# Upstream error/non-SSE bodies should be small and arrive promptly. Bound the
# read so a misbehaving upstream that opens a non-event-stream response and then
# trickles (or never finishes) the body cannot pin a worker forever — the
# client's read timeout is left unbounded for legitimately long completions.
_ERROR_BODY_READ_TIMEOUT = 120.0

app = FastAPI(title="UniBridge LLM Converter")


def _make_client(timeout: float | None) -> httpx.AsyncClient:
    """AsyncClient factory; tests monkeypatch this to inject a MockTransport."""
    return httpx.AsyncClient(timeout=timeout, verify=settings.tls_verify)


# Cap the full-body trace so a giant request (huge system prompt + many tool
# schemas) can't blow up a single log line; the diff-relevant prefix survives.
_TRACE_BODY_MAX = 200_000


def _summarize_tool_calls(tool_calls: object) -> object:
    """Compact view of a streaming ``delta.tool_calls`` for the trace log: just
    the index/id/name and the arguments-fragment length, never the full args."""
    if not isinstance(tool_calls, list):
        return None
    out = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        args = fn.get("arguments")
        out.append(
            {
                "index": tc.get("index"),
                "id": tc.get("id"),
                "name": fn.get("name"),
                "args_len": len(args) if isinstance(args, str) else 0,
            }
        )
    return out or None


def _trace_incoming_messages_request(parsed: dict) -> None:
    """Log the decisive parts of an incoming ``/v1/messages`` body so two clients
    hitting the same model can be diffed (system steering / tool set / thinking).
    Gated by ``settings.trace``; dumps the full body (capped) at INFO."""
    if not settings.trace:
        return
    try:
        system = parsed.get("system")
        if isinstance(system, str):
            system_kind, system_len = "str", len(system)
        elif isinstance(system, list):
            system_kind, system_len = "blocks", len(system)
        elif system is None:
            system_kind, system_len = "absent", 0
        else:
            system_kind, system_len = type(system).__name__, 0
        tools = parsed.get("tools")
        tool_names = (
            [t.get("name") for t in tools if isinstance(t, dict)]
            if isinstance(tools, list)
            else []
        )
        logger.info(
            "converter trace request: model=%s stream=%s system=%s/%d "
            "tools=%d thinking=%s tool_choice=%s max_tokens=%s temperature=%s keys=%s",
            parsed.get("model"),
            bool(parsed.get("stream", False)),
            system_kind,
            system_len,
            len(tool_names),
            parsed.get("thinking"),
            parsed.get("tool_choice"),
            parsed.get("max_tokens"),
            parsed.get("temperature"),
            sorted(parsed.keys()),
        )
        logger.info("converter trace request tool_names=%s", tool_names)
        body = json.dumps(parsed, ensure_ascii=False)
        if len(body) > _TRACE_BODY_MAX:
            body = body[:_TRACE_BODY_MAX] + f"…[+{len(body) - _TRACE_BODY_MAX} chars]"
        logger.info("converter trace request body=%s", body)
    except Exception:
        logger.exception("converter trace: request inspect failed")


async def _trace_upstream_chunks(
    chunks: AsyncIterator[dict], tag: str
) -> AsyncIterator[dict]:
    """Pass-through that logs each DECISIVE upstream OpenAI chunk — one carrying a
    ``finish_reason`` or ``delta.tool_calls`` — so we can see whether vLLM emitted
    structured tool calls or finished with plain text. Token-by-token content
    deltas are intentionally NOT logged (too noisy)."""
    async for chunk in chunks:
        try:
            choices = chunk.get("choices") or []
            ch = choices[0] if isinstance(choices, list) and choices else {}
            if isinstance(ch, dict):
                delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
                fr = ch.get("finish_reason")
                tc = delta.get("tool_calls")
                if fr or tc:
                    logger.info(
                        "converter trace upstream[%s]: finish_reason=%s tool_calls=%s "
                        "has_content=%s has_reasoning=%s",
                        tag,
                        fr,
                        _summarize_tool_calls(tc),
                        bool(delta.get("content")),
                        bool(delta.get("reasoning_content")),
                    )
        except Exception:
            logger.exception("converter trace: upstream chunk inspect failed")
        yield chunk


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

    _trace_incoming_messages_request(parsed)

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
                except (KeyError, TypeError, ValueError, AttributeError):
                    # 2xx JSON that is structurally unexpected makes translation
                    # raise; forward the raw upstream body unchanged rather than
                    # returning a bare 500.
                    logger.warning(
                        "converter messages: upstream 2xx body could not be translated; "
                        "forwarding raw body unchanged",
                        exc_info=True,
                    )
                    content = upstream.content
                    media_type = upstream.headers.get("content-type")
                    resp_headers = filter_headers(upstream.headers.items(), DROP_FROM_RESPONSE)
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
            upstream_chunks = iter_openai_sse_chunks(upstream)
            if settings.trace:
                upstream_chunks = _trace_upstream_chunks(
                    upstream_chunks, str(bridge_model)
                )
            anthropic_events = openai_stream_to_anthropic_events(
                upstream_chunks,
                model=str(bridge_model),
            )
            # Run the bridge output through ``sanitize_events`` too so any
            # invariant slip in the conversion still gets caught (empty-delta
            # drop, monotonic indices, dangling-block close).
            async for sanitized in sanitize_events(anthropic_events):
                yield format_sse(sanitized)
        except Exception:
            # The bridge/upstream raised mid-stream (connection reset, malformed
            # chunk, etc.). ``message_start`` — and possibly an open content
            # block — has already reached the client, so emit a terminal
            # Anthropic ``error`` event (the spec's stream terminus) instead of
            # letting the exception propagate and leave the client hanging on a
            # truncated message with no terminator. Mirrors the /v1/responses
            # route's ``response.failed`` fallback.
            logger.exception("converter messages: bridge error mid-stream")
            yield format_sse(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": "converter stream error"},
                }
            )
        finally:
            await upstream.aclose()
            await client.aclose()

    resp_headers = filter_headers(upstream.headers.items(), DROP_FROM_RESPONSE)
    resp_headers["Cache-Control"] = "no-cache"
    resp_headers["X-Accel-Buffering"] = "no"

    return StreamingResponse(
        with_heartbeat(body_iter(), settings.sse_heartbeat_seconds),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type="text/event-stream",
    )


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    """Translate an OpenAI Responses request through the chat-completions route.

    Resolves ``previous_response_id`` from the in-memory conversation store,
    forwards to LiteLLM, translates the result back to the Responses shape, and
    (when ``store`` is not false) persists the accumulated transcript under a
    freshly minted ``resp_<id>`` so the next turn can chain off it.
    """
    raw = await request.body()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _bad_request("request body is not valid JSON")
    if not isinstance(parsed, dict):
        return _bad_request("request body must be a JSON object")

    is_stream = bool(parsed.get("stream", False))
    store_flag = parsed.get("store", True)
    prev_id = parsed.get("previous_response_id")

    prior_messages = None
    if prev_id is not None:
        if not isinstance(prev_id, str) or not prev_id:
            return _bad_request("previous_response_id must be a non-empty string")
        prior_messages = conversation_store.get(prev_id)
        if prior_messages is None:
            return Response(
                status_code=400,
                content=json.dumps(previous_response_not_found_body(prev_id)).encode("utf-8"),
                media_type="application/json",
            )

    chat_body = responses_request_to_chat_body(parsed, prior_messages)
    chat_body["stream"] = is_stream
    if is_stream:
        stream_options = chat_body.get("stream_options") or {}
        stream_options.setdefault("include_usage", True)
        chat_body["stream_options"] = stream_options
    base_messages = chat_body["messages"]  # prior chain + this turn's input
    chat_bytes = json.dumps(chat_body, ensure_ascii=False).encode("utf-8")

    fwd_headers = filter_headers(request.headers.items(), DROP_FROM_REQUEST)
    fwd_headers["content-type"] = "application/json"
    upstream_url = f"{settings.LITELLM_URL}/v1/chat/completions"
    response_id = new_response_id()

    logger.debug(
        "converter responses: upstream=%s stream=%s prev=%s messages=%d tools=%d",
        upstream_url, is_stream, bool(prev_id),
        len(base_messages), len(chat_body.get("tools") or []),
    )

    client = _make_client(settings.request_timeout)
    upstream_req = client.build_request("POST", upstream_url, content=chat_bytes, headers=fwd_headers)

    if not is_stream:
        try:
            upstream = await client.send(upstream_req)
            resp_headers = filter_headers(upstream.headers.items(), DROP_FROM_RESPONSE)
            content = upstream.content
            media_type = upstream.headers.get("content-type")
            if (
                200 <= upstream.status_code < 300
                and (media_type or "").lower().startswith("application/json")
            ):
                try:
                    chat = json.loads(content)
                    if isinstance(chat, dict):
                        resp_obj = chat_response_to_responses_body(
                            chat, parsed, response_id, emit_reasoning=settings.emit_reasoning
                        )
                        content = json.dumps(resp_obj, ensure_ascii=False).encode("utf-8")
                        media_type = "application/json"
                        resp_headers.pop("content-length", None)
                        if store_flag:
                            message = (chat.get("choices") or [{}])[0].get("message") or {}
                            assistant = assistant_message_from_chat(message)
                            # Skip persisting an empty assistant turn (no content,
                            # no tool_calls) — matches the streaming path, which
                            # only persists when there is real output.
                            if assistant.get("content") or assistant.get("tool_calls"):
                                conversation_store.put(
                                    response_id, base_messages + [assistant]
                                )
                                # Supersede the chained-from response: a linear
                                # chain only needs the latest transcript, so drop
                                # the parent to keep total memory O(N) not O(N^2).
                                # (Trades away branching off a shared prev id.)
                                if prev_id is not None:
                                    conversation_store.delete(prev_id)
                except json.JSONDecodeError:
                    logger.warning(
                        "converter responses: upstream 2xx returned unparseable JSON; "
                        "forwarding raw body unchanged"
                    )
                except (KeyError, TypeError, ValueError, AttributeError):
                    # A 2xx body that parses as JSON but is structurally unexpected
                    # (e.g. choices is a dict) makes translation raise. Forward the
                    # raw upstream body unchanged rather than turning it into a bare
                    # 500 — mirrors the streaming response.failed fallback.
                    logger.warning(
                        "converter responses: upstream 2xx body could not be translated; "
                        "forwarding raw body unchanged",
                        exc_info=True,
                    )
                    content = upstream.content
                    media_type = upstream.headers.get("content-type")
                    resp_headers = filter_headers(upstream.headers.items(), DROP_FROM_RESPONSE)
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
        await client.aclose()
        raise

    upstream_ctype = upstream.headers.get("content-type", "")
    if not upstream_ctype.lower().startswith("text/event-stream"):
        try:
            try:
                content = await asyncio.wait_for(upstream.aread(), timeout=_ERROR_BODY_READ_TIMEOUT)
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

    holder: dict = {}

    async def body_iter() -> AsyncIterator[bytes]:
        persisted = False
        failed = False
        last_seq = -1
        try:
            events = chat_stream_to_responses_events(
                iter_openai_sse_chunks(upstream),
                response_id=response_id,
                request_body=parsed,
                holder=holder,
                emit_reasoning=settings.emit_reasoning,
            )
            async for payload in events:
                sn = payload.get("sequence_number")
                if isinstance(sn, int):
                    last_seq = sn
                # Persist the transcript just BEFORE the client sees the terminal
                # event (which carries the response id), closing the race where a
                # fast follow-up chains off an id not yet stored.
                if (
                    not persisted
                    and store_flag
                    and payload.get("type") in ("response.completed", "response.incomplete")
                    and holder.get("assistant_message")
                ):
                    conversation_store.put(
                        response_id, base_messages + [holder["assistant_message"]]
                    )
                    # Supersede the parent (see non-stream branch) — linear chain
                    # retains only the latest transcript.
                    if prev_id is not None:
                        conversation_store.delete(prev_id)
                    persisted = True
                yield format_sse(payload)
        except Exception:
            # The bridge raised mid-stream (malformed upstream chunk, etc.). Emit a
            # best-effort terminal failure so the client isn't left hanging on a
            # truncated stream, and do not persist a partial transcript. The
            # synthesized event must continue the monotonic sequence_number series
            # the normal path emits.
            failed = True
            logger.exception("converter responses: bridge error mid-stream")
            yield format_sse(
                {
                    "type": "response.failed",
                    "sequence_number": last_seq + 1,
                    "response": {
                        "id": response_id, "object": "response", "status": "failed",
                        "error": {"code": "server_error", "message": "converter stream error"},
                        "output": [], "usage": None,
                    },
                }
            )
        finally:
            await upstream.aclose()
            await client.aclose()
            # Fallback persistence if the terminal event path didn't run but a
            # complete transcript is available; never persist after a bridge error.
            if not persisted and not failed and store_flag and holder.get("assistant_message"):
                conversation_store.put(response_id, base_messages + [holder["assistant_message"]])
                if prev_id is not None:
                    conversation_store.delete(prev_id)

    resp_headers = filter_headers(upstream.headers.items(), DROP_FROM_RESPONSE)
    resp_headers["Cache-Control"] = "no-cache"
    resp_headers["X-Accel-Buffering"] = "no"

    return StreamingResponse(
        with_heartbeat(body_iter(), settings.sse_heartbeat_seconds),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type="text/event-stream",
    )
