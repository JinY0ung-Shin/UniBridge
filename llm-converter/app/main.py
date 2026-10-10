"""LLM endpoint converter.

Translates newer LLM API shapes that sglang/vLLM-backed models do not serve
reliably into the well-supported ``/v1/chat/completions`` shape, then forwards
to the upstream LLM gateway — LiteLLM or Bifrost, picked by ``LLM_GATEWAY``.

``POST /v1/messages`` (Anthropic Messages) and ``POST /v1/responses`` (OpenAI
Responses) are translated to an OpenAI chat-completions body, sent to
``{upstream}/v1/chat/completions``, and the response (streaming SSE or one-shot
JSON) is translated back — bypassing the gateways' own adapters, which
mis-serialize tool calls and reasoning content for vLLM/SGLang backends.
``GET /v1/models`` relays the gateway's listing, and
``POST /v1/messages/count_tokens`` is answered locally.

Authentication is handled upstream by APISIX (key-auth + credential
injection); this service trusts its private network and forwards the gateway
credential headers APISIX set — only the active gateway's (see
``sse.forward_request_headers``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

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
    namespace_map_from_tools,
    new_response_id,
    previous_response_not_found_body,
    resolve_length_as_completed,
    responses_request_to_chat_body,
)
from app.responses_state import conversation_store
from app.sse import (
    format_sse,
    forward_request_headers,
    forward_response_headers,
    iter_openai_sse_chunks,
    with_heartbeat,
)
from app.stream_sanitizer import sanitize_events
from app.token_estimate import InvalidCountRequest, estimate_input_tokens

logger = logging.getLogger(__name__)

# Uvicorn configures only its own ``uvicorn*`` loggers; this module's logger
# propagates to a root logger that defaults to WARNING, so the opt-in INFO traces
# below would be swallowed. When tracing is on, raise the ``app`` package logger
# to INFO and make sure a root handler exists to actually emit it.
if settings.trace:
    logging.getLogger("app").setLevel(logging.INFO)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO)

# Upstream error/non-SSE bodies should be small and arrive promptly. Bound the
# read so a misbehaving upstream that opens a non-event-stream response and then
# trickles (or never finishes) the body cannot pin a worker forever — the
# client's read timeout is left unbounded for legitimately long completions.
_ERROR_BODY_READ_TIMEOUT = 120.0

# A model listing is a small response — nothing generates — so this is a
# worker-safety net on the fetch itself, not the caller's deadline
# (``settings.models_timeout``): Claude Code's discovery gives up after ~3s, and
# Bifrost asks every provider key live on each listing with no deadline of its
# own, so a fetch may outlive its caller and still fill the cache for the next.
_MODELS_FETCH_CEILING = 30.0

app = FastAPI(title="UniBridge LLM Converter")


def _make_client(timeout: float | None) -> httpx.AsyncClient:
    """AsyncClient factory; tests monkeypatch this to inject a MockTransport."""
    return httpx.AsyncClient(timeout=timeout, verify=settings.tls_verify)


async def _conversation_store_get(resp_id: str) -> list[dict] | None:
    return await asyncio.to_thread(conversation_store.get, resp_id)


async def _conversation_store_put(resp_id: str, messages: list[dict]) -> bool:
    return await asyncio.to_thread(conversation_store.put, resp_id, messages)


async def _conversation_store_delete(resp_id: str) -> None:
    await asyncio.to_thread(conversation_store.delete, resp_id)


# Cap the full-body trace so a giant request (huge system prompt + many tool
# schemas) can't blow up a single log line; the diff-relevant prefix survives.
_TRACE_BODY_MAX = 200_000

# Cap each logged tool-call argument fragment / reconstructed args blob so a
# huge tool input can't blow up a log line, while still showing the shape.
_TRACE_ARGS_MAX = 4_000


def _is_json(s: object) -> bool:
    """True when ``s`` is a string that parses as a whole JSON value."""
    if not isinstance(s, str):
        return False
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _summarize_tool_calls(tool_calls: object) -> object:
    """Compact view of a streaming ``delta.tool_calls`` for the trace log: the
    index/id/name, the arguments-fragment length, AND a capped preview of the
    actual fragment — so duplicated / truncated / malformed args (the vLLM-GLM
    dialect failure) are visible, not just their length."""
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
                "args": args[:_TRACE_ARGS_MAX] if isinstance(args, str) else None,
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
    deltas are intentionally NOT logged (too noisy). A terminal ``END`` line
    reports totals so the three failure shapes are distinguishable:
      - HANG: no ``END`` line at all (stream never completes), or END with
        ``last_finish_reason=None``.
      - tool-call-as-TEXT: ``toolcall_deltas=0`` but ``content_chunks>0`` and
        ``last_finish_reason=stop`` (the call came back as prose, not structure).
      - structured: ``toolcall_deltas>0`` (then check the per-chunk ``args`` and
        the downstream trace for malformed/duplicated args)."""
    chunk_count = 0
    toolcall_delta_count = 0
    content_chunks = 0
    reasoning_chunks = 0
    last_finish: object = None
    # Accumulate the assistant text + reasoning (capped) so a turn that ends
    # with NO structured tool call can be inspected: if the body contains a
    # ``<tool_call>``-style marker, vLLM's parser failed to extract a call the
    # model actually made (announce-then-stop); if not, it was a plain reply.
    content_buf: list[str] = []
    reasoning_buf: list[str] = []
    content_len = 0
    reasoning_len = 0
    async for chunk in chunks:
        try:
            choices = chunk.get("choices") or []
            ch = choices[0] if isinstance(choices, list) and choices else {}
            if isinstance(ch, dict):
                delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
                fr = ch.get("finish_reason")
                tc = delta.get("tool_calls")
                chunk_count += 1
                if tc:
                    toolcall_delta_count += 1
                c = delta.get("content")
                if isinstance(c, str) and c:
                    content_chunks += 1
                    if content_len < _TRACE_ARGS_MAX:
                        content_buf.append(c)
                        content_len += len(c)
                r = delta.get("reasoning_content")
                if isinstance(r, str) and r:
                    reasoning_chunks += 1
                    if reasoning_len < _TRACE_ARGS_MAX:
                        reasoning_buf.append(r)
                        reasoning_len += len(r)
                if fr:
                    last_finish = fr
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
            err = chunk.get("error")
            if err:
                logger.info("converter trace upstream[%s]: ERROR chunk=%s", tag, err)
        except Exception:
            logger.exception("converter trace: upstream chunk inspect failed")
        yield chunk
    logger.info(
        "converter trace upstream[%s] END: chunks=%d toolcall_deltas=%d "
        "content_chunks=%d reasoning_chunks=%d last_finish_reason=%s",
        tag,
        chunk_count,
        toolcall_delta_count,
        content_chunks,
        reasoning_chunks,
        last_finish,
    )
    # When a turn produced no structured tool call, dump the actual text +
    # reasoning so we can see whether a tool call is hiding in the prose (vLLM
    # parser miss) or the model simply replied. Marker hits are flagged.
    if toolcall_delta_count == 0:
        content_text = "".join(content_buf)
        reasoning_text = "".join(reasoning_buf)
        markers = [
            m
            for m in ("<tool_call>", "</tool_call>", "functools", "tool_call", "function_call", "<|tool")
            if m in content_text or m in reasoning_text
        ]
        logger.info(
            "converter trace upstream[%s] END no-toolcall: markers=%s content=%s",
            tag,
            markers,
            content_text[:_TRACE_ARGS_MAX],
        )
        logger.info(
            "converter trace upstream[%s] END no-toolcall: reasoning=%s",
            tag,
            reasoning_text[:_TRACE_ARGS_MAX],
        )


