"""Local input-token estimate for ``POST /v1/messages/count_tokens``.

Neither gateway offers a token count for a self-hosted chat model: Bifrost's
Anthropic ``count_tokens`` needs the backend's own Anthropic endpoint (a custom
OpenAI-compatible provider answers 404), and LiteLLM's falls back to a generic
tokenizer anyway. So the converter answers from the request alone, without an
upstream call and without a tokenizer (this image ships none, and fetching one
is impossible offline).

Clients use the count to decide when to compact a conversation, so the estimate
errs high: an early compaction costs a little context, a late one a failed turn.

The request is checked as it is counted; a value of the wrong type raises
:class:`InvalidCountRequest`, which the route answers with a 400.
"""

from __future__ import annotations

import json
import math
from typing import Any

# BPE tokenizers average about 4 characters per token on English prose, but code,
# JSON tool schemas and identifiers split finer: Qwen3's tokenizer measured an
# estimate based on 4 at 0.77-0.87 of the real count. 3 per token keeps it high.
_ASCII_CHARS_PER_TOKEN = 3
# Hangul, CJK and most other non-ASCII scripts often cost a token per character
# (or more) in English-centric vocabularies; one per character keeps the
# estimate on the high side without a tokenizer. Applied per character.
_NON_ASCII_TOKENS_PER_CHAR = 1

# An image costs what the backend's vision encoder makes of it, which the base64
# payload says nothing about without decoding it. Anthropic prices an unresized
# image at up to ~1,600 tokens (about 1.15 megapixels / 750), and the Qwen-VL
# family lands in the same range for a ~1-megapixel image (one token per 28x28
# patch), so every image block counts as that ceiling.
IMAGE_BLOCK_TOKENS = 1_600
# A base64 or URL document (a PDF) has an unknown page count; a few pages of
# text plus a page image each is the order of magnitude. Text and content
# documents are counted from their text instead.
DOCUMENT_BLOCK_TOKENS = 3_000
# Chat templates wrap every turn in role markers and delimiters
# (``<|im_start|>user\n … <|im_end|>\n`` is about five tokens).
MESSAGE_OVERHEAD_TOKENS = 5
# Each tool definition is rendered into the prompt with some wrapping on top of
# its name, description and schema.
TOOL_OVERHEAD_TOKENS = 10


class InvalidCountRequest(ValueError):
    """The body does not have the Messages request shape; ``str()`` says where."""


def text_tokens(text: str) -> int:
    """Tokens for ``text``: ~3 ASCII characters per token, 1 per other character."""
    if not text:
        return 0
    ascii_chars = len(text.encode("ascii", "ignore"))
    other_chars = len(text) - ascii_chars
    return math.ceil(ascii_chars / _ASCII_CHARS_PER_TOKEN) + other_chars * _NON_ASCII_TOKENS_PER_CHAR


def _json_tokens(value: Any) -> int:
    return text_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise InvalidCountRequest(f"{where}: expected a string")
    return value


def _optional_string(value: Any, where: str) -> str:
    return "" if value is None else _string(value, where)


def _content_tokens(content: Any, where: str) -> int:
    """Tokens for a message's (or tool result's) ``content``: a string or blocks."""
    if isinstance(content, str):
        return text_tokens(content)
    if isinstance(content, list):
        return sum(_block_tokens(block, f"{where}.{i}") for i, block in enumerate(content))
    raise InvalidCountRequest(f"{where}: expected a string or an array of content blocks")


def _document_tokens(block: dict, where: str) -> int:
    source = block.get("source")
    if source is None:
        return DOCUMENT_BLOCK_TOKENS
    if not isinstance(source, dict):
        raise InvalidCountRequest(f"{where}.source: expected an object")
    if source.get("type") == "text":
        return text_tokens(_string(source.get("data"), f"{where}.source.data"))
    if source.get("type") == "content":
        return _content_tokens(source.get("content"), f"{where}.source.content")
    return DOCUMENT_BLOCK_TOKENS


def _block_tokens(block: Any, where: str) -> int:
    if not isinstance(block, dict):
        raise InvalidCountRequest(f"{where}: expected a content block object")
    btype = _string(block.get("type"), f"{where}.type")
    if btype == "text":
        return text_tokens(_string(block.get("text"), f"{where}.text"))
    if btype == "thinking":
        # The signature is an opaque integrity blob, not prompt text.
        return text_tokens(_optional_string(block.get("thinking"), f"{where}.thinking"))
    if btype == "redacted_thinking":
        return 0
    if btype in ("tool_use", "server_tool_use"):
        tool_input = block.get("input")
        if tool_input is not None and not isinstance(tool_input, dict):
            raise InvalidCountRequest(f"{where}.input: expected an object")
        return text_tokens(_optional_string(block.get("name"), f"{where}.name")) + _json_tokens(
            tool_input or {}
        )
    if btype == "tool_result":
        content = block.get("content")
        return 0 if content is None else _content_tokens(content, f"{where}.content")
    if btype == "image":
        return IMAGE_BLOCK_TOKENS
    if btype == "document":
        return _document_tokens(block, where)
    # Anything else (search results, web search output, …) counts as its JSON,
    # which over- rather than under-counts.
    return _json_tokens(block)


def _message_tokens(message: Any, where: str) -> int:
    if not isinstance(message, dict):
        raise InvalidCountRequest(f"{where}: expected a message object")
    _string(message.get("role"), f"{where}.role")
    if "content" not in message:
        raise InvalidCountRequest(f"{where}.content: field required")
    return MESSAGE_OVERHEAD_TOKENS + _content_tokens(message["content"], f"{where}.content")


def _tool_tokens(tool: Any, where: str) -> int:
    if not isinstance(tool, dict):
        raise InvalidCountRequest(f"{where}: expected a tool object")
    tool_type = _optional_string(tool.get("type"), f"{where}.type")
    if "input_schema" in tool or tool_type in ("", "custom"):
        schema = tool.get("input_schema")
        if schema is not None and not isinstance(schema, dict):
            raise InvalidCountRequest(f"{where}.input_schema: expected an object")
        return (
            TOOL_OVERHEAD_TOKENS
            + text_tokens(_optional_string(tool.get("name"), f"{where}.name"))
            + text_tokens(_optional_string(tool.get("description"), f"{where}.description"))
            + _json_tokens(schema or {})
        )
    # A server tool (``web_search_*`` and the like) is described by its config.
    return TOOL_OVERHEAD_TOKENS + _json_tokens(tool)


def estimate_input_tokens(body: dict) -> int:
    """Estimated input tokens of an Anthropic Messages request body.

    Counts ``system`` (a string or text blocks), every message's content (text,
    tool calls, tool results, thinking, images, documents) plus a per-turn
    overhead, and every tool definition. Never less than 1. Raises
    :class:`InvalidCountRequest` for a value of the wrong type.
    """
    total = 0
    system = body.get("system")
    if isinstance(system, str):
        total += text_tokens(system)
    elif isinstance(system, list):
        total += sum(_block_tokens(block, f"system.{i}") for i, block in enumerate(system))
    elif system is not None:
        raise InvalidCountRequest("system: expected a string or an array of text blocks")

    messages = body.get("messages")
    if not isinstance(messages, list):
        raise InvalidCountRequest("messages: field required (an array of messages)")
    total += sum(_message_tokens(message, f"messages.{i}") for i, message in enumerate(messages))

    tools = body.get("tools")
    if isinstance(tools, list):
        total += sum(_tool_tokens(tool, f"tools.{i}") for i, tool in enumerate(tools))
    elif tools is not None:
        raise InvalidCountRequest("tools: expected an array of tools")
    return max(total, 1)
