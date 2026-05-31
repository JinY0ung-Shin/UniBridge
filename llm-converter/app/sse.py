"""SSE parsing/serialization and header-filtering helpers for the converter.

Extracted from the upstream proxy route so the conversion-direction logic
(OpenAI ``/v1/chat/completions`` SSE → Anthropic Messages SSE) and the HTTP
plumbing stay testable in isolation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict

import httpx

logger = logging.getLogger(__name__)

# Hop-by-hop headers (RFC 7230 §6.1) plus transport-framing headers set by
# httpx/Starlette automatically. Never forwarded in either direction.
_HOP_BY_HOP = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
)

# Stripped from the request before forwarding to LiteLLM:
# - ``accept-encoding``: httpx negotiates and auto-decompresses internally; if
#   we forwarded the client's value, upstream would compress and we'd decode
#   anyway — wasted upstream CPU.
# NOTE: ``authorization`` is intentionally NOT dropped — APISIX injects the
# LiteLLM master key there via proxy-rewrite, and we must pass it through.
DROP_FROM_REQUEST = _HOP_BY_HOP | frozenset({"accept-encoding"})

# Stripped from the upstream response before returning to the client:
# - ``content-encoding``: httpx returns decoded bytes from ``.content``/
#   ``.aread()``/``.aiter_lines()``, and the SSE branch re-serializes events as
#   plain UTF-8. Forwarding the original encoding would mislead the client into
#   decompressing already-decoded data.
DROP_FROM_RESPONSE = _HOP_BY_HOP | frozenset({"content-encoding"})


def filter_headers(items, drop: frozenset) -> Dict[str, str]:
    return {k: v for k, v in items if k.lower() not in drop}


def format_sse(evt: Dict[str, Any]) -> bytes:
    """Serialize an Anthropic event dict back into an SSE frame."""
    etype = evt.get("type", "message")
    data = json.dumps(evt, separators=(",", ":"), ensure_ascii=False)
    return f"event: {etype}\ndata: {data}\n\n".encode("utf-8")


async def iter_openai_sse_chunks(
    response: httpx.Response,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield parsed chunk dicts from an OpenAI ``/v1/chat/completions`` SSE.

    OpenAI's wire format is the spec-minimal SSE: each frame is one
    ``data: <json>\\n\\n`` block. The terminal ``data: [DONE]`` sentinel
    signals end-of-stream and carries no payload to forward.
    """
    data_lines: list[str] = []
    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            data_lines = []
            if payload.strip() == "[DONE]":
                continue
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("bridge: skipping non-JSON OpenAI SSE payload: %r", payload[:200])
                continue
            if isinstance(evt, dict):
                yield evt
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        payload = "\n".join(data_lines)
        if payload.strip() == "[DONE]":
            return
        try:
            evt = json.loads(payload)
            if isinstance(evt, dict):
                yield evt
        except json.JSONDecodeError:
            logger.warning("bridge: dropping trailing non-JSON OpenAI SSE payload")