async def _trace_downstream_events(
    events: AsyncIterator[dict], tag: str
) -> AsyncIterator[dict]:
    """Pass-through over the Anthropic events the converter EMITS to the SDK.

    The upstream trace shows what vLLM sent; this shows what the bridge
    produced from it — exactly what ``claude_agent_sdk`` consumes. For each
    tool_use block it logs the id/name and the RECONSTRUCTED arguments (the
    concatenated ``input_json_delta`` fragments) plus whether they parse as
    valid JSON, so a malformed tool_use (empty name, duplicated/invalid JSON)
    is visible here even when the raw upstream looked fine. Also logs the
    terminal ``stop_reason`` and any terminal ``error`` event."""
    args_buf: dict = {}
    names: dict = {}
    async for evt in events:
        try:
            et = evt.get("type")
            if et == "content_block_start":
                cb = evt.get("content_block") or {}
                if cb.get("type") in ("tool_use", "server_tool_use"):
                    idx = evt.get("index")
                    args_buf[idx] = []
                    names[idx] = cb.get("name")
                    logger.info(
                        "converter trace downstream[%s]: tool_use START index=%s id=%s name=%r",
                        tag,
                        idx,
                        cb.get("id"),
                        cb.get("name"),
                    )
            elif et == "content_block_delta":
                d = evt.get("delta") or {}
                if d.get("type") == "input_json_delta":
                    idx = evt.get("index")
                    if idx in args_buf:
                        args_buf[idx].append(d.get("partial_json") or "")
            elif et == "content_block_stop":
                idx = evt.get("index")
                if idx in args_buf:
                    joined = "".join(args_buf.pop(idx))
                    logger.info(
                        "converter trace downstream[%s]: tool_use END index=%s name=%r "
                        "args_valid_json=%s args_len=%d args=%s",
                        tag,
                        idx,
                        names.pop(idx, None),
                        _is_json(joined),
                        len(joined),
                        joined[:_TRACE_ARGS_MAX],
                    )
            elif et == "message_delta":
                logger.info(
                    "converter trace downstream[%s]: message_delta stop_reason=%s",
                    tag,
                    (evt.get("delta") or {}).get("stop_reason"),
                )
            elif et == "error":
                logger.info(
                    "converter trace downstream[%s]: ERROR event=%s", tag, evt.get("error")
                )
        except Exception:
            logger.exception("converter trace: downstream event inspect failed")
        yield evt


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


