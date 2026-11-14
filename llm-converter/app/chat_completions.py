"""The chat/completions side that both bridges share.

``/v1/messages`` and ``/v1/responses`` send the same chat/completions request to
Bifrost and read the same responses back. What they do with that side is kept
here, once, so the two bridges cannot drift apart again:

* :func:`normalize_for_model` — the request reshaping strict chat templates
  need, applied last and gated by the outbound model.
* :func:`read_chat_usage` / :func:`merge_chat_usage` — the usage counters.
* :class:`ToolCallAccumulator` — tool calls assembled from streamed
  ``delta.tool_calls`` fragments.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, NamedTuple, Optional

from .config import settings
from .system_norm import normalize_system_messages
from .tool_norm import normalize_tool_messages

# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def normalize_for_model(messages: list[dict], model: Any) -> list[dict]:
    """Reshape an assembled chat ``messages`` array for strict templates.

    First the placement of system turns (``CONVERTER_MID_SYSTEM_POLICY``, for the
    models ``CONVERTER_MID_SYSTEM_MODEL_PATTERN`` matches), then tool results
    pulled up next to their ``tool_calls`` turn (for the models
    ``CONVERTER_TOOL_NORM_MODEL_PATTERN`` matches): a demoted reminder, or a
    separate user turn, wedged in between is ``user → tool``, which Mistral
    Large 4's template rejects. Run as the last request step, so both see the
    fully assembled array; a model neither pattern matches gets the array back
    untouched.
    """
    return normalize_tool_messages(
        normalize_system_messages(
            messages,
            settings.mid_system_policy,
            model=model,
            model_pattern=settings.mid_system_model_regex,
        ),
        model=model,
        model_pattern=settings.tool_norm_model_regex,
    )


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def token_count(value: Any) -> Optional[int]:
    """An upstream token counter as a non-negative int, or ``None`` when it is
    not a number at all.

    OpenAI-compatible backends were seen sending an int, a float or a numeric
    string here; a float is truncated. ``null``, a bool, NaN/infinity and
    anything else are not counts, so a caller can fall through to an alias
    field. A negative count is 0.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        count = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        count = int(value)
    elif isinstance(value, str):
        try:
            count = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return max(0, count)


def _first_count(*values: Any) -> int:
    for value in values:
        count = token_count(value)
        if count is not None:
            return count
    return 0


class ChatUsage(NamedTuple):
    """chat/completions ``usage``, every counter read the same way for both
    bridges (see :func:`token_count`; a missing one is 0)."""

    #: Inclusive: cache reads AND cache writes are part of it.
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int
    #: The cache-write TTL breakdown (``ephemeral_5m_input_tokens`` /
    #: ``ephemeral_1h_input_tokens``), when the upstream sent one.
    cache_creation: Optional[dict]


def read_chat_usage(usage: Any) -> ChatUsage:
    """Read a chat/completions ``usage`` object.

    The cache fields come under several names. ``prompt_tokens_details`` is
    preferred (``cached_tokens``; ``cache_creation_tokens`` or
    ``cache_write_tokens``), then the top-level Anthropic-style
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens``. They are
    aliases of one count, never added together.
    """
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}

    creation = details.get("cache_creation_token_details")
    if not isinstance(creation, dict):
        creation = usage.get("cache_creation")
    breakdown = (
        {
            key: _first_count(creation.get(key))
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        }
        if isinstance(creation, dict)
        else None
    )
    return ChatUsage(
        prompt_tokens=_first_count(usage.get("prompt_tokens")),
        completion_tokens=_first_count(usage.get("completion_tokens")),
        total_tokens=_first_count(usage.get("total_tokens")),
        cache_read_tokens=_first_count(
            details.get("cached_tokens"), usage.get("cache_read_input_tokens")
        ),
        cache_creation_tokens=_first_count(
            details.get("cache_creation_tokens"),
            details.get("cache_write_tokens"),
            usage.get("cache_creation_input_tokens"),
        ),
        reasoning_tokens=_first_count(completion_details.get("reasoning_tokens")),
        cache_creation=breakdown,
    )


