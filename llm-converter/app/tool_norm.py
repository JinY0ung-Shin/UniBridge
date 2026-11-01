"""Keep tool results adjacent to their ``tool_calls`` turn (Mistral Large 4).

Background
----------
Mistral Large 4's chat template validates role order and raises
``Unexpected role 'tool' after role 'user'`` (a 400 from the backend). Inside
one Anthropic user turn ``messages_bridge`` already emits the ``tool`` messages
first; what still breaks the order is a message sitting *between* an
assistant's ``tool_calls`` and its results across turn boundaries — a
mid-history system reminder placed before the ``tool_result`` turn
(``system_norm`` role-swaps it to ``user``) or a separate user turn sent ahead
of it.

Mistral declined to relax the template (mistralai/Mistral-Large-4-1T-A52B
discussion #5): the model was trained on that order, so requests have to be
reshaped before they reach it. :func:`normalize_tool_messages` moves each
window's tool results to directly after the assistant turn; every other message
follows, in order. Content is never changed or dropped, and a transcript that
already fits comes back untouched.

Empty tool content needs nothing here: the template rejects only a non-string
empty content (``None``/``[]``), the bridges always emit a string, and vLLM
turns a tool message's content into a string before rendering anyway.

Gated on the outbound model name like ``system_norm``
(``CONVERTER_TOOL_NORM_MODEL_PATTERN``, default ``mistral-large-4``), so other
backends keep receiving the client's own shape. Pure: never reads settings.
"""

from __future__ import annotations

import re
from typing import Any


def _role(message: Any) -> Any:
    return message.get("role") if isinstance(message, dict) else None


def _calls_tools(message: Any) -> bool:
    return _role(message) == "assistant" and bool(message.get("tool_calls"))


def normalize_tool_messages(
    messages: list[dict],
    model: Any = None,
    model_pattern: re.Pattern[str] | None = None,
) -> list[dict]:
    """Move tool results up to the assistant ``tool_calls`` turn they answer.

    For every assistant message carrying ``tool_calls``, the window up to the
    next assistant message is rewritten as: all ``tool`` messages of the window
    (in their original order), then every other message (in its original
    order). A window already in that shape is left alone, so a well-formed
    transcript comes back as the caller's own list.

    *model_pattern* gates the rewrite the same way as
    :func:`app.system_norm.normalize_system_messages`: with a pattern, only a
    string *model* the pattern searches successfully is reshaped. ``None``
    normalizes unconditionally (the unit tests' contract).

    Never mutates *messages* or its dicts, and is idempotent — ``/v1/responses``
    persists the translated transcript and re-translates it on every chained turn.
    """
    if model_pattern is not None and not (
        isinstance(model, str) and model_pattern.search(model)
    ):
        return messages
    if not isinstance(messages, list):
        return messages

    out: list[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        message = messages[i]
        out.append(message)
        i += 1
        if not _calls_tools(message):
            continue
        end = i
        while end < n and _role(messages[end]) != "assistant":
            end += 1
        window = messages[i:end]
        out.extend(m for m in window if _role(m) == "tool")
        out.extend(m for m in window if _role(m) != "tool")
        i = end

    if all(a is b for a, b in zip(out, messages)):
        return messages
    return out