def _model_alias_root(prefix: str) -> str:
    """The bare vendor word of an alias prefix — ``claude/`` → ``claude``.

    Used to recognize models that already look like the aliased vendor, so
    ``claude-sonnet-4`` (a real Anthropic model proxied by LiteLLM) is not
    advertised a second time as ``claude/claude-sonnet-4``.
    """
    return prefix.rstrip("/").lower()


def _fill_model_entry(entry: dict, model_id: object) -> dict:
    """Give one listing entry the union of the OpenAI and Anthropic model schemas.

    The same listing is read by OpenAI-shaped clients (``object``, ``created``,
    ``owned_by``) and Anthropic-shaped ones (``type``, ``display_name``,
    ``created_at``), and neither tolerates the other's shape well. Filling both
    means one response satisfies either parser.

    Upstream fields win: only missing keys are added. ``owned_by`` defaults to
    the gateway's name (``litellm`` or ``bifrost``), since neither the gateway
    nor a self-hosted backend has a better owner to name. ``created_at`` is
    derived from ``created`` and both are left absent when upstream sent no
    timestamp — a fabricated date is worse than a missing optional field.
    """
    entry["id"] = model_id
    entry.setdefault("object", "model")
    entry.setdefault("type", "model")
    entry.setdefault("owned_by", settings.gateway)
    if isinstance(model_id, str):
        entry.setdefault("display_name", model_id)
    created = entry.get("created")
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        try:
            entry.setdefault(
                "created_at",
                datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat(),
            )
        except (OverflowError, OSError, ValueError):
            pass
    return entry


