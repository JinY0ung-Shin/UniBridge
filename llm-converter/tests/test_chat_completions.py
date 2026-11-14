"""The chat/completions reading both bridges share (``app.chat_completions``).

Each case runs through BOTH bridges: they once had a copy each, and fixes
landed in one of them only (62daeae gave ``/v1/messages`` the vLLM/GLM and
sglang tool-call dialects; ``/v1/responses`` kept truncating and duplicating
arguments, and overwrote stream usage chunk by chunk).
"""

from __future__ import annotations

import json
import math

import pytest

from app import chat_completions
from app.chat_completions import (
    ToolCallAccumulator,
    _JsonShape,
    is_complete_json,
    read_chat_usage,
    token_count,
)
from app.messages_bridge import (
    openai_response_to_anthropic_body,
    openai_stream_to_anthropic_events,
)
from app.responses_bridge import (
    chat_response_to_responses_body,
    chat_stream_to_responses_events,
)
from tests.helpers import as_async, collect


async def _messages_tool_calls(chunks: list[dict]) -> list[tuple[str, str, str]]:
    """(id, name, arguments) of every tool_use block ``/v1/messages`` streams."""
    events = await collect(
        openai_stream_to_anthropic_events(as_async(json.loads(json.dumps(chunks))), model="m")
    )
    blocks: dict[int, list] = {}
    for event in events:
        if event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
            block = event["content_block"]
            blocks[event["index"]] = [block["id"], block["name"], ""]
        elif (
            event["type"] == "content_block_delta"
            and event["delta"]["type"] == "input_json_delta"
        ):
            blocks[event["index"]][2] += event["delta"]["partial_json"]
    return [tuple(block) for _, block in sorted(blocks.items())]


async def _responses_run(chunks: list[dict]) -> tuple[list[dict], dict]:
    holder: dict = {}
    events = await collect(
        chat_stream_to_responses_events(
            as_async(json.loads(json.dumps(chunks))),
            response_id="resp_x",
            request_body={"model": "m"},
            holder=holder,
        )
    )
    return events, holder


async def _responses_tool_calls(chunks: list[dict]) -> list[tuple[str, str, str]]:
    """(call_id, name, arguments) of every function_call item ``/v1/responses``
    ends with — after checking that the argument deltas the client was sent add
    up to the same arguments, item by item."""
    events, holder = await _responses_run(chunks)
    streamed: dict[str, str] = {}
    for event in events:
        if event["type"] == "response.function_call_arguments.delta":
            streamed[event["item_id"]] = streamed.get(event["item_id"], "") + event["delta"]
    items = [it for it in events[-1]["response"]["output"] if it["type"] == "function_call"]
    for item in items:
        assert streamed.get(item["id"], "") == item["arguments"]
    persisted = [
        (tc["id"], tc["function"]["name"], tc["function"]["arguments"])
        for tc in holder.get("assistant_message", {}).get("tool_calls", [])
    ]
    calls = [(it["call_id"], it["name"], it["arguments"]) for it in items]
    assert persisted == calls
    return calls


def _tool_delta(index: object = 0, *, call_id=None, name=None, arguments=None) -> dict:
    tool_call: dict = {"function": {}}
    if index is not None:
        tool_call["index"] = index
    if call_id is not None:
        tool_call["id"] = call_id
    if name is not None:
        tool_call["function"]["name"] = name
    if arguments is not None:
        tool_call["function"]["arguments"] = arguments
    return {"choices": [{"index": 0, "delta": {"tool_calls": [tool_call]}}]}


_FINISH = {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}

