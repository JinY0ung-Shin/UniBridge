"""OpenAI Responses API ↔ Chat Completions translation.

Pure, HTTP-independent translation between the Responses API (/v1/responses) and
the Chat Completions API (/v1/chat/completions), in both directions:

* :func:`responses_request_to_chat_body` — Responses request → chat request.
* :func:`chat_response_to_responses_body` — non-streaming chat response → Responses object.
* :func:`chat_stream_to_responses_events` — chat SSE chunk stream → Responses SSE event dicts.

Field/shape rules follow the OpenAI Python SDK type definitions (verified): the
``object`` is ``"response"``; usage keys are ``input_tokens``/``output_tokens``/
``total_tokens``; a function_call item carries BOTH ``id`` (synthesized ``fc_…``)
and ``call_id`` (the upstream tool-call id); ``output_text`` is an SDK-derived
convenience, never a wire field. The streaming event names and ordering match
the ResponseStreamEvent union.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator, Optional

# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _new_fc_id() -> str:
    return f"fc_{uuid.uuid4().hex[:24]}"


def _new_reasoning_id() -> str:
    return f"rs_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# Request: Responses → Chat Completions
# ---------------------------------------------------------------------------


def _map_role(role: Any) -> str:
    # sglang/vLLM-backed chat endpoints accept system/user/assistant/tool; map
    # the Responses-only ``developer`` role to ``system`` for broad compatibility.
    if role == "developer":
        return "system"
    if role in ("user", "assistant", "system", "tool"):
        return role
    return "user"


def _content_to_chat(content: Any, role: str) -> Any:
    """Translate a Responses message ``content`` to chat content.

    Collapses text parts to a plain string; if image parts are present, returns
    a chat multimodal content array. ``input_text``/``output_text``/``text`` →
    text; ``input_image`` → ``image_url``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    multimodal: list[dict] = []
    has_image = False
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            t = part.get("text")
            if isinstance(t, str):
                text_parts.append(t)
                multimodal.append({"type": "text", "text": t})
        elif ptype == "input_image":
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url:
                # input_image referenced only by file_id has no clean chat
                # equivalent; skip rather than emit image_url:{url:null}.
                continue
            has_image = True
            img: dict[str, Any] = {"url": url}
            detail = part.get("detail")
            if detail and detail != "original":
                img["detail"] = detail
            multimodal.append({"type": "image_url", "image_url": img})
        elif ptype == "refusal":
            t = part.get("refusal")
            if isinstance(t, str):
                text_parts.append(t)

    if has_image:
        return multimodal
    return "".join(text_parts)


def _input_to_messages(input_data: Any) -> list[dict]:
    """Translate the Responses ``input`` (string or item array) to chat messages.

    Consecutive ``function_call`` items (and an immediately-preceding toolless
    assistant ``message``) are coalesced into ONE assistant message carrying all
    their ``tool_calls``. The Chat Completions contract requires every
    ``role:"tool"`` result to sit adjacent to the single assistant-with-tool_calls
    block that issued it; emitting one assistant message per *parallel* call would
    interleave an extra assistant message between a call and its result and get
    the whole follow-up turn rejected (400) upstream.
    """
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if not isinstance(input_data, list):
        return []

    messages: list[dict] = []
    # The assistant message currently accumulating tool_calls, or None. Reset
    # ("flushed") by any item that ends a tool-call run (a message or a tool
    # result), so a later function_call starts a fresh assistant block.
    pending: Optional[dict] = None

    for item in input_data:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype in (None, "message") and "role" in item:
            pending = None
            role = _map_role(item.get("role"))
            messages.append({"role": role, "content": _content_to_chat(item.get("content"), role)})
        elif itype == "function_call":
            args = item.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {}, ensure_ascii=False)
            tool_call = {
                "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                "type": "function",
                "function": {"name": item.get("name") or "", "arguments": args},
            }
            if pending is None:
                # Fold into an immediately-preceding toolless assistant message
                # (same-turn text + tool calls) when present; else open a new one.
                if (
                    messages
                    and messages[-1].get("role") == "assistant"
                    and "tool_calls" not in messages[-1]
                ):
                    pending = messages[-1]
                    pending["tool_calls"] = [tool_call]
                else:
                    pending = {"role": "assistant", "content": "", "tool_calls": [tool_call]}
                    messages.append(pending)
            else:
                pending["tool_calls"].append(tool_call)
        elif itype == "function_call_output":
            pending = None
            out = item.get("output", "")
            if isinstance(out, list):
                # Responses allows output as an array of content parts; collapse
                # the text parts rather than serializing the raw structure.
                out = "".join(
                    p.get("text", "")
                    for p in out
                    if isinstance(p, dict) and p.get("type") in ("output_text", "text", "input_text")
                )
            elif not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            messages.append(
                {"role": "tool", "tool_call_id": item.get("call_id") or "", "content": out}
            )
        # reasoning items and unknown types are dropped on the request path.
    return messages