def _augment_models_body(body: dict, prefix: str) -> dict:
    """Return ``body`` with alias clones appended and both schemas filled in.

    Non-list ``data`` is left alone: it is not a listing this understands, and
    guessing at it would corrupt whatever it really is.
    """
    data = body.get("data")
    if not isinstance(data, list):
        return body

    root = _model_alias_root(prefix)
    entries: list = []
    for item in data:
        if not isinstance(item, dict):
            entries.append(item)  # unrecognized entry, forwarded untouched
            continue
        model_id = item.get("id")
        entries.append(_fill_model_entry(dict(item), model_id))
        if (
            prefix
            and root
            and isinstance(model_id, str)
            and not model_id.lower().startswith(root)
        ):
            entries.append(_fill_model_entry(dict(item), f"{prefix}{model_id}"))

    augmented = dict(body)
    augmented.setdefault("object", "list")
    augmented["data"] = entries
    # Anthropic's listing is paginated; say so, truthfully, in one page.
    augmented["has_more"] = False
    augmented["first_id"] = entries[0].get("id") if _is_entry(entries, 0) else None
    augmented["last_id"] = entries[-1].get("id") if _is_entry(entries, -1) else None
    return augmented


def _is_entry(entries: list, index: int) -> bool:
    return bool(entries) and isinstance(entries[index], dict)


# --- Model listing -------------------------------------------------------------
# One process-wide cache is correct because the listing does not depend on who
# asks: APISIX injects the same gateway credential on llm-models whichever API
# key called (the LiteLLM master key, or the Bifrost gateway virtual key), so
# every caller would get the same answer from upstream. A credential per caller
# (one Bifrost virtual key per API key, say) would need a cache per credential.

# Top-level fields of a Bifrost listing or error body that describe the gateway's
# own keys rather than the models: ``key_statuses`` names every provider key by
# id and carries each failing backend's raw error, internal host names included;
# ``extra_fields`` holds the routing details. Never forwarded.
_GATEWAY_INTERNAL_FIELDS = ("key_statuses", "extra_fields")


def _clock() -> float:
    """Monotonic seconds for the listing cache; tests replace this, not
    ``time.monotonic``, which the event loop's own timers run on."""
    return time.monotonic()


@dataclass
class _ListingSnapshot:
    upstream_url: str
    gateway: str
    body: dict  # the sanitized upstream listing, before per-request aliasing
    fetched_at: float  # _clock()


@dataclass
class _FetchOutcome:
    """One upstream listing fetch: a ``listing`` to cache and serve, an upstream
    response to forward as it is, or an ``error`` (``timeout``/``unreachable``)."""

    listing: Optional[dict] = None
    status_code: int = 0
    content: bytes = b""
    headers: Optional[dict] = None
    media_type: Optional[str] = None
    error: Optional[str] = None


_listing_snapshot: Optional[_ListingSnapshot] = None
# (event loop, (upstream url, gateway), task) of the fetch in flight. Concurrent
# callers for the same upstream join it instead of each asking; a task is only
# joinable from its own loop.
_listing_fetch: Optional[tuple] = None


def reset_models_cache() -> None:
    """Forget the cached listing and any fetch in flight (used by the tests)."""
    global _listing_snapshot, _listing_fetch
    _listing_snapshot = None
    _listing_fetch = None


def _public_model_id(model_id: object, gateway: str) -> object:
    """The id clients call a listed model by.

    Bifrost lists every model as ``<provider>/<id>``, while clients send the bare
    id, which the provider key's ``models``/``aliases`` resolve — the same names
    LiteLLM listed. Only the provider segment goes: the id itself may contain
    ``/`` (``Qwen/Qwen3.5-32B``).
    """
    if gateway == "bifrost" and isinstance(model_id, str) and "/" in model_id:
        return model_id.split("/", 1)[1]
    return model_id


def _sanitize_listing(data: list, gateway: str) -> dict:
    """The listing rebuilt from ``data`` alone, under the ids clients call."""
    entries: list = []
    seen: set = set()
    for item in data:
        if isinstance(item, dict):
            item = dict(item)
            item["id"] = _public_model_id(item.get("id"), gateway)
            if gateway == "bifrost" and isinstance(item["id"], str):
                # Two providers serving one public name list it twice; Bifrost
                # sends the bare name to one of them either way.
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
        entries.append(item)
    return {"object": "list", "data": entries}


def _without_gateway_internals(body: dict) -> dict:
    return {key: value for key, value in body.items() if key not in _GATEWAY_INTERNAL_FIELDS}


