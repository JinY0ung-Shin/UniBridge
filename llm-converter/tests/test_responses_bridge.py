"""Unit tests for the Responses ↔ Chat Completions translation."""

from __future__ import annotations

from typing import AsyncIterator, Iterable, List

from app.responses_bridge import (
    assistant_message_from_chat,
    chat_response_to_responses_body,
    chat_stream_to_responses_events,
    responses_request_to_chat_body,
)


async def _as_async(items: Iterable[dict]) -> AsyncIterator[dict]:
    for it in items:
        yield it


async def _collect(aiter) -> List[dict]:
    return [e async for e in aiter]


# ---------------------------------------------------------------------------
# Request: Responses -> Chat
# ---------------------------------------------------------------------------


def test_request_instructions_and_string_input():
    body = {"model": "m", "instructions": "be terse", "input": "hello", "max_output_tokens": 50}
    out = responses_request_to_chat_body(body)
    assert out["model"] == "m"
    assert out["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
    ]
    assert out["max_completion_tokens"] == 50
    assert "max_tokens" not in out


def test_request_reasoning_effort_forwarded():
    body = {"model": "m", "input": "hello", "reasoning": {"effort": "high", "summary": "auto"}}
    out = responses_request_to_chat_body(body)
    assert out["reasoning_effort"] == "high"
    assert "reasoning" not in out


def test_request_input_items_function_call_roundtrip():
    body = {
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "weather?"}]},
            {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{\"q\":1}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
        ],
    }
    out = responses_request_to_chat_body(body)
    assert out["messages"][0] == {"role": "user", "content": "weather?"}
    asst = out["messages"][1]
    assert asst["role"] == "assistant"
    assert asst["tool_calls"][0]["id"] == "call_1"
    assert asst["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": "{\"q\":1}"}
    assert out["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}


def test_request_tools_and_tool_choice_reshape():
    body = {
        "model": "m",
        "input": "hi",
        "tools": [{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "f"},
    }
    out = responses_request_to_chat_body(body)
    assert out["tools"] == [
        {"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}
    ]
    assert out["tool_choice"] == {"type": "function", "function": {"name": "f"}}


def test_request_prior_messages_prepended_then_followup_instructions():
    prior = [{"role": "system", "content": "orig"}, {"role": "user", "content": "q1"},
             {"role": "assistant", "content": "a1"}]
    body = {"model": "m", "instructions": "new", "input": "q2", "previous_response_id": "resp_x"}
    out = responses_request_to_chat_body(body, prior_messages=prior)
    # prior chain prepended; a follow-up instructions applies to the current turn,
    # appended as a system message ahead of the new input (OpenAI allows this).
    assert out["messages"][:3] == prior
    assert out["messages"][3] == {"role": "system", "content": "new"}
    assert out["messages"][4] == {"role": "user", "content": "q2"}


def test_request_prior_messages_without_followup_instructions():
    prior = [{"role": "system", "content": "orig"}, {"role": "user", "content": "q1"}]
    body = {"model": "m", "input": "q2", "previous_response_id": "resp_x"}
    out = responses_request_to_chat_body(body, prior_messages=prior)
    assert out["messages"] == prior + [{"role": "user", "content": "q2"}]


def test_request_function_call_output_array_extracts_text():
    body = {"model": "m", "input": [
        {"type": "function_call_output", "call_id": "c1",
         "output": [{"type": "output_text", "text": "part1 "}, {"type": "output_text", "text": "part2"}]},
    ]}
    out = responses_request_to_chat_body(body)
    assert out["messages"][0] == {"role": "tool", "tool_call_id": "c1", "content": "part1 part2"}


def test_request_tool_strict_preserved():
    body = {"model": "m", "input": "hi",
            "tools": [{"type": "function", "name": "f", "parameters": {}, "strict": True}]}
    out = responses_request_to_chat_body(body)
    assert out["tools"][0]["function"]["strict"] is True


def test_request_input_image_file_id_only_is_skipped():
    body = {"model": "m", "input": [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "file_id": "file_123"},  # no image_url
        ]},
    ]}
    out = responses_request_to_chat_body(body)
    # No image_url:{url:null} part emitted; only text survives (collapsed to str).
    assert out["messages"][0] == {"role": "user", "content": "look"}