def merge_chat_usage(merged: dict, usage: dict) -> None:
    """Fold one stream chunk's ``usage`` into *merged*, field by field.

    Counts are cumulative, not increments, so a field a later chunk carries
    replaces the earlier value. A field it leaves out, or sends as ``null``,
    keeps the earlier one: a terminal usage chunk that has only the output
    counts must not zero the prompt count. Nested details merge the same way,
    one level down.
    """
    for key, value in usage.items():
        if isinstance(value, dict):
            previous = merged.get(key)
            merged[key] = {
                **(previous if isinstance(previous, dict) else {}),
                **{k: v for k, v in value.items() if v is not None},
            }
        elif value is not None:
            merged[key] = value


# ---------------------------------------------------------------------------
# Streamed tool calls
# ---------------------------------------------------------------------------


def is_complete_json(value: object) -> bool:
    """True when *value* is a string that parses as a whole JSON value."""
    if not isinstance(value, str):
        return False
    try:
        json.loads(value)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


# Inside a JSON string only a quote or a backslash changes anything.
_STRING_SPECIAL = re.compile(r'["\\]')
_JSON_WHITESPACE = " \t\n\r"


class _JsonShape:
    """Follows text as it is appended, far enough to rule out that it is one
    complete JSON value: an open string or container, a closer with no opener,
    or anything but whitespace after the value ended. When it cannot rule that
    out, :attr:`may_be_complete` is true and only a real parse can tell.

    Arguments stream in thousands of small fragments; with this a call parses
    what it holds only when the shape allows it is complete (for an object,
    once its closing brace is in), instead of joining and parsing everything on
    every fragment, which made a long call quadratic.
    """

    __slots__ = ("depth", "in_string", "escaped", "started", "ended", "broken")

    def __init__(self) -> None:
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.started = False
        self.ended = False
        self.broken = False

    def feed(self, text: str) -> None:
        i, n = 0, len(text)
        while i < n and not self.broken:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                    i += 1
                    continue
                match = _STRING_SPECIAL.search(text, i)
                if match is None:
                    return
                i = match.end()
                if match.group() == "\\":
                    self.escaped = True
                else:
                    self.in_string = False
                    self.ended = self.depth == 0
                continue
            ch = text[i]
            i += 1
            if ch in _JSON_WHITESPACE:
                continue
            if self.ended:
                self.broken = True  # a second value after the top-level one
                return
            self.started = True
            if ch == '"':
                self.in_string = True
            elif ch in "{[":
                self.depth += 1
            elif ch in "}]":
                self.depth -= 1
                self.broken = self.depth < 0
                self.ended = self.depth == 0

    @property
    def may_be_complete(self) -> bool:
        return self.started and not (self.broken or self.in_string or self.depth)


class StreamedToolCall:
    """One tool call, as far as its fragments have assembled it."""

    __slots__ = ("upstream_id", "name", "argument_parts", "_shape")

    def __init__(self, call_id: Any, name: Any) -> None:
        # The id the upstream actually sent, ``None`` until one arrives. The
        # bridges synthesize their own id for a call that never gets one, and
        # only this one may be compared against a later fragment's id.
        self.upstream_id: Optional[str] = call_id if _nonempty_str(call_id) else None
        self.name: str = name if _nonempty_str(name) else ""
        self.argument_parts: list[str] = []
        self._shape = _JsonShape()

    @property
    def arguments(self) -> str:
        return "".join(self.argument_parts)

    def _append(self, fragment: str) -> None:
        self.argument_parts.append(fragment)
        self._shape.feed(fragment)

    def _replace(self, arguments: str) -> None:
        self.argument_parts = [arguments]
        self._shape = _JsonShape()
        self._shape.feed(arguments)

    def _holds_complete_json(self) -> bool:
        return self._shape.may_be_complete and is_complete_json(self.arguments)


def _is_another_call(call: StreamedToolCall, call_id: Any, name: Any) -> bool:
    """Whether a fragment on *call*'s index belongs to a different call."""
    if _nonempty_str(call_id) and call.upstream_id is not None:
        return call_id != call.upstream_id
    return bool(call.name) and _nonempty_str(name) and name != call.name