# The one request header a listing fetch carries, per gateway: the credential
# APISIX injected. The fetch and its result are shared by every caller, so
# nothing of the first caller's own may shape them — LiteLLM, for one, prefers a
# client-sent ``x-litellm-api-key`` to ``Authorization``, which would hand the
# other callers that key's narrower listing or its 401.
_LISTING_CREDENTIAL = {"litellm": "authorization", "bifrost": "x-bf-vk"}


def _listing_credentials(headers, gateway: str) -> dict:
    name = _LISTING_CREDENTIAL[gateway]
    value = headers.get(name)
    return {name: value} if value else {}


def _scrub_upstream_error(
    status_code: int, content: bytes, media_type: Optional[str]
) -> bytes:
    """A non-2xx JSON error body forwarded to the client, minus the gateway's
    routing details: Bifrost puts the provider, the provider key's name and the
    backend model id in a top-level ``extra_fields``. Anything else — a 2xx, a
    body that is not JSON, a JSON value that is not an object — is unchanged.
    (``content-length`` never travels with these bodies: it is hop-by-hop.)"""
    if 200 <= status_code < 300 or not (media_type or "").lower().startswith("application/json"):
        return content
    try:
        parsed = json.loads(content)
    except ValueError:
        return content
    if not isinstance(parsed, dict) or not any(key in parsed for key in _GATEWAY_INTERNAL_FIELDS):
        return content
    return json.dumps(_without_gateway_internals(parsed), ensure_ascii=False).encode("utf-8")