def _tools_to_chat(tools: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") != "function":
            # Built-in tools (web_search, file_search, …) have no chat equivalent.
            continue
        # Responses uses internally-tagged ({type, name, ...}); accept the
        # already-nested chat form too.
        if isinstance(t.get("function"), dict):
            fn = t["function"]
        else:
            fn = {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
            }
        if not fn.get("name"):
            continue
        chat_fn: dict[str, Any] = {
            "name": fn.get("name"),
            "description": fn.get("description", "") or "",
            "parameters": fn.get("parameters", {}) or {},
        }
        # Responses function tools are strict by default; preserve an explicit
        # strict flag (from the nested or flat form) so structured-output
        # guarantees the caller asked for survive the reshape.
        strict = fn.get("strict")
        if strict is None and isinstance(t.get("strict"), bool):
            strict = t.get("strict")
        if strict is not None:
            chat_fn["strict"] = strict
        out.append({"type": "function", "function": chat_fn})
    return out


def _tool_choice_to_chat(tc: Any) -> Any:
    if tc is None:
        return None
    if isinstance(tc, str):
        return tc if tc in ("auto", "none", "required") else None
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "function":
            name = tc.get("name") or (tc.get("function") or {}).get("name")
            if name:
                return {"type": "function", "function": {"name": name}}
        if ttype in ("auto", "none", "required"):
            return ttype
    return None


def _text_format_to_response_format(text: Any) -> Optional[dict]:
    if not isinstance(text, dict):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None
    ftype = fmt.get("type")
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        schema: dict[str, Any] = {"name": fmt.get("name")}
        if "schema" in fmt:
            schema["schema"] = fmt.get("schema")
        if "strict" in fmt:
            schema["strict"] = fmt.get("strict")
        return {"type": "json_schema", "json_schema": schema}
    # "text" (the default) → omit response_format entirely.
    return None


def responses_request_to_chat_body(
    body: dict, prior_messages: Optional[list[dict]] = None
) -> dict:
    """Translate a Responses request body to a Chat Completions request body.

    ``prior_messages`` (resolved from ``previous_response_id``) is prepended. The
    chain already carries the original turn's system message, but OpenAI allows
    a new ``instructions`` on a follow-up to apply to the current turn — so when
    both are present the new instructions is appended as a system message ahead
    of this turn's input.
    """
    out: dict[str, Any] = {}
    if body.get("model") is not None:
        out["model"] = body["model"]

    messages: list[dict] = []
    if prior_messages:
        messages.extend(prior_messages)
        if body.get("instructions"):
            messages.append({"role": "system", "content": body["instructions"]})
    elif body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    messages.extend(_input_to_messages(body.get("input", [])))
    out["messages"] = messages

    if "max_output_tokens" in body and body["max_output_tokens"] is not None:
        out["max_completion_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p", "parallel_tool_calls", "metadata"):
        if k in body and body[k] is not None:
            out[k] = body[k]
    if "stream" in body:
        out["stream"] = bool(body["stream"])

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = reasoning["effort"]

    user = body.get("user") or body.get("safety_identifier")
    if user:
        out["user"] = user

    tools = _tools_to_chat(body.get("tools"))
    if tools:
        out["tools"] = tools
    tc = _tool_choice_to_chat(body.get("tool_choice"))
    if tc is not None:
        out["tool_choice"] = tc
    rf = _text_format_to_response_format(body.get("text"))
    if rf is not None:
        out["response_format"] = rf

    if out.get("stream"):
        so = out.get("stream_options") or {}
        if isinstance(so, dict):
            so.setdefault("include_usage", True)
        out["stream_options"] = so

    return out


# ---------------------------------------------------------------------------
# Shared response-object construction
# ---------------------------------------------------------------------------


def _usage_to_responses(usage: Any) -> Optional[dict]:
    if not isinstance(usage, dict):
        return None
    pd = usage.get("prompt_tokens_details") or {}
    cd = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "input_tokens_details": {"cached_tokens": pd.get("cached_tokens", 0) if isinstance(pd, dict) else 0},
        "output_tokens": usage.get("completion_tokens", 0),
        "output_tokens_details": {"reasoning_tokens": cd.get("reasoning_tokens", 0) if isinstance(cd, dict) else 0},
        "total_tokens": usage.get("total_tokens", 0),
    }