def test_request_text_json_schema_to_response_format():
    body = {"model": "m", "input": "hi",
            "text": {"format": {"type": "json_schema", "name": "S", "schema": {"type": "object"}, "strict": True}}}
    out = responses_request_to_chat_body(body)
    assert out["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "S", "schema": {"type": "object"}, "strict": True},
    }


# ---------------------------------------------------------------------------
# Non-streaming response: Chat -> Responses
# ---------------------------------------------------------------------------


def test_response_text_and_tool_call_mapping():
    chat = {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1741569952, "model": "qwen",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hi there",
                        "tool_calls": [{"id": "call_2", "type": "function",
                                        "function": {"name": "get_weather", "arguments": "{\"l\":\"SF\"}"}}]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 19, "completion_tokens": 10, "total_tokens": 29},
    }
    out = chat_response_to_responses_body(chat, {"model": "qwen"}, "resp_1")
    assert out["object"] == "response"
    assert out["id"] == "resp_1"
    assert out["created_at"] == 1741569952
    assert out["status"] == "completed"
    assert "output_text" not in out  # SDK-derived, never a wire field

    msg = out["output"][0]
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert msg["content"] == [{"type": "output_text", "text": "Hi there", "annotations": []}]

    fc = out["output"][1]
    assert fc["type"] == "function_call"
    assert fc["call_id"] == "call_2"          # correlation id from chat tool_call id
    assert fc["id"].startswith("fc_")          # synthesized item id, distinct
    assert fc["id"] != "call_2"
    assert fc["name"] == "get_weather"
    assert fc["arguments"] == "{\"l\":\"SF\"}"

    assert out["usage"] == {
        "input_tokens": 19, "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 10, "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 29,
    }