async def _fetch_listing(upstream_url: str, headers: dict, gateway: str) -> _FetchOutcome:
    """Ask upstream for its listing once; a listing also becomes the snapshot."""
    global _listing_snapshot
    client = _make_client(settings.request_timeout)
    try:
        upstream_req = client.build_request("GET", f"{upstream_url}/v1/models", headers=headers)
        try:
            upstream = await asyncio.wait_for(
                client.send(upstream_req), timeout=_MODELS_FETCH_CEILING
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            logger.warning("converter models: upstream timed out")
            return _FetchOutcome(error="timeout")
        except httpx.HTTPError as exc:
            logger.warning("converter models: upstream unreachable: %s", exc)
            return _FetchOutcome(error="unreachable")

        content = upstream.content
        media_type = upstream.headers.get("content-type")
        parsed: object = None
        if (media_type or "").lower().startswith("application/json"):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                logger.warning("converter models: upstream JSON body does not parse; forwarding raw")
        # Anything but a 2xx JSON listing — an auth error, an HTML error page, a
        # body that does not parse — is forwarded as it is, so the client sees
        # what really happened upstream; only the gateway's own key details go.
        if (
            200 <= upstream.status_code < 300
            and isinstance(parsed, dict)
            and isinstance(parsed.get("data"), list)
        ):
            listing = _sanitize_listing(parsed["data"], gateway)
            _listing_snapshot = _ListingSnapshot(upstream_url, gateway, listing, _clock())
            return _FetchOutcome(listing=listing)
        resp_headers = forward_response_headers(upstream.headers.items())
        if isinstance(parsed, dict) and any(key in parsed for key in _GATEWAY_INTERNAL_FIELDS):
            content = json.dumps(_without_gateway_internals(parsed), ensure_ascii=False).encode(
                "utf-8"
            )
            resp_headers.pop("content-length", None)
        return _FetchOutcome(
            status_code=upstream.status_code,
            content=content,
            headers=resp_headers,
            media_type=media_type,
        )
    finally:
        await client.aclose()


def _join_listing_fetch(upstream_url: str, headers: dict, gateway: str) -> asyncio.Task:
    """The fetch in flight on this loop, or a new one (single-flight)."""
    global _listing_fetch
    loop = asyncio.get_running_loop()
    target = (upstream_url, gateway)
    if _listing_fetch is not None:
        fetch_loop, fetch_target, task = _listing_fetch
        if fetch_loop is loop and fetch_target == target and not task.done():
            return task
    task = loop.create_task(_fetch_listing(upstream_url, headers, gateway))
    _listing_fetch = (loop, target, task)

    def _finished(done: asyncio.Task) -> None:
        global _listing_fetch
        if _listing_fetch is not None and _listing_fetch[2] is done:
            _listing_fetch = None
        # A caller that stopped waiting never awaits it; retrieve the outcome so
        # an unexpected exception is not reported as never retrieved.
        if not done.cancelled():
            done.exception()

    task.add_done_callback(_finished)
    return task


def _listing_response(listing: dict) -> Response:
    augmented = _augment_models_body(listing, settings.model_alias_prefix)
    return Response(
        content=json.dumps(augmented, ensure_ascii=False).encode("utf-8"),
        status_code=200,
        media_type="application/json",
    )


def _models_error(status_code: int, error_type: str, message: str) -> Response:
    return Response(
        status_code=status_code,
        content=json.dumps(
            {"type": "error", "error": {"type": error_type, "message": message}}
        ).encode("utf-8"),
        media_type="application/json",
    )


@app.get("/v1/models")
async def models(request: Request) -> Response:
    """List the upstream models, each also advertised under the alias prefix.

    Exists so Claude Code can auto-detect models through the gateway: it filters
    the listing for Claude-looking ids, which no deployment name has, so every
    model is advertised a second time as ``claude/<id>``. Those aliased ids are
    callable — ``/v1/messages`` and ``/v1/responses`` strip the prefix back off.
    ``CONVERTER_MODEL_ALIAS_PREFIX=""`` turns the aliasing off.

    The listing is rebuilt from the upstream ``data`` alone (Bifrost ids lose
    their provider segment) and cached for ``CONVERTER_MODELS_CACHE_TTL``.
    Concurrent misses share one upstream fetch, which carries only the gateway
    credential APISIX injected, and a caller waits for it
    ``CONVERTER_MODELS_TIMEOUT`` at most (2s on Bifrost, 30s on LiteLLM): past
    that, or when upstream times out, is unreachable or answers 5xx, the last
    good listing answers while it is younger than ``CONVERTER_MODELS_STALE_MAX``.
    """
    gateway = settings.gateway
    upstream_url = settings.upstream_url
    snapshot = _listing_snapshot
    if snapshot is not None and (
        snapshot.upstream_url != upstream_url or snapshot.gateway != gateway
    ):
        snapshot = None
    ttl = settings.models_cache_ttl
    if snapshot is not None and ttl > 0 and _clock() - snapshot.fetched_at < ttl:
        return _listing_response(snapshot.body)

    task = _join_listing_fetch(
        upstream_url, _listing_credentials(request.headers, gateway), gateway
    )
    wait = settings.models_timeout
    try:
        # Shielded: a caller giving up must not cancel the fetch, which goes on
        # to fill the cache for the next caller.
        outcome = await asyncio.wait_for(asyncio.shield(task), timeout=wait if wait > 0 else None)
    except asyncio.TimeoutError:
        outcome = _FetchOutcome(error="timeout")
    if outcome.listing is not None:
        return _listing_response(outcome.listing)

    transient = outcome.error is not None or outcome.status_code >= 500
    stale_max = settings.models_stale_max
    if snapshot is not None and transient and stale_max > 0:
        age = _clock() - snapshot.fetched_at
        if age <= stale_max:
            logger.warning(
                "converter models: upstream %s; answering with the listing from %.0fs ago",
                outcome.error or f"answered {outcome.status_code}",
                age,
            )
            return _listing_response(snapshot.body)
    if outcome.error == "timeout":
        return _models_error(504, "timeout", "upstream request timed out")
    if outcome.error == "unreachable":
        return _models_error(502, "api_error", "upstream is unreachable")
    return Response(
        content=outcome.content,
        status_code=outcome.status_code,
        headers=outcome.headers,
        media_type=outcome.media_type,
    )


def _strip_model_alias_prefix(parsed: dict) -> None:
    """Strip the advertised alias prefix off an inbound ``model``, in place.

    ``GET /v1/models`` advertises ``{prefix}{id}`` twins of every model, so a
    client that picked one sends it back here and the gateway would not recognize it.
    Stripping before the bridges run means both the outbound body and the
    mid-system model-pattern gate see the deployment's real name.

    A model genuinely registered with a prefixed name would be shadowed by its
    own alias — don't name deployments that way.
    """
    prefix = settings.model_alias_prefix
    if not prefix:
        return
    model = parsed.get("model")
    if isinstance(model, str) and model.startswith(prefix):
        parsed["model"] = model[len(prefix) :]


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Response:
    """Estimate an Anthropic Messages request's input tokens, without upstream.

    Claude Code asks this before sending large requests and to decide when to
    compact. No gateway can count for a self-hosted model (Bifrost's endpoint
    needs the backend's own Anthropic API), so the count is a local, deliberately
    high estimate (``app/token_estimate.py``). The body is the Messages request
    itself; ``messages`` must be present, and a value of the wrong type anywhere
    in it is a 400 like the Messages route's.
    """
    raw = await request.body()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _bad_request("request body is not valid JSON")
    if not isinstance(parsed, dict):
        return _bad_request("request body must be a JSON object")

    _strip_model_alias_prefix(parsed)
    try:
        tokens = estimate_input_tokens(parsed)
    except InvalidCountRequest as exc:
        return _bad_request(str(exc))
    return Response(
        content=json.dumps({"input_tokens": tokens}).encode("utf-8"),
        media_type="application/json",
    )


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

    _strip_model_alias_prefix(parsed)

    _trace_incoming_messages_request(parsed)

    is_stream = bool(parsed.get("stream", False))

    openai_body = anthropic_request_to_openai_body(parsed)
    openai_bytes = json.dumps(openai_body, ensure_ascii=False).encode("utf-8")

    fwd_headers = forward_request_headers(request.headers.items(), settings.gateway)
    fwd_headers["content-type"] = "application/json"

    upstream_url = f"{settings.upstream_url}/v1/chat/completions"
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
            try:
                # Bound the whole non-streaming round-trip. The httpx ``read``
                # timeout is left unbounded for legitimately long completions, so
                # without this a gateway that accepts the connection then stalls
                # the body would pin this worker forever. See
                # ``settings.nonstream_timeout``.
                upstream = await asyncio.wait_for(
                    client.send(upstream_req), timeout=settings.nonstream_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "converter messages: non-streaming upstream timed out after %ss",
                    settings.nonstream_timeout,
                )
                return Response(
                    status_code=504,
                    content=json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "timeout",
                                "message": "upstream request timed out",
                            },
                        }
                    ).encode("utf-8"),
                    media_type="application/json",
                )
            resp_headers = forward_response_headers(upstream.headers.items())
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
                    if isinstance(openai_resp, dict) and openai_resp.get("error"):
                        # Some OpenAI-compatible upstreams return HTTP 200 with an
                        # error-shaped body (no ``choices``). Translating it would
                        # fabricate a successful-looking empty message, hiding the
                        # real failure. Forward it verbatim instead — mirrors the
                        # streaming path's ``chunk.get("error")`` detection. Truthy
                        # check: some providers set ``"error": null`` on normal
                        # bodies.
                        logger.warning(
                            "converter messages: upstream returned %s with an error "
                            "body; forwarding verbatim",
                            upstream.status_code,
                        )
                    elif isinstance(openai_resp, dict):
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
                    resp_headers = forward_response_headers(upstream.headers.items())
            content = _scrub_upstream_error(upstream.status_code, content, media_type)
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
            resp_headers = forward_response_headers(upstream.headers.items())
            content = _scrub_upstream_error(upstream.status_code, content, upstream_ctype)
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
            sanitized_events = sanitize_events(anthropic_events)
            if settings.trace:
                # Trace the FINAL events the SDK receives (post-sanitize), so a
                # malformed tool_use shows up exactly as the consumer sees it.
                sanitized_events = _trace_downstream_events(
                    sanitized_events, str(bridge_model)
                )
            async for sanitized in sanitized_events:
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

    resp_headers = forward_response_headers(upstream.headers.items())
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
    forwards to the gateway, translates the result back to the Responses shape, and
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

    _strip_model_alias_prefix(parsed)

    # Resolved once per request: the streaming path needs the same answer for
    # every item status and the terminal event, and header inspection is only
    # meaningful here (the bridge is HTTP-independent).
    length_as_completed = resolve_length_as_completed(
        settings.length_as_completed, request.headers
    )

    # Codex bundles its client-side tools (multi_agent_v1 sub-agents, image_gen)
    # in a Responses ``namespace`` tool the request path flattens to top-level
    # chat functions. This maps each flattened function back to its namespace so
    # the response paths can re-stamp it onto returned calls for Codex to route.
    # Built from the ORIGINAL request tools (the chat body no longer has them).
    namespace_map = namespace_map_from_tools(parsed.get("tools"))

    is_stream = bool(parsed.get("stream", False))
    store_flag = parsed.get("store", True)
    prev_id = parsed.get("previous_response_id")

    prior_messages = None
    if prev_id is not None:
        if not isinstance(prev_id, str) or not prev_id:
            return _bad_request("previous_response_id must be a non-empty string")
        prior_messages = await _conversation_store_get(prev_id)
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

    fwd_headers = forward_request_headers(request.headers.items(), settings.gateway)
    fwd_headers["content-type"] = "application/json"
    upstream_url = f"{settings.upstream_url}/v1/chat/completions"
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
            try:
                # Bound the whole non-streaming round-trip; see the /v1/messages
                # branch and ``settings.nonstream_timeout``.
                upstream = await asyncio.wait_for(
                    client.send(upstream_req), timeout=settings.nonstream_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "converter responses: non-streaming upstream timed out after %ss",
                    settings.nonstream_timeout,
                )
                return Response(
                    status_code=504,
                    content=json.dumps(
                        {"error": {"type": "timeout", "message": "upstream request timed out"}}
                    ).encode("utf-8"),
                    media_type="application/json",
                )
            resp_headers = forward_response_headers(upstream.headers.items())
            content = upstream.content
            media_type = upstream.headers.get("content-type")
            if (
                200 <= upstream.status_code < 300
                and (media_type or "").lower().startswith("application/json")
            ):
                try:
                    chat = json.loads(content)
                    if isinstance(chat, dict) and chat.get("error"):
                        # HTTP 200 with an error-shaped body (no ``choices``):
                        # forward verbatim rather than fabricating an empty
                        # successful Responses object. Mirrors /v1/messages.
                        logger.warning(
                            "converter responses: upstream returned %s with an error "
                            "body; forwarding verbatim",
                            upstream.status_code,
                        )
                    elif isinstance(chat, dict):
                        resp_obj = chat_response_to_responses_body(
                            chat, parsed, response_id,
                            emit_reasoning=settings.emit_reasoning,
                            length_as_completed=length_as_completed,
                            namespace_map=namespace_map,
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
                                stored = await _conversation_store_put(
                                    response_id, base_messages + [assistant]
                                )
                                # Supersede the chained-from response: a linear
                                # chain only needs the latest transcript, so drop
                                # the parent to keep total memory O(N) not O(N^2).
                                # (Trades away branching off a shared prev id.)
                                if stored and prev_id is not None:
                                    await _conversation_store_delete(prev_id)
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
                    resp_headers = forward_response_headers(upstream.headers.items())
            content = _scrub_upstream_error(upstream.status_code, content, media_type)
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
            resp_headers = forward_response_headers(upstream.headers.items())
            content = _scrub_upstream_error(upstream.status_code, content, upstream_ctype)
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
                length_as_completed=length_as_completed,
                namespace_map=namespace_map,
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
                    stored = await _conversation_store_put(
                        response_id, base_messages + [holder["assistant_message"]]
                    )
                    # Supersede the parent (see non-stream branch) — linear chain
                    # retains only the latest transcript.
                    if stored and prev_id is not None:
                        await _conversation_store_delete(prev_id)
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
                stored = await _conversation_store_put(
                    response_id, base_messages + [holder["assistant_message"]]
                )
                if stored and prev_id is not None:
                    await _conversation_store_delete(prev_id)

    resp_headers = forward_response_headers(upstream.headers.items())
    resp_headers["Cache-Control"] = "no-cache"
    resp_headers["X-Accel-Buffering"] = "no"

    return StreamingResponse(
        with_heartbeat(body_iter(), settings.sse_heartbeat_seconds),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type="text/event-stream",
    )