def _finish_to_status(finish: Any) -> tuple[str, Optional[dict]]:
    if finish == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    if finish == "content_filter":
        return "incomplete", {"reason": "content_filter"}
    # stop / tool_calls / function_call / None → completed
    return "completed", None


def _build_response_object(
    *,
    request_body: dict,
    model: str,
    created_at: int,
    response_id: str,
    status: str,
    output: list[dict],
    usage: Optional[dict],
    incomplete_details: Optional[dict] = None,
    error: Optional[dict] = None,
) -> dict:
    """Assemble a Responses ``response`` object, echoing inbound request params."""
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": error,
        "incomplete_details": incomplete_details,
        "instructions": request_body.get("instructions"),
        "max_output_tokens": request_body.get("max_output_tokens"),
        "model": model,
        "output": output,
        "parallel_tool_calls": request_body.get("parallel_tool_calls", True),
        "previous_response_id": request_body.get("previous_response_id"),
        "reasoning": request_body.get("reasoning") or {"effort": None, "summary": None},
        "store": request_body.get("store", True),
        "temperature": request_body.get("temperature"),
        "text": request_body.get("text") or {"format": {"type": "text"}},
        "tool_choice": request_body.get("tool_choice", "auto"),
        "tools": request_body.get("tools", []) or [],
        "top_p": request_body.get("top_p"),
        "truncation": request_body.get("truncation", "disabled"),
        "usage": usage,
        "metadata": request_body.get("metadata") or {},
    }


def assistant_message_from_chat(message: dict) -> dict:
    """Build the chat-format assistant message to persist for chaining."""
    content = message.get("content")
    if not content:
        # Fall back to the refusal text so a refusal turn is preserved in the
        # chain (matches the streaming finalize_holder path) instead of storing
        # an empty assistant turn that diverges from what the client received.
        content = message.get("refusal") or ""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    tool_calls = message.get("tool_calls")
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ---------------------------------------------------------------------------
# Non-streaming response: Chat Completions → Responses
# ---------------------------------------------------------------------------


def chat_response_to_responses_body(
    chat: dict,
    request_body: dict,
    response_id: str,
    *,
    emit_reasoning: bool = True,
) -> dict:
    """Translate a non-streaming chat completion into a Responses object."""
    choices = chat.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    status, incomplete = _finish_to_status(finish)
    item_status = "incomplete" if status == "incomplete" else "completed"

    output: list[dict] = []

    # SGLang/legacy vLLM: ``reasoning_content``; renamed vLLM: ``reasoning``.
    reasoning_text = message.get("reasoning_content") or message.get("reasoning")
    if emit_reasoning and isinstance(reasoning_text, str) and reasoning_text:
        output.append(
            {
                "id": _new_reasoning_id(),
                "type": "reasoning",
                "status": item_status,
                "summary": [],
                "content": [{"type": "reasoning_text", "text": reasoning_text}],
            }
        )

    content = message.get("content")
    refusal = message.get("refusal")
    # Keep both as distinct content parts within a SINGLE message item when the
    # upstream returns both (the if/elif here previously dropped the refusal, and
    # diverged from the streaming path). OpenAI permits a refusal part alongside
    # an output_text part in one message item.
    parts: list[dict] = []
    if isinstance(content, str) and content:
        parts.append({"type": "output_text", "text": content, "annotations": []})
    if isinstance(refusal, str) and refusal:
        parts.append({"type": "refusal", "refusal": refusal})
    if parts:
        output.append(
            {
                "id": _new_message_id(),
                "type": "message",
                "status": item_status,
                "role": "assistant",
                "content": parts,
            }
        )

    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        cid = tc.get("id")
        if not cid:
            # Upstream omitted the tool-call id (some local models do). Synthesize
            # one and write it back into the message so the persisted transcript
            # (assistant_message_from_chat) uses the SAME id the client receives,
            # keeping a later function_call_output round-trip resolvable.
            cid = f"call_{uuid.uuid4().hex[:16]}"
            tc["id"] = cid
        output.append(
            {
                "id": _new_fc_id(),
                "type": "function_call",
                "status": item_status,
                "call_id": cid,
                "name": fn.get("name") or "",
                "arguments": args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False),
            }
        )

    created_at = chat.get("created")
    if not isinstance(created_at, int):
        created_at = int(time.time())

    return _build_response_object(
        request_body=request_body,
        model=chat.get("model") or request_body.get("model") or "",
        created_at=created_at,
        response_id=response_id,
        status=status,
        output=output,
        usage=_usage_to_responses(chat.get("usage")),
        incomplete_details=incomplete,
    )