# (case, chunks, the (name, arguments) both bridges must end with)
_DIALECTS = [
    (
        # sglang #5661 / vLLM's stripped finish chunk: no index on the continuation.
        "index-less continuation",
        [
            _tool_delta(call_id="call_1", name="ls", arguments='{"p":'),
            _tool_delta(None, arguments='"/"}'),
            _FINISH,
        ],
        [("ls", '{"p":"/"}')],
    ),
    (
        "index-less fragment before any indexed one",
        [_tool_delta(None, call_id="call_1", name="ls", arguments='{"p":"/"}'), _FINISH],
        [("ls", '{"p":"/"}')],
    ),
    (
        # vLLM/GLM #31437: complete arguments, then the same again on the finish
        # chunk without id or name.
        "identical restatement",
        [
            _tool_delta(call_id="call_1", name="ls", arguments='{"p":"/"}'),
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"p":"/"}'}}]},
                "finish_reason": "tool_calls"}]},
        ],
        [("ls", '{"p":"/"}')],
    ),
    (
        "identical restatement with the id repeated",
        [
            _tool_delta(call_id="call_1", name="ls", arguments='{"p":"/"}'),
            _tool_delta(call_id="call_1", arguments='{"p":"/"}'),
            _FINISH,
        ],
        [("ls", '{"p":"/"}')],
    ),
    (
        "one index reused by two calls",
        [
            _tool_delta(call_id="call_a", name="Read", arguments='{"path":"a"}'),
            _tool_delta(call_id="call_b", name="Bash", arguments='{"command":"ls"}'),
            _FINISH,
        ],
        [("Read", '{"path":"a"}'), ("Bash", '{"command":"ls"}')],
    ),
    (
        "canonical fragments with the id on each",
        [
            _tool_delta(call_id="call_1", name="Bash", arguments='{"comm'),
            _tool_delta(call_id="call_1", arguments='and":'),
            _tool_delta(arguments='"ls"}'),
            _FINISH,
        ],
        [("Bash", '{"command":"ls"}')],
    ),
    (
        "id and name arriving on a later fragment",
        [
            _tool_delta(arguments='{"city":'),
            _tool_delta(call_id="call_1", name="weather", arguments='"Seoul"}'),
            _FINISH,
        ],
        [("weather", '{"city":"Seoul"}')],
    ),
    (
        "two parallel calls interleaved",
        [
            _tool_delta(0, call_id="call_a", name="f", arguments='{"a":'),
            _tool_delta(1, call_id="call_b", name="g", arguments='{"b":'),
            _tool_delta(0, arguments="1}"),
            _tool_delta(1, arguments="2}"),
            _FINISH,
        ],
        [("f", '{"a":1}'), ("g", '{"b":2}')],
    ),
    (
        # The same id ties the fragments to one call, which keeps its first
        # name: /v1/responses has already sent it on output_item.added.
        # (/v1/messages used to take the last.)
        "a different name under the same id",
        [
            _tool_delta(call_id="call_1", name="first", arguments='{"a":'),
            _tool_delta(call_id="call_1", name="second", arguments="1}"),
            _FINISH,
        ],
        [("first", '{"a":1}')],
    ),
    (
        # No ids to compare: a different name is another call, or Read would
        # be handed Bash's arguments (or both, glued together).
        "one index reused by two calls without ids",
        [
            _tool_delta(name="Read", arguments='{"path":"a"}'),
            _tool_delta(name="Bash", arguments='{"command":"ls"}'),
            _FINISH,
        ],
        [("Read", '{"path":"a"}'), ("Bash", '{"command":"ls"}')],
    ),
    (
        "a call without an id, then an id and another name",
        [
            _tool_delta(name="Read", arguments='{"path":'),
            _tool_delta(arguments='"a"}'),
            _tool_delta(call_id="call_b", name="Bash", arguments='{"command":"ls"}'),
            _FINISH,
        ],
        [("Read", '{"path":"a"}'), ("Bash", '{"command":"ls"}')],
    ),
    (
        "the name repeated on every fragment",
        [
            _tool_delta(name="Bash", arguments='{"command":'),
            _tool_delta(name="Bash", arguments='"ls"}'),
            _FINISH,
        ],
        [("Bash", '{"command":"ls"}')],
    ),
]


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [pytest.param(chunks, expected, id=case) for case, chunks, expected in _DIALECTS],
)
async def test_both_bridges_assemble_the_same_tool_calls(chunks, expected):
    messages = await _messages_tool_calls(chunks)
    responses = await _responses_tool_calls(chunks)

    assert [(name, args) for _, name, args in messages] == expected
    assert [(name, args) for _, name, args in responses] == expected
    for _, _, args in messages + responses:
        json.loads(args)


async def test_index_less_fragment_keeps_the_upstream_id_in_both_bridges():
    chunks = [
        _tool_delta(call_id="call_1", name="ls", arguments='{"p":'),
        _tool_delta(None, arguments='"/"}'),
        _FINISH,
    ]
    assert [call_id for call_id, _, _ in await _messages_tool_calls(chunks)] == ["call_1"]
    assert [call_id for call_id, _, _ in await _responses_tool_calls(chunks)] == ["call_1"]


async def test_a_restatement_with_a_new_value_replaces_on_messages_but_appends_on_responses():
    """``/v1/messages`` holds a call until it is complete and can still swap the
    arguments; ``/v1/responses`` has already sent the first value as a delta and
    cannot take it back, so only an identical restatement is dropped there."""
    chunks = [
        _tool_delta(call_id="call_1", name="weather", arguments='{"city":"Seoul"}'),
        _tool_delta(arguments='{"city":"Busan"}'),
        _FINISH,
    ]
    assert await _messages_tool_calls(chunks) == [("call_1", "weather", '{"city":"Busan"}')]
    assert await _responses_tool_calls(chunks) == [
        ("call_1", "weather", '{"city":"Seoul"}{"city":"Busan"}')
    ]