class ToolCallFragment(NamedTuple):
    """What one ``delta.tool_calls`` entry did."""

    call: StreamedToolCall
    #: The entry opened :attr:`call`.
    opened: bool
    #: The call that held this entry's index until another call (a different
    #: id, or a different name) took it over. It is complete; the bridge closes
    #: it before opening :attr:`call`.
    superseded: Optional[StreamedToolCall]
    #: The argument text :attr:`call` took from the entry, ``None`` when the
    #: entry carried none or only repeated what the call already had.
    arguments: Optional[str]


class ToolCallAccumulator:
    """Assemble tool calls from streamed chat/completions ``delta.tool_calls``.

    The upstreams behind Bifrost do not all stream tool calls the OpenAI way.
    Three dialects were seen in the field:

    * A continuation or finish fragment without ``index`` (sglang #5661,
      vLLM's stripped finish chunk #31437). It belongs to the most recent call,
      or the first one when there is none yet; dropping it truncated the
      arguments.
    * One ``index`` reused for two distinct sequential calls (vLLM/GLM,
      #31437). A non-empty ``id`` different from the one the open call was
      given upstream starts a new call; folding it in would overwrite the first
      call and lose it. The comparison is against the id the upstream really
      sent, never a synthesized one, so a real id arriving on a later fragment
      of a call opened without one is that call's metadata. When there are not
      two ids to compare, a name different from the one the open call already
      has starts a new call too, rather than giving one tool the other's
      arguments. A name arriving for a call that has none is adopted; with the
      same id on both, the call keeps its first name.
    * Complete arguments sent in one delta and then sent again whole on the
      finish chunk (vLLM/GLM, #31437). When what the call holds already parses
      as JSON and the fragment does too, it is a restatement, not a
      continuation; appending it would hand the client ``{...}{...}``. Partial
      fragments never both parse (``{"command":`` does not), so canonical
      streaming is unaffected.

    What a restatement does depends on when the bridge sends arguments out.
    ``/v1/messages`` holds every call until it is complete, so a restatement
    with a different value replaces the arguments
    (``replace_restated=True``). ``/v1/responses`` streams each fragment to the
    client as it arrives and cannot take one back, so it only drops a
    restatement identical to what it already sent; a different one is appended
    as before.
    """

    def __init__(self, *, replace_restated: bool) -> None:
        self._replace_restated = replace_restated
        self._calls: list[StreamedToolCall] = []
        self._open: dict[int, StreamedToolCall] = {}
        self._last_index: Optional[int] = None

    def add(self, entry: dict) -> ToolCallFragment:
        """Record one ``delta.tool_calls`` entry."""
        index = entry.get("index")
        if not isinstance(index, int):
            index = self._last_index if self._last_index is not None else 0
        self._last_index = index
        fn = entry.get("function")
        fn = fn if isinstance(fn, dict) else {}
        call_id = entry.get("id")
        name = fn.get("name")

        call = self._open.get(index)
        superseded = None
        if call is not None and _is_another_call(call, call_id, name):
            superseded, call = call, None
        opened = call is None
        if call is None:
            call = StreamedToolCall(call_id, name)
            self._calls.append(call)
            self._open[index] = call
        else:
            if call.upstream_id is None and _nonempty_str(call_id):
                call.upstream_id = call_id
            if not call.name and _nonempty_str(name):
                call.name = name
        return ToolCallFragment(call, opened, superseded, self._take_arguments(call, fn))

    def _take_arguments(self, call: StreamedToolCall, fn: dict) -> Optional[str]:
        fragment = fn.get("arguments")
        if not _nonempty_str(fragment):
            return None
        # Cheapest test first: the shape of what is held rules a restatement
        # out on almost every fragment of a call still being streamed, then the
        # fragment, then the full parse of what is held.
        if (
            call._shape.may_be_complete
            and is_complete_json(fragment)
            and call._holds_complete_json()
        ):
            if fragment == call.arguments:
                return None
            if self._replace_restated:
                call._replace(fragment)
                return fragment
        call._append(fragment)
        return fragment

    def take(self) -> list[StreamedToolCall]:
        """Every call since the last take, in arrival order, and start afresh.

        Arrival order is index order for a conformant upstream and keeps two
        calls that shared one index in the order the model asked for them.
        After a take a fragment opens a new call even on an index seen before;
        the last index is kept, so an index-less fragment still lands on it.
        """
        calls = self._calls
        self._calls = []
        self._open.clear()
        return calls