def test_response_length_finish_is_incomplete():
    chat = {"model": "m", "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}
    out = chat_response_to_responses_body(chat, {"model": "m"}, "resp_1")
    assert out["status"] == "incomplete"
    assert out["incomplete_details"] == {"reason": "max_output_tokens"}


def test_response_renamed_reasoning_field_becomes_reasoning_item():
    # vLLM renamed ``reasoning_content`` → ``reasoning`` in non-streaming too.
    chat = {"model": "m", "choices": [{"message": {"content": "hi", "reasoning": "why"},
                                       "finish_reason": "stop"}]}
    out = chat_response_to_responses_body(chat, {"model": "m"}, "resp_1")
    assert [it["type"] for it in out["output"]] == ["reasoning", "message"]
    assert out["output"][0]["content"] == [{"type": "reasoning_text", "text": "why"}]


def test_assistant_message_from_chat():
    msg = {"role": "assistant", "content": "hi", "tool_calls": [{"id": "c", "type": "function",
                                                                  "function": {"name": "f", "arguments": "{}"}}]}
    assert assistant_message_from_chat(msg) == msg
    assert assistant_message_from_chat({"role": "assistant", "content": None}) == {"role": "assistant", "content": ""}


# ---------------------------------------------------------------------------
# Streaming: Chat SSE chunks -> Responses events
# ---------------------------------------------------------------------------


async def _run_stream(chunks, request_body=None):
    holder: dict = {}
    events = await _collect(
        chat_stream_to_responses_events(
            _as_async(chunks), response_id="resp_S", request_body=request_body or {"model": "m"},
            holder=holder, emit_reasoning=True,
        )
    )
    return events, holder


async def test_stream_text_then_tool_call():
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "Let me"}}]},
        {"choices": [{"delta": {"content": " check."}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_abc", "type": "function", "function": {"name": "get_weather", "arguments": "{\"l\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"SF\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}},
    ]
    events, holder = await _run_stream(chunks)

    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert types[1] == "response.in_progress"
    assert types[-1] == "response.completed"

    # sequence_number strictly increasing from 0
    seqs = [e["sequence_number"] for e in events]
    assert seqs == list(range(len(events)))

    # text item fully opened -> streamed -> closed before tool item opens
    assert "response.output_item.added" in types
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types

    # terminal carries full output[] with both items, in order, plus usage
    final = events[-1]["response"]
    assert final["status"] == "completed"
    assert [it["type"] for it in final["output"]] == ["message", "function_call"]
    fc = final["output"][1]
    assert fc["call_id"] == "call_abc" and fc["id"].startswith("fc_")
    assert fc["arguments"] == "{\"l\":\"SF\"}"
    assert final["usage"]["input_tokens"] == 9 and final["usage"]["output_tokens"] == 5

    # text done text is the concatenation
    text_done = next(e for e in events if e["type"] == "response.output_text.delta")
    assert text_done["delta"] == "Let me"
    done = next(e for e in events if e["type"] == "response.output_text.done")
    assert done["text"] == "Let me check."

    # assistant message captured for persistence
    assert holder["assistant_message"]["content"] == "Let me check."
    assert holder["assistant_message"]["tool_calls"][0]["id"] == "call_abc"
    assert holder["status"] == "completed"


async def test_stream_reasoning_precedes_text():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events, _ = await _run_stream(chunks)
    final = events[-1]["response"]
    assert [it["type"] for it in final["output"]] == ["reasoning", "message"]
    assert any(e["type"] == "response.reasoning_text.delta" for e in events)


async def test_stream_renamed_reasoning_field_precedes_text():
    # vLLM renamed ``reasoning_content`` → ``reasoning``; both keys must open
    # a reasoning item.
    chunks = [
        {"choices": [{"delta": {"reasoning": "thinking"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events, _ = await _run_stream(chunks)
    final = events[-1]["response"]
    assert [it["type"] for it in final["output"]] == ["reasoning", "message"]
    assert any(e["type"] == "response.reasoning_text.delta" for e in events)


async def test_stream_reasoning_wrapped_in_content_part_events():
    # The reasoning item must be bracketed by content_part.added/.done (part
    # type reasoning_text), mirroring the message item, so a consumer that
    # reconstructs content parts from those events sees the reasoning text.
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events, _ = await _run_stream(chunks)

    reasoning_added = [e for e in events
                       if e["type"] == "response.content_part.added"
                       and e["part"]["type"] == "reasoning_text"]
    reasoning_done = [e for e in events
                      if e["type"] == "response.content_part.done"
                      and e["part"]["type"] == "reasoning_text"]
    assert len(reasoning_added) == 1
    assert len(reasoning_done) == 1
    assert reasoning_done[0]["part"]["text"] == "thinking"

    # content_part.added precedes the first reasoning_text.delta; content_part.done
    # precedes the reasoning item's output_item.done.
    rt_delta = next(e["sequence_number"] for e in events
                    if e["type"] == "response.reasoning_text.delta")
    assert reasoning_added[0]["sequence_number"] < rt_delta
    reasoning_oi = reasoning_added[0]["output_index"]
    item_done = next(e["sequence_number"] for e in events
                     if e["type"] == "response.output_item.done"
                     and e["output_index"] == reasoning_oi)
    assert reasoning_done[0]["sequence_number"] < item_done

    # sequence_number stays strictly increasing from 0 with the added events.
    assert [e["sequence_number"] for e in events] == list(range(len(events)))


async def test_stream_text_after_tool_call_does_not_nest_item_lifecycles():
    # tool call opens first, then a trailing text note. The tool item must be
    # fully closed (output_item.done) before the message item is opened
    # (output_item.added) — item lifecycles must not nest/interleave.
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_x", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {"content": "trailing text"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events, _ = await _run_stream(chunks)

    def seq_of(etype, oi):
        return next(e["sequence_number"] for e in events
                    if e["type"] == etype and e["output_index"] == oi)

    tool_done = seq_of("response.output_item.done", 0)   # function_call at oi 0
    msg_added = seq_of("response.output_item.added", 1)  # message at oi 1
    assert tool_done < msg_added
    # terminal output[] order is still index-sorted: function_call then message.
    final = events[-1]["response"]
    assert [it["type"] for it in final["output"]] == ["function_call", "message"]


async def test_stream_length_finish_is_incomplete():
    chunks = [
        {"choices": [{"delta": {"content": "partial"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]
    events, holder = await _run_stream(chunks)
    assert events[-1]["type"] == "response.incomplete"
    final = events[-1]["response"]
    assert final["incomplete_details"] == {"reason": "max_output_tokens"}
    # The truncated item also carries incomplete status, not completed.
    assert final["output"][0]["status"] == "incomplete"
    assert holder["status"] == "incomplete"


async def test_stream_refusal_emitted_as_refusal_events():
    chunks = [
        {"choices": [{"delta": {"refusal": "I can't help with that"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events, holder = await _run_stream(chunks)
    types = [e["type"] for e in events]
    assert "response.refusal.delta" in types
    assert "response.refusal.done" in types
    final = events[-1]["response"]
    assert final["output"][0]["content"][0] == {"type": "refusal", "refusal": "I can't help with that"}
    # refusal text is persisted as the assistant turn content
    assert holder["assistant_message"]["content"] == "I can't help with that"


async def test_stream_empty_produces_no_assistant_message():
    chunks = [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    events, holder = await _run_stream(chunks)
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output"] == []
    # nothing real produced → no persistence
    assert "assistant_message" not in holder


async def test_stream_upstream_error_chunk_emits_failed_and_skips_persist():
    chunks = [
        {"choices": [{"delta": {"content": "partial"}}]},
        {"error": {"code": "rate_limit_exceeded", "message": "slow down"}},
    ]
    events, holder = await _run_stream(chunks)
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["status"] == "failed"
    assert events[-1]["response"]["error"] == {"code": "rate_limit_exceeded", "message": "slow down"}
    # sequence_number stays strictly increasing from 0 through the failure event.
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    # No assistant_message is left for the route to persist (a failed turn must
    # not poison a future previous_response_id chain).
    assert holder.get("assistant_message") is None
    assert holder["status"] == "failed"


async def test_stream_text_after_tool_call_terminal_output_ordered_by_index():
    # Pathological ordering: tool call first, then text. Terminal output[] must
    # still be ordered by output_index (tool=0, text=1).
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_x", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {"content": "trailing text"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events, _ = await _run_stream(chunks)
    final = events[-1]["response"]
    types_in_order = [it["type"] for it in final["output"]]
    assert types_in_order == ["function_call", "message"]


# ---------------------------------------------------------------------------
# Regression: defects found in the 2026-06 adversarial review
# ---------------------------------------------------------------------------


def test_request_parallel_function_calls_coalesce_into_single_assistant_message():
    # Parallel tool calls replayed via input[] must become ONE assistant message
    # carrying all tool_calls, so each tool result stays adjacent to it (else the
    # Chat Completions upstream rejects the interleaved sequence with a 400).
    body = {"model": "m", "input": [
        {"type": "message", "role": "user", "content": "weather in SF and NY?"},
        {"type": "function_call", "call_id": "call_1", "name": "wx", "arguments": '{"city":"SF"}'},
        {"type": "function_call", "call_id": "call_2", "name": "wx", "arguments": '{"city":"NY"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "60F"},
        {"type": "function_call_output", "call_id": "call_2", "output": "50F"},
    ]}
    msgs = responses_request_to_chat_body(body)["messages"]
    assert msgs[0] == {"role": "user", "content": "weather in SF and NY?"}
    assert msgs[1]["role"] == "assistant"
    assert [tc["id"] for tc in msgs[1]["tool_calls"]] == ["call_1", "call_2"]
    assert msgs[2] == {"role": "tool", "tool_call_id": "call_1", "content": "60F"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_2", "content": "50F"}
    assert len(msgs) == 4
    # adjacency invariant: every tool result's id was issued by the preceding block
    issued = {tc["id"] for tc in msgs[1]["tool_calls"]}
    assert all(m["tool_call_id"] in issued for m in msgs if m["role"] == "tool")


def test_request_sequential_tool_calls_across_results_are_separate_blocks():
    # fc, fco, fc, fco are two distinct turns — a tool result flushes the run, so
    # the second call must NOT be coalesced into the first assistant block.
    body = {"model": "m", "input": [
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "r1"},
        {"type": "function_call", "call_id": "c2", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "r2"},
    ]}
    msgs = responses_request_to_chat_body(body)["messages"]
    assert [m["role"] for m in msgs] == ["assistant", "tool", "assistant", "tool"]
    assert [tc["id"] for tc in msgs[0]["tool_calls"]] == ["c1"]
    assert [tc["id"] for tc in msgs[2]["tool_calls"]] == ["c2"]


def test_request_assistant_text_then_tool_calls_merge_into_one_message():
    # An assistant text message immediately followed by function_calls is a single
    # turn (content + tool_calls), matching the streaming finalize_holder shape.
    body = {"model": "m", "input": [
        {"type": "message", "role": "assistant", "content": "let me check"},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
    ]}
    msgs = responses_request_to_chat_body(body)["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == "let me check"
    assert [tc["id"] for tc in msgs[0]["tool_calls"]] == ["c1"]


def test_assistant_message_from_chat_falls_back_to_refusal():
    msg = {"role": "assistant", "content": None, "refusal": "I can't help with that"}
    assert assistant_message_from_chat(msg) == {
        "role": "assistant", "content": "I can't help with that"
    }


def test_response_content_and_refusal_both_kept_as_parts():
    chat = {"model": "m", "choices": [
        {"message": {"role": "assistant", "content": "hello", "refusal": "nope"},
         "finish_reason": "stop"}]}
    out = chat_response_to_responses_body(chat, {"model": "m"}, "resp_1")
    parts = out["output"][0]["content"]
    assert {"type": "output_text", "text": "hello", "annotations": []} in parts
    assert {"type": "refusal", "refusal": "nope"} in parts


async def test_stream_tool_index_reuse_with_distinct_ids_splits_calls():
    # A non-conformant upstream that reuses index 0 for two DISTINCT calls must
    # yield two separate function_call items, not one with concatenated args.
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function",
             "function": {"name": "f1", "arguments": '{"x":1}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_b", "type": "function",
             "function": {"name": "f2", "arguments": '{"y":2}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events, holder = await _run_stream(chunks)
    fcs = [it for it in events[-1]["response"]["output"] if it["type"] == "function_call"]
    assert [fc["call_id"] for fc in fcs] == ["call_a", "call_b"]
    assert [fc["arguments"] for fc in fcs] == ['{"x":1}', '{"y":2}']
    assert [fc["name"] for fc in fcs] == ["f1", "f2"]
    # both calls survive into the persisted chaining transcript
    assert len(holder["assistant_message"]["tool_calls"]) == 2


async def test_stream_tool_index_reuse_continuation_fragment_still_appends():
    # The fix must not regress the normal case: a same-index delta with NO id is
    # an argument continuation and keeps appending to the open call.
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function",
             "function": {"name": "f", "arguments": '{"x":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events, _ = await _run_stream(chunks)
    fcs = [it for it in events[-1]["response"]["output"] if it["type"] == "function_call"]
    assert len(fcs) == 1
    assert fcs[0]["arguments"] == '{"x":1}'


async def test_stream_text_then_refusal_not_blended_in_persisted_content():
    chunks = [
        {"choices": [{"delta": {"content": "hello"}}]},
        {"choices": [{"delta": {"refusal": "nope"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events, holder = await _run_stream(chunks)
    msg_items = [it for it in events[-1]["response"]["output"] if it["type"] == "message"]
    assert len(msg_items) == 2  # both delivered, as distinct items
    # persisted chain content is the real text only, never "hellonope"
    assert holder["assistant_message"]["content"] == "hello"


async def test_stream_index_reuse_then_disconnect_persists_each_call_once():
    # Disconnect (GeneratorExit) after an index-0 reuse must not persist the
    # still-open second call twice (index reuse leaves it twice in tool_order).
    async def gen():
        yield {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function",
             "function": {"name": "f1", "arguments": '{"x":1}'}}]}}]}
        yield {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_b", "type": "function",
             "function": {"name": "f2", "arguments": '{"y":2}'}}]}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}

    holder: dict = {}
    agen = chat_stream_to_responses_events(
        gen(), response_id="resp_D", request_body={"model": "m"}, holder=holder
    )
    added = 0
    async for e in agen:
        if e["type"] == "response.output_item.added":
            added += 1
        if added == 2:  # both tool items open → simulate client disconnect now
            break
    await agen.aclose()

    ids = [tc["id"] for tc in holder.get("assistant_message", {}).get("tool_calls", [])]
    assert ids == ["call_a", "call_b"]  # no duplicate call_b