async def test_identical_restatement_sends_no_second_delta_on_responses():
    chunks = [
        _tool_delta(call_id="call_1", name="ls", arguments='{"p":"/"}'),
        _tool_delta(arguments='{"p":"/"}'),
        _FINISH,
    ]
    events, _ = await _responses_run(chunks)
    deltas = [e for e in events if e["type"] == "response.function_call_arguments.delta"]
    assert [d["delta"] for d in deltas] == ['{"p":"/"}']


def test_accumulator_take_starts_afresh_but_keeps_the_last_index():
    acc = ToolCallAccumulator(replace_restated=True)
    acc.add({"index": 3, "id": "call_a", "function": {"name": "f", "arguments": "{}"}})
    [first] = acc.take()
    assert (first.upstream_id, first.name, first.arguments) == ("call_a", "f", "{}")
    assert acc.take() == []

    # Same index after a take: a new call, and an index-less fragment lands on it.
    opened = acc.add({"index": 3, "function": {"arguments": '{"x":'}})
    continued = acc.add({"function": {"arguments": "1}"}})
    assert opened.opened and not continued.opened
    assert continued.call is opened.call
    assert opened.call.arguments == '{"x":1}'


_SHAPE_SAMPLES = [
    '{"path":"a","content":"x"}',
    '{"s":"quote \\" brace } bracket ] backslash \\\\"}',
    '{"u":"\\u00e9\\n","n":[1,2.5e3,-0,true,false,null],"o":{"p":{}}}',
    '  [ {"a" : [ ] } ]  ',
    '"just a string"',
    "123",
    "true",
    "{}",
    "[]",
    # Not JSON, in ways the shape catches and in ways only the parse does.
    '{"a":1',
    '{"a":"unterminated}',
    '{"a":1}}',
    '{"a":1}{"b":2}',
    '{"a":1} x',
    "]",
    "{]",
    '{"a":tru}',
    "   ",
    "12 34",
]


@pytest.mark.parametrize("sample", _SHAPE_SAMPLES)
@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
def test_json_shape_never_rules_out_complete_json(sample, size):
    """The shape only lets a full parse be skipped: wherever the text so far
    parses, it must say ``may_be_complete``, however the text was split."""
    shape = _JsonShape()
    for start in range(0, len(sample), size):
        shape.feed(sample[start:start + size])
        prefix = sample[: start + size]
        if is_complete_json(prefix):
            assert shape.may_be_complete, prefix


def test_a_long_call_parses_what_it_holds_only_when_it_may_be_complete(monkeypatch):
    """Joining and parsing the held arguments on every fragment made a long
    call quadratic (200 KB in 4-byte fragments took seconds, on the one
    worker). A streamed object is parsed whole once, when its closing brace
    may have made it complete — here at the restatement."""
    parsed: list[int] = []
    real = chat_completions.is_complete_json

    def counting(value):
        parsed.append(len(value) if isinstance(value, str) else 0)
        return real(value)

    monkeypatch.setattr(chat_completions, "is_complete_json", counting)
    payload = json.dumps({"path": "a.py", "content": "def f(x):\n    return [1, 2]\n" * 300})
    acc = ToolCallAccumulator(replace_restated=True)
    acc.add({"index": 0, "id": "c1", "function": {"name": "f", "arguments": payload[:4]}})
    for start in range(4, len(payload), 4):
        acc.add({"index": 0, "function": {"arguments": payload[start:start + 4]}})
    assert parsed == []
    assert acc.add({"index": 0, "function": {"arguments": payload}}).arguments is None
    assert parsed == [len(payload), len(payload)]  # the fragment, then what is held
    assert acc.take()[0].arguments == payload


def test_accumulator_ignores_junk_ids_names_and_arguments():
    acc = ToolCallAccumulator(replace_restated=False)
    fragment = acc.add({"index": 0, "id": 7, "function": {"name": 8, "arguments": 9}})
    assert fragment.opened and fragment.arguments is None
    assert (fragment.call.upstream_id, fragment.call.name, fragment.call.arguments) == (None, "", "")
    assert acc.add({"index": 0, "function": "junk"}).call is fragment.call


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (12, 12),
        (0, 0),
        (-3, 0),  # a negative count is 0, on both bridges
        (12.9, 12),  # floats truncate
        (" 12 ", 12),  # numeric strings parse
        ("-4", 0),
        ("12.5", None),
        ("many", None),
        (None, None),
        (True, None),
        (math.nan, None),
        (math.inf, None),
        ([12], None),
    ],
)
def test_token_count(value, expected):
    assert token_count(value) == expected


