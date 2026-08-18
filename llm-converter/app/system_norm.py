"""System-message placement normalization for chat/completions bodies.

Background
----------
Newer Qwen-generation chat templates (Qwen3.5 / Qwen3.6) do not merely ignore a
misplaced system turn — they hard-error on it::

    {"message": "System message must be at the beginning.", ...}   # HTTP 400

The template raises whenever ``messages`` holds a system message anywhere but
index 0, or holds more than one. Both of our inbound shapes routinely produce
exactly that, and Claude Code (2.1.234, verified by live capture) produces both
at once:

* its top-level Anthropic ``system`` arrives as MULTIPLE text blocks, and
* it plants genuine ``role: "system"`` reminder messages INSIDE ``messages[]``
  at non-first positions as a conversation grows.

The converter used to forward both verbatim, so the very first Claude Code
request to a strict backend 400'd before the model saw a token. This module
rewrites *placement* only — every instruction the client sent still reaches the
model, just from a position the template accepts.

The active policy comes from ``CONVERTER_MID_SYSTEM_POLICY``
(:attr:`app.config._Settings.mid_system_policy`); the bridges call
:func:`normalize_system_messages` as the last step of request translation, so
one implementation covers both ``/v1/messages`` and ``/v1/responses``.
"""

from __future__ import annotations

from typing import Any

# Separator between the texts of two MERGED system messages. Each one is an
# independent instruction block, so they need a blank line between them — unlike
# the fragments *within* one message's content, which are pieces of a single
# string and are concatenated bare.
_MERGE_SEPARATOR = "\n\n"


def _content_text(content: Any) -> str:
    """Extract the plain text of one chat message's ``content``.

    Mirrors ``messages_bridge._flatten_text_blocks``: a plain string passes
    through, a structured part array collapses to the concatenation of its
    ``{type: "text"}`` parts, and anything else (``None``, a number, a dict)
    yields ``""`` rather than a stringified repr — a junk content field must not
    inject garbage into a merged system prompt.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _merge_texts(texts: list[str]) -> str:
    """Join system texts with a blank line, skipping empty ones.

    Empty texts are dropped rather than joined so a content-less system message
    (a junk block, or one whose only parts were images) cannot leave a stray
    blank line in the middle of the merged prompt.
    """
    return _MERGE_SEPARATOR.join(text for text in texts if text)


def _is_system(message: Any) -> bool:
    """True for a dict message whose ``role`` is ``system``.

    Defensive on both counts: the bridges emit well-formed dicts, but a
    transcript replayed from the ``previous_response_id`` store is only as good
    as what was persisted, and a non-dict entry must not raise here.
    """
    return isinstance(message, dict) and message.get("role") == "system"


def _hoist(messages: list[dict]) -> list[dict]:
    """Collect every system message into one leading system message."""
    texts: list[str] = []
    rest: list[dict] = []
    for message in messages:
        if _is_system(message):
            texts.append(_content_text(message.get("content")))
        else:
            rest.append(message)
    if not texts:
        # ``texts`` gains an entry per system message (even an empty one), so an
        # empty list means there was no system message at all — nothing to do.
        return messages
    return [{"role": "system", "content": _merge_texts(texts)}, *rest]


def _demote_to_user(messages: list[dict]) -> list[dict]:
    """Merge the leading system run; role-swap every later system to ``user``.

    The leading run collapses into a single head message because the strict
    templates reject a *second* system turn even when it sits at index 1. Later
    system messages keep their content and their position — only ``role``
    changes — so the reminder text still lands at the point in the conversation
    where the client meant it to be read.
    """
    head_run = 0
    for message in messages:
        if not _is_system(message):
            break
        head_run += 1
    tail = messages[head_run:]

    if head_run <= 1 and not any(_is_system(message) for message in tail):
        # Already legal (at most one system, and only at index 0). Return the
        # caller's own list so the common case allocates nothing.
        return messages

    out: list[dict] = []
    if head_run:
        # Copy the first message rather than building a fresh dict so any extra
        # keys a caller attached (e.g. ``name``) survive the merge.
        head = dict(messages[0])
        head["content"] = _merge_texts(
            [_content_text(message.get("content")) for message in messages[:head_run]]
        )
        out.append(head)
    for message in tail:
        if _is_system(message):
            demoted = dict(message)
            demoted["role"] = "user"
            out.append(demoted)
        else:
            out.append(message)
    return out


def normalize_system_messages(messages: list[dict], policy: str) -> list[dict]:
    """Rewrite *messages* so no system turn lands where a strict template 400s.

    Policies:

    * ``"user"`` (default) — merge the leading run of system messages into one
      head message and role-swap any later system message to ``user`` in place.
      Keeps each reminder at its own position in the history, which is where it
      is relevant, at the cost of the model seeing it as user text.
    * ``"hoist"`` — merge the text of ALL system messages into one leading
      system message and drop the originals. Keeps everything at system
      authority, at the cost of moving late reminders far from their context.
    * ``"asis"`` — pass through untouched, for a backend whose template
      tolerates mid-history system turns (or to reproduce the raw client shape
      while debugging). Any unrecognized policy takes the ``"user"`` path,
      matching the config layer's fallback.

    Never mutates *messages* or the dicts inside it: changed messages are
    shallow copies, unchanged ones are shared by reference.

    Idempotent under every policy — ``f(f(x)) == f(x)``. That is load-bearing,
    not incidental: ``/v1/responses`` persists the *normalized* transcript for
    ``previous_response_id`` chaining, so every follow-up turn re-normalizes
    output this function already produced.
    """
    if policy == "asis" or not isinstance(messages, list) or not messages:
        return messages
    if policy == "hoist":
        return _hoist(messages)
    return _demote_to_user(messages)