# ---------------------------------------------------------------------------
# Streaming: Chat Completions SSE chunks → Responses SSE events
# ---------------------------------------------------------------------------


def _upstream_error_to_responses(err: Any) -> dict:
    """Map an upstream OpenAI-style error object to a Responses error payload."""
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or "server_error"
        message = err.get("message")
        return {
            "code": str(code),
            "message": message if isinstance(message, str) and message else "upstream error",
        }
    return {"code": "server_error", "message": "upstream error"}


class _StreamState:
    __slots__ = (
        "seq", "next_oi", "output", "usage", "finish",
        "reasoning", "text", "tools", "tool_order",
    )

    def __init__(self) -> None:
        self.seq = 0
        self.next_oi = 0
        self.output: list[dict] = []
        self.usage: Optional[dict] = None
        self.finish: Optional[str] = None
        self.reasoning: Optional[dict] = None  # {id, oi, buf}
        self.text: Optional[dict] = None        # {id, oi, buf}
        self.tools: dict[int, dict] = {}         # upstream tool index -> {id, oi, call_id, name, buf}
        self.tool_order: list[int] = []


async def chat_stream_to_responses_events(
    chunks: AsyncIterator[dict],
    *,
    response_id: str,
    request_body: dict,
    holder: dict,
    emit_reasoning: bool = True,
) -> AsyncIterator[dict]:
    """Convert parsed chat-completions SSE chunks into Responses SSE event dicts.

    ``holder`` is populated on completion with ``assistant_message`` (chat-format,
    for conversation persistence) and ``status``. Every emitted event carries a
    monotonically increasing ``sequence_number`` starting at 0.
    """
    s = _StreamState()
    model = request_body.get("model") or ""
    created_at = int(time.time())

    def bump(payload: dict) -> dict:
        payload["sequence_number"] = s.seq
        s.seq += 1
        return payload

    def lifecycle(etype: str, status: str, output: list[dict], usage: Optional[dict], incomplete=None) -> dict:
        return bump(
            {
                "type": etype,
                "response": _build_response_object(
                    request_body=request_body, model=model, created_at=created_at,
                    response_id=response_id, status=status, output=output, usage=usage,
                    incomplete_details=incomplete,
                ),
            }
        )

    def item_status() -> str:
        # An item still open when the stream truncates inherits the truncation
        # status; items closed earlier (because a new item started) are complete.
        return "incomplete" if _finish_to_status(s.finish)[0] == "incomplete" else "completed"

    def close_reasoning() -> list[dict]:
        if s.reasoning is None:
            return []
        full = "".join(s.reasoning["buf"])
        item = {
            "id": s.reasoning["id"], "type": "reasoning", "status": item_status(),
            "summary": [], "content": [{"type": "reasoning_text", "text": full}],
        }
        evs = [
            bump({"type": "response.reasoning_text.done", "item_id": s.reasoning["id"],
                  "output_index": s.reasoning["oi"], "content_index": 0, "text": full}),
            bump({"type": "response.content_part.done", "item_id": s.reasoning["id"],
                  "output_index": s.reasoning["oi"], "content_index": 0,
                  "part": {"type": "reasoning_text", "text": full}}),
            bump({"type": "response.output_item.done", "output_index": s.reasoning["oi"], "item": item}),
        ]
        s.output.append((s.reasoning["oi"], item))
        s.reasoning = None
        return evs

    def open_message(kind: str) -> list[dict]:
        """Open a message output item (kind 'text' or 'refusal'); returns events."""
        s.text = {"id": _new_message_id(), "oi": s.next_oi, "buf": [], "kind": kind}
        s.next_oi += 1
        part = ({"type": "refusal", "refusal": ""} if kind == "refusal"
                else {"type": "output_text", "text": "", "annotations": []})
        return [
            bump({"type": "response.output_item.added", "output_index": s.text["oi"],
                  "item": {"id": s.text["id"], "type": "message", "status": "in_progress",
                           "role": "assistant", "content": []}}),
            bump({"type": "response.content_part.added", "item_id": s.text["id"],
                  "output_index": s.text["oi"], "content_index": 0, "part": part}),
        ]

    def close_text() -> list[dict]:
        if s.text is None:
            return []
        full = "".join(s.text["buf"])
        oi = s.text["oi"]
        if s.text["kind"] == "refusal":
            part = {"type": "refusal", "refusal": full}
            done_evt = {"type": "response.refusal.done", "item_id": s.text["id"],
                        "output_index": oi, "content_index": 0, "refusal": full}
        else:
            part = {"type": "output_text", "text": full, "annotations": []}
            done_evt = {"type": "response.output_text.done", "item_id": s.text["id"],
                        "output_index": oi, "content_index": 0, "text": full}
        item = {"id": s.text["id"], "type": "message", "status": item_status(),
                "role": "assistant", "content": [part]}
        evs = [
            bump(done_evt),
            bump({"type": "response.content_part.done", "item_id": s.text["id"],
                  "output_index": oi, "content_index": 0, "part": part}),
            bump({"type": "response.output_item.done", "output_index": oi, "item": item}),
        ]
        s.output.append((oi, item))
        s.text = None
        return evs

    def close_tool(idx: int) -> list[dict]:
        t = s.tools.get(idx)
        if t is None:
            return []
        args = "".join(t["buf"])
        item = {
            "id": t["id"], "type": "function_call", "status": item_status(),
            "call_id": t["call_id"], "name": t["name"], "arguments": args,
        }
        evs = [
            bump({"type": "response.function_call_arguments.done", "item_id": t["id"],
                  "output_index": t["oi"], "name": t["name"], "arguments": args}),
            bump({"type": "response.output_item.done", "output_index": t["oi"], "item": item}),
        ]
        s.output.append((t["oi"], item))
        del s.tools[idx]
        return evs

    def close_all_tools() -> list[dict]:
        """Close every still-open tool item (in open order)."""
        evs: list[dict] = []
        for idx in list(s.tool_order):
            evs.extend(close_tool(idx))
        return evs

    def finalize_holder(items: list[dict], status: str) -> None:
        """Populate ``holder`` with the chat-format assistant message for persistence.

        Left unset when the turn produced no real output, so the route skips
        persisting an empty assistant turn.
        """
        text_full = "".join(
            (p.get("text") or "")
            for it in items if it.get("type") == "message"
            for p in it.get("content", [])
        )
        if not text_full:
            # Pure-refusal turn: persist the refusal text so the chain reflects
            # it. When real output_text exists we do NOT blend the refusal into
            # it (which previously produced e.g. "hello"+"nope" -> "hellonope").
            text_full = "".join(
                (p.get("refusal") or "")
                for it in items if it.get("type") == "message"
                for p in it.get("content", [])
            )
        tool_calls = [
            {"id": it["call_id"], "type": "function",
             "function": {"name": it["name"], "arguments": it["arguments"]}}
            for it in items if it.get("type") == "function_call"
        ]
        holder["status"] = status
        if text_full or tool_calls:
            msg: dict[str, Any] = {"role": "assistant", "content": text_full}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            holder["assistant_message"] = msg

    # ── lifecycle preamble ──
    yield lifecycle("response.created", "in_progress", [], None)
    yield lifecycle("response.in_progress", "in_progress", [], None)

    try:
        async for chunk in chunks:
            # Truthy check (not ``is not None``): some OpenAI-compatible providers
            # set ``"error": null`` / ``{}`` on otherwise-normal chunks.
            err = chunk.get("error")
            if err:
                # Upstream streamed an error object instead of choices. Emit a
                # sequence-numbered terminal response.failed (every other event
                # carries sequence_number too) and block the route from
                # persisting a partial turn for this id.
                holder["status"] = "failed"
                holder["assistant_message"] = None
                yield bump(
                    {
                        "type": "response.failed",
                        "response": _build_response_object(
                            request_body=request_body, model=model, created_at=created_at,
                            response_id=response_id, status="failed",
                            output=[it for _, it in sorted(s.output, key=lambda x: x[0])],
                            usage=s.usage, error=_upstream_error_to_responses(err),
                        ),
                    }
                )
                return

            usage = chunk.get("usage")
            if isinstance(usage, dict):
                s.usage = _usage_to_responses(usage)

            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                delta = {}

            # Reasoning (vendor extension — ``reasoning_content`` from
            # SGLang/legacy vLLM, ``reasoning`` after the vLLM rename). Only
            # opened when it genuinely precedes any text/tool output;
            # late-arriving reasoning is dropped so the reasoning→text→tools
            # ordering of output[] is never violated.
            rc = delta.get("reasoning_content") or delta.get("reasoning")
            if emit_reasoning and isinstance(rc, str) and rc:
                if s.reasoning is None and s.text is None and not s.tools:
                    s.reasoning = {"id": _new_reasoning_id(), "oi": s.next_oi, "buf": []}
                    s.next_oi += 1
                    yield bump({"type": "response.output_item.added", "output_index": s.reasoning["oi"],
                                "item": {"id": s.reasoning["id"], "type": "reasoning", "status": "in_progress",
                                         "summary": [], "content": []}})
                    yield bump({"type": "response.content_part.added", "item_id": s.reasoning["id"],
                                "output_index": s.reasoning["oi"], "content_index": 0,
                                "part": {"type": "reasoning_text", "text": ""}})
                if s.reasoning is not None:
                    s.reasoning["buf"].append(rc)
                    yield bump({"type": "response.reasoning_text.delta", "item_id": s.reasoning["id"],
                                "output_index": s.reasoning["oi"], "content_index": 0, "delta": rc})

            # Assistant text.
            content = delta.get("content")
            if isinstance(content, str) and content:
                for e in close_reasoning():
                    yield e
                # Close any open tool item first so item lifecycles never nest
                # (a model that emits a trailing text note after a tool call
                # must not have the message item's added→done sequence wrap an
                # already-open function_call item). Tool args stream
                # contiguously, so an open tool here is already complete.
                for e in close_all_tools():
                    yield e
                if s.text is None:
                    for e in open_message("text"):
                        yield e
                elif s.text["kind"] != "text":
                    for e in close_text():
                        yield e
                    for e in open_message("text"):
                        yield e
                s.text["buf"].append(content)
                yield bump({"type": "response.output_text.delta", "item_id": s.text["id"],
                            "output_index": s.text["oi"], "content_index": 0, "delta": content})

            # Assistant refusal (sglang/vLLM can stream a refusal instead of text).
            refusal = delta.get("refusal")
            if isinstance(refusal, str) and refusal:
                for e in close_reasoning():
                    yield e
                for e in close_all_tools():
                    yield e
                if s.text is None:
                    for e in open_message("refusal"):
                        yield e
                elif s.text["kind"] != "refusal":
                    for e in close_text():
                        yield e
                    for e in open_message("refusal"):
                        yield e
                s.text["buf"].append(refusal)
                yield bump({"type": "response.refusal.delta", "item_id": s.text["id"],
                            "output_index": s.text["oi"], "content_index": 0, "delta": refusal})

            # Tool calls.
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tci = tc.get("index")
                if not isinstance(tci, int):
                    continue
                fn = tc.get("function") or {}
                incoming_id = tc.get("id")
                existing = s.tools.get(tci)
                # Some non-conformant upstreams reuse a single index for distinct
                # sequential tool calls. A delta carrying a NON-EMPTY id that
                # differs from the open item's call_id is a new call, not a
                # continuation — close the current item and open a fresh one so the
                # second call's id/name/arguments are not merged into the first
                # (which would corrupt args into e.g. {"a":1}{"b":2}).
                is_new_call = (
                    existing is not None
                    and isinstance(incoming_id, str)
                    and bool(incoming_id)
                    and incoming_id != existing["call_id"]
                )
                if existing is None or is_new_call:
                    if is_new_call:
                        for e in close_tool(tci):
                            yield e
                    # reasoning/text precede tool calls — close them first.
                    for e in close_reasoning():
                        yield e
                    for e in close_text():
                        yield e
                    t = {"id": _new_fc_id(), "oi": s.next_oi,
                         "call_id": incoming_id or f"call_{uuid.uuid4().hex[:16]}",
                         "name": fn.get("name") or "", "buf": []}
                    s.next_oi += 1
                    s.tools[tci] = t
                    s.tool_order.append(tci)
                    yield bump({"type": "response.output_item.added", "output_index": t["oi"],
                                "item": {"id": t["id"], "type": "function_call", "status": "in_progress",
                                         "call_id": t["call_id"], "name": t["name"], "arguments": ""}})
                else:
                    t = s.tools[tci]
                    if not t["name"] and fn.get("name"):
                        t["name"] = fn["name"]
                args_frag = fn.get("arguments")
                if isinstance(args_frag, str) and args_frag:
                    t["buf"].append(args_frag)
                    yield bump({"type": "response.function_call_arguments.delta", "item_id": t["id"],
                                "output_index": t["oi"], "delta": args_frag})

            fr = choice.get("finish_reason")
            if isinstance(fr, str) and fr:
                s.finish = fr

        # ── close any open items, then order output[] by output_index ──
        for e in close_reasoning():
            yield e
        for e in close_text():
            yield e
        for idx in list(s.tool_order):
            for e in close_tool(idx):
                yield e

        final_output = [it for _, it in sorted(s.output, key=lambda x: x[0])]
        status, incomplete = _finish_to_status(s.finish)
        # Populate the holder BEFORE the terminal event so the route can persist
        # the transcript before the client (which now has the response id) can
        # fire a follow-up that chains off it.
        finalize_holder(final_output, status)

        terminal_type = "response.incomplete" if status == "incomplete" else "response.completed"
        yield lifecycle(terminal_type, status, final_output, s.usage, incomplete)
    finally:
        # On client cancellation (GeneratorExit) the try-block above may not run
        # its close_* pass, so still-open items never reach s.output. Synthesize
        # them from their buffers — their *.delta content already reached the
        # client — so the persisted transcript reflects what was actually sent,
        # honoring the best-effort partial-state promise.
        if "assistant_message" not in holder:
            pairs = list(s.output)
            if s.text is not None:
                txt = "".join(s.text["buf"])
                part = (
                    {"type": "refusal", "refusal": txt}
                    if s.text["kind"] == "refusal"
                    else {"type": "output_text", "text": txt, "annotations": []}
                )
                pairs.append((s.text["oi"], {"type": "message", "content": [part]}))
            # dict.fromkeys dedupes: an index reused for a new call (close_tool +
            # reopen) leaves it twice in tool_order, and unlike the close_* loop
            # this path has no idempotency guard — without dedup a still-open
            # reused call would be appended (and persisted) twice.
            for idx in dict.fromkeys(s.tool_order):
                t = s.tools.get(idx)
                if t is not None:
                    pairs.append(
                        (
                            t["oi"],
                            {
                                "type": "function_call",
                                "call_id": t["call_id"],
                                "name": t["name"],
                                "arguments": "".join(t["buf"]),
                            },
                        )
                    )
            partial = [it for _, it in sorted(pairs, key=lambda x: x[0])]
            finalize_holder(partial, holder.get("status") or "incomplete")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def previous_response_not_found_body(prev_id: str) -> dict:
    return {
        "error": {
            "type": "invalid_request_error",
            "code": "previous_response_not_found",
            "param": "previous_response_id",
            "message": f"Previous response with id '{prev_id}' not found.",
        }
    }