# (usage, messages (input, output, cache_read, cache_creation), responses
#  (input, cached, output, reasoning, total)). The first three rows are where
# the bridges used to disagree.
_USAGE_CASES = [
    pytest.param(
        {"prompt_tokens": "100", "completion_tokens": 20.0, "total_tokens": "120"},
        (100, 20, 0, 0),
        (100, 0, 20, 0, 120),
        id="string and float counters (messages read them as 0)",
    ),
    pytest.param(
        {"prompt_tokens": -5, "completion_tokens": 7, "total_tokens": 2},
        (0, 7, 0, 0),
        (0, 0, 7, 0, 2),
        id="negative counter (responses passed it through)",
    ),
    pytest.param(
        {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
         "cache_read_input_tokens": 800},
        (200, 50, 800, 0),
        (1000, 800, 50, 0, 1050),
        id="top-level cache_read_input_tokens (responses ignored it)",
    ),
    pytest.param(
        {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
         "prompt_tokens_details": {"cached_tokens": 600},
         "cache_read_input_tokens": 800,
         "completion_tokens_details": {"reasoning_tokens": 30}},
        (400, 50, 600, 0),
        (1000, 600, 50, 30, 1050),
        id="details win over the top-level alias",
    ),
    pytest.param(
        {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
         "prompt_tokens_details": "junk", "completion_tokens_details": None},
        (0, 0, 0, 0),
        (0, 0, 0, 0, 0),
        id="nulls and junk",
    ),
]


@pytest.mark.parametrize(("usage", "messages_expected", "responses_expected"), _USAGE_CASES)
def test_both_bridges_read_usage_the_same_way(usage, messages_expected, responses_expected):
    anthropic = openai_response_to_anthropic_body({"usage": usage})["usage"]
    assert (
        anthropic["input_tokens"],
        anthropic["output_tokens"],
        anthropic["cache_read_input_tokens"],
        anthropic["cache_creation_input_tokens"],
    ) == messages_expected

    chat = {"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": usage}
    responses = chat_response_to_responses_body(chat, {"model": "m"}, "resp_1")["usage"]
    assert (
        responses["input_tokens"],
        responses["input_tokens_details"]["cached_tokens"],
        responses["output_tokens"],
        responses["output_tokens_details"]["reasoning_tokens"],
        responses["total_tokens"],
    ) == responses_expected


def test_read_chat_usage_without_a_usage_object_is_all_zero():
    assert read_chat_usage("junk") == read_chat_usage({}) == read_chat_usage(None)
    assert read_chat_usage(None).prompt_tokens == 0
    assert read_chat_usage(None).cache_creation is None


_SPLIT_USAGE_STREAM = [
    {"choices": [{"index": 0, "delta": {"content": "hi"}}],
     "usage": {"prompt_tokens": 1000, "completion_tokens": 1, "total_tokens": 1001,
               "prompt_tokens_details": {"cached_tokens": 800}}},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    # A trailing usage chunk with only the output side: the prompt count and
    # the cache detail must survive it.
    {"choices": [], "usage": {"completion_tokens": 50, "total_tokens": 1050,
                              "prompt_tokens": None, "prompt_tokens_details": None}},
]


async def test_responses_stream_merges_usage_chunks_field_by_field():
    events, _ = await _responses_run(_SPLIT_USAGE_STREAM)
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["usage"] == {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 800},
        "output_tokens": 50,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 1050,
    }


async def test_messages_stream_merges_the_same_usage_chunks():
    events = await collect(
        openai_stream_to_anthropic_events(as_async(_SPLIT_USAGE_STREAM), model="m")
    )
    assert events[-2]["usage"] == {
        "input_tokens": 200,
        "output_tokens": 50,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 0,
    }


async def test_responses_stream_failure_reports_the_merged_usage():
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "hi"}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}},
        {"error": {"message": "backend died", "type": "server_error"}},
    ]
    events, holder = await _responses_run(chunks)
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["usage"]["input_tokens"] == 10
    assert holder["status"] == "failed"


async def test_responses_stream_without_usage_reports_none():
    events, _ = await _responses_run([{"choices": [{"index": 0, "delta": {"content": "hi"}}]}])
    assert events[-1]["response"]["usage"] is None
