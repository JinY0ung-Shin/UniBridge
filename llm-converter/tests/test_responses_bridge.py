"""Unit tests for the Responses ↔ Chat Completions translation."""

from __future__ import annotations

from typing import AsyncIterator, Iterable, List

from app.responses_bridge import (
    _input_to_messages,
    _usage_to_responses,
    assistant_message_from_chat,
    chat_response_to_responses_body,
    chat_stream_to_responses_events,
    is_codex_client,
    namespace_map_from_tools,
    resolve_length_as_completed,
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
    # LiteLLM drops reasoning_effort for models outside its gpt-5/o-series name
    # map unless the request marks it allowed.
    assert out["allowed_openai_params"] == ["reasoning_effort"]
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


def test_request_codex_compaction_empty_tools_drops_tool_params():
    """Codex CLI hard-codes ``tool_choice``/``parallel_tool_calls`` on every
    request and sends ``tools: []`` on tool-less turns — its automatic context
    compaction request. vLLM/SGLang 400 on either parameter without tools."""
    body = {
        "model": "m",
        "input": "summarize",
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }
    out = responses_request_to_chat_body(body)
    assert "tools" not in out
    assert "tool_choice" not in out
    assert "parallel_tool_calls" not in out


def test_request_codex_compaction_only_builtin_tools_drops_tool_params():
    """Same shape when every inbound tool is a non-function type the converter
    drops (``web_search``, ``namespace``): the translated list ends up empty."""
    body = {
        "model": "m",
        "input": "summarize",
        "tools": [{"type": "web_search", "external_web_access": False}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }
    out = responses_request_to_chat_body(body)
    assert "tools" not in out
    assert "tool_choice" not in out
    assert "parallel_tool_calls" not in out


def test_request_codex_tool_params_kept_with_a_function_tool():
    body = {
        "model": "m",
        "input": "summarize",
        "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }
    out = responses_request_to_chat_body(body)
    assert out["tools"] == [
        {"type": "function", "function": {"name": "shell", "description": "", "parameters": {"type": "object"}}}
    ]
    assert out["tool_choice"] == "auto"
    assert out["parallel_tool_calls"] is False


def test_request_prior_messages_prepended_then_followup_instructions():
    prior = [{"role": "system", "content": "orig"}, {"role": "user", "content": "q1"},
             {"role": "assistant", "content": "a1"}]
    body = {"model": "qwen3.5-test", "instructions": "new", "input": "q2",
            "previous_response_id": "resp_x"}
    out = responses_request_to_chat_body(body, prior_messages=prior)
    # prior chain prepended; a follow-up instructions applies to the current turn,
    # appended ahead of the new input. It is appended as a system message, but
    # that lands past index 0, so the default placement policy role-swaps it to
    # ``user`` in place — strict chat templates 400 on a mid-array system turn.
    assert out["messages"][:3] == prior
    assert out["messages"][3] == {"role": "user", "content": "new"}
    assert out["messages"][4] == {"role": "user", "content": "q2"}


def test_request_followup_instructions_stay_system_for_a_model_outside_the_gate():
    prior = [{"role": "system", "content": "orig"}, {"role": "user", "content": "q1"},
             {"role": "assistant", "content": "a1"}]
    body = {"model": "gpt-4o-mini", "instructions": "new", "input": "q2",
            "previous_response_id": "resp_x"}
    out = responses_request_to_chat_body(body, prior_messages=prior)
    # The chaining shape a tolerant backend gets: the follow-up instructions stays
    # a second system message, mid-array, at system authority — where the chain
    # assembled it, and how this bridge behaved before the placement fix existed.
    assert out["messages"] == prior + [
        {"role": "system", "content": "new"},
        {"role": "user", "content": "q2"},
    ]


def test_request_prior_messages_without_followup_instructions():
    prior = [{"role": "system", "content": "orig"}, {"role": "user", "content": "q1"}]
    body = {"model": "m", "input": "q2", "previous_response_id": "resp_x"}
    out = responses_request_to_chat_body(body, prior_messages=prior)
    assert out["messages"] == prior + [{"role": "user", "content": "q2"}]


def test_request_followup_instructions_merge_into_head_under_hoist_policy(monkeypatch):
    monkeypatch.setenv("CONVERTER_MID_SYSTEM_POLICY", "hoist")
    prior = [{"role": "system", "content": "orig"}, {"role": "user", "content": "q1"},
             {"role": "assistant", "content": "a1"}]
    # A real deployment name carries the provider prefix; the gate searches the
    # model string rather than matching it whole, so this still matches.
    body = {"model": "hosted_vllm/qwen3.6-32b", "instructions": "new", "input": "q2",
            "previous_response_id": "resp_x"}
    out = responses_request_to_chat_body(body, prior_messages=prior)
    # hoist keeps the follow-up instructions at system authority by folding it
    # into the chain's original system prompt, leaving exactly one system turn.
    assert out["messages"] == [
        {"role": "system", "content": "orig\n\nnew"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]


def test_request_developer_item_is_demoted_when_not_leading():
    body = {"model": "qwen3.5-test", "input": [
        {"type": "message", "role": "user", "content": "q"},
        {"type": "message", "role": "developer", "content": "rule"},
        {"type": "message", "role": "assistant", "content": "a"},
    ]}
    out = responses_request_to_chat_body(body)
    # developer → system is preserved as a mapping, but a system turn past index 0
    # is what strict templates reject, so the default policy role-swaps it.
    assert [m["role"] for m in out["messages"]] == ["user", "user", "assistant"]
    assert out["messages"][1] == {"role": "user", "content": "rule"}


def test_request_developer_item_keeps_system_role_under_asis_policy(monkeypatch):
    monkeypatch.setenv("CONVERTER_MID_SYSTEM_POLICY", "asis")
    # Gate-matching model, so ``asis`` is what preserves the role here.
    body = {"model": "qwen3.5-test", "input": [
        {"type": "message", "role": "user", "content": "q"},
        {"type": "message", "role": "developer", "content": "rule"},
        {"type": "message", "role": "assistant", "content": "a"},
    ]}
    out = responses_request_to_chat_body(body)
    assert out["messages"][1] == {"role": "system", "content": "rule"}


def test_request_developer_item_keeps_system_role_for_a_model_outside_the_gate():
    # Unchained counterpart to the gate test above: a mid-array system turn that
    # came straight from the input array, not from a resolved chain.
    body = {"model": "gpt-4o-mini", "input": [
        {"type": "message", "role": "user", "content": "q"},
        {"type": "message", "role": "developer", "content": "rule"},
        {"type": "message", "role": "assistant", "content": "a"},
    ]}
    out = responses_request_to_chat_body(body)
    assert [m["role"] for m in out["messages"]] == ["user", "system", "assistant"]


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


def test_response_length_finish_reported_as_completed_when_enabled():
    chat = {"model": "m", "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}
    out = chat_response_to_responses_body(
        chat, {"model": "m"}, "resp_1", length_as_completed=True
    )
    assert out["status"] == "completed"
    assert out["incomplete_details"] is None
    # The item status has to agree with the terminal status, or the client sees
    # a completed response holding an incomplete message.
    assert out["output"][0]["status"] == "completed"


def test_assistant_message_from_chat():
    msg = {"role": "assistant", "content": "hi", "tool_calls": [{"id": "c", "type": "function",
                                                                  "function": {"name": "f", "arguments": "{}"}}]}
    assert assistant_message_from_chat(msg) == msg
    assert assistant_message_from_chat({"role": "assistant", "content": None}) == {"role": "assistant", "content": ""}


# ---------------------------------------------------------------------------
# Streaming: Chat SSE chunks -> Responses events
# ---------------------------------------------------------------------------


async def _run_stream(chunks, request_body=None, *, length_as_completed=False, namespace_map=None):
    holder: dict = {}
    events = await _collect(
        chat_stream_to_responses_events(
            _as_async(chunks), response_id="resp_S", request_body=request_body or {"model": "m"},
            holder=holder, emit_reasoning=True, length_as_completed=length_as_completed,
            namespace_map=namespace_map,
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


async def test_stream_length_finish_reported_as_completed_when_enabled():
    chunks = [
        {"choices": [{"delta": {"content": "partial"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]
    events, holder = await _run_stream(chunks, length_as_completed=True)
    assert events[-1]["type"] == "response.completed"
    final = events[-1]["response"]
    assert final["incomplete_details"] is None
    assert final["output"][0]["status"] == "completed"
    assert holder["status"] == "completed"


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


async def test_stream_late_arriving_id_backfills_without_splitting():
    # A call whose opening delta carries no id is emitted with a synthesized
    # call_id. When the real upstream id shows up on a later fragment of that
    # SAME call it is metadata, not a second call — comparing it against the
    # synthesized id would split one call into two.
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"city":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "upstream_9", "type": "function",
             "function": {"name": "weather", "arguments": '"Seoul"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events, holder = await _run_stream(chunks)
    fcs = [it for it in events[-1]["response"]["output"] if it["type"] == "function_call"]
    assert len(fcs) == 1
    assert fcs[0]["arguments"] == '{"city":"Seoul"}'
    assert fcs[0]["name"] == "weather"  # late name is adopted too
    assert len(holder["assistant_message"]["tool_calls"]) == 1


async def test_stream_synthesized_call_id_is_not_rewritten_by_a_late_real_id():
    # output_item.added has already told the client this item's call_id, and the
    # client correlates its function_call_output against it, so a real id
    # arriving afterwards must not rewrite it: added, the arguments deltas, done
    # and the final output[] entry all have to agree.
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "weather", "arguments": '{"city":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "upstream_9", "function": {"arguments": '"Seoul"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events, holder = await _run_stream(chunks)
    added = [e for e in events if e["type"] == "response.output_item.added"]
    assert len(added) == 1
    item_id, call_id = added[0]["item"]["id"], added[0]["item"]["call_id"]
    assert call_id.startswith("call_") and call_id != "upstream_9"

    arg_deltas = [e for e in events if e["type"] == "response.function_call_arguments.delta"]
    assert {e["item_id"] for e in arg_deltas} == {item_id}

    done = [e for e in events if e["type"] == "response.output_item.done"]
    assert len(done) == 1
    assert done[0]["item"]["id"] == item_id
    assert done[0]["item"]["call_id"] == call_id

    fcs = [it for it in events[-1]["response"]["output"] if it["type"] == "function_call"]
    assert [(fc["id"], fc["call_id"]) for fc in fcs] == [(item_id, call_id)]
    assert [tc["id"] for tc in holder["assistant_message"]["tool_calls"]] == [call_id]


async def test_stream_id_less_open_then_two_distinct_real_ids_splits_once():
    # Backfilling the first real id must not disable the split guard: a SECOND,
    # different real id on the same index still opens a new call. The first
    # call keeps the call_id it was emitted with (not the backfilled call_a),
    # and each call keeps only its own arguments.
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "f1", "arguments": '{"x":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "function": {"arguments": "1}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_b", "type": "function",
             "function": {"name": "f2", "arguments": '{"y":2}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events, holder = await _run_stream(chunks)
    added = [e for e in events if e["type"] == "response.output_item.added"]
    first_call_id = added[0]["item"]["call_id"]
    assert first_call_id not in ("call_a", "call_b")

    fcs = [it for it in events[-1]["response"]["output"] if it["type"] == "function_call"]
    assert [fc["call_id"] for fc in fcs] == [first_call_id, "call_b"]
    assert [fc["arguments"] for fc in fcs] == ['{"x":1}', '{"y":2}']
    assert [fc["name"] for fc in fcs] == ["f1", "f2"]
    assert len(holder["assistant_message"]["tool_calls"]) == 2


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


# ---------------------------------------------------------------------------
# Regression: defects found in the 2026-09 Codex CLI conformance run
# ---------------------------------------------------------------------------

_OMITTED_1 = "[1 non-text tool output part(s) omitted: tool results reach this model as text only]"


def test_usage_null_counters_are_coerced_to_int_zero():
    # Codex CLI deserializes the usage counters as required i64; a null from an
    # OpenAI-compatible upstream would fail the whole turn with
    # "invalid type: null, expected i64".
    usage = _usage_to_responses(
        {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "prompt_tokens_details": {"cached_tokens": None},
            "completion_tokens_details": None,
        }
    )
    assert usage == {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    }
    for value in (
        usage["input_tokens"],
        usage["input_tokens_details"]["cached_tokens"],
        usage["output_tokens"],
        usage["output_tokens_details"]["reasoning_tokens"],
        usage["total_tokens"],
    ):
        assert isinstance(value, int) and not isinstance(value, bool)


def test_usage_real_counters_map_one_to_one():
    assert _usage_to_responses(
        {
            "prompt_tokens": 19,
            "completion_tokens": 10,
            "total_tokens": 29,
            "prompt_tokens_details": {"cached_tokens": 7},
            "completion_tokens_details": {"reasoning_tokens": 4},
        }
    ) == {
        "input_tokens": 19,
        "input_tokens_details": {"cached_tokens": 7},
        "output_tokens": 10,
        "output_tokens_details": {"reasoning_tokens": 4},
        "total_tokens": 29,
    }


def test_usage_non_dict_is_none():
    assert _usage_to_responses(None) is None
    assert _usage_to_responses("19") is None


async def test_stream_null_usage_counter_reaches_client_as_int_zero():
    chunks = [
        {"choices": [{"delta": {"content": "hi"}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": None, "completion_tokens": 5, "total_tokens": None},
        },
    ]
    events, _ = await _run_stream(chunks)
    assert events[-1]["type"] == "response.completed"
    usage = events[-1]["response"]["usage"]
    assert usage["input_tokens"] == 0
    assert isinstance(usage["input_tokens"], int)
    assert usage["output_tokens"] == 5
    assert usage["total_tokens"] == 0


def test_request_reasoning_effort_carries_allowed_openai_params():
    out = responses_request_to_chat_body({"model": "m", "input": "hi", "reasoning": {"effort": "high"}})
    assert out["reasoning_effort"] == "high"
    assert out["allowed_openai_params"] == ["reasoning_effort"]


def test_request_without_reasoning_omits_both_keys():
    out = responses_request_to_chat_body({"model": "m", "input": "hi"})
    assert "reasoning_effort" not in out
    assert "allowed_openai_params" not in out
    # An effort-less reasoning object must not smuggle either key in either.
    for reasoning in ({}, {"summary": "auto"}, {"effort": None}, {"effort": ""}, "high"):
        out = responses_request_to_chat_body({"model": "m", "input": "hi", "reasoning": reasoning})
        assert "reasoning_effort" not in out, f"failed for {reasoning!r}"
        assert "allowed_openai_params" not in out, f"failed for {reasoning!r}"


def test_request_function_call_output_image_part_leaves_placeholder():
    # Codex's view_image tool returns an input_image data URL as the tool
    # result. A chat role:"tool" message is text-only, so the part cannot be
    # forwarded — the model must at least be told something was dropped.
    messages = _input_to_messages(
        [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"}
                ],
            }
        ]
    )
    assert messages == [{"role": "tool", "tool_call_id": "c1", "content": _OMITTED_1}]


def test_request_function_call_output_text_plus_image_keeps_both():
    out = responses_request_to_chat_body(
        {
            "model": "m",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": [
                        {"type": "output_text", "text": "ok"},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"},
                    ],
                }
            ],
        }
    )
    assert out["messages"][0] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": f"ok\n{_OMITTED_1}",
    }


def test_request_function_call_output_counts_every_non_text_part():
    messages = _input_to_messages(
        [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
                    {"type": "encrypted_content", "data": "zzz"},
                    "not-a-dict",  # non-dict parts are skipped, not counted
                ],
            }
        ]
    )
    assert messages[0]["content"] == (
        "[3 non-text tool output part(s) omitted: tool results reach this model as text only]"
    )


# ---------------------------------------------------------------------------
# finish_reason=length reporting mode + Codex client detection
# ---------------------------------------------------------------------------


def test_is_codex_client_recognizes_both_headers():
    assert is_codex_client({"originator": "codex_exec"}) is True
    assert is_codex_client({"originator": "codex_vscode"}) is True
    assert is_codex_client({"user-agent": "codex_cli_rs/0.144.3 (Ubuntu)"}) is True
    # Case and surrounding whitespace are the client's business, not ours.
    assert is_codex_client({"originator": " Codex_CLI_RS "}) is True


def test_is_codex_client_rejects_other_clients():
    assert is_codex_client({}) is False
    assert is_codex_client({"user-agent": "python-httpx/0.28"}) is False
    # A non-str value (or no headers object at all) must not raise.
    assert is_codex_client({"originator": None}) is False
    assert is_codex_client(None) is False


def test_resolve_length_as_completed_modes():
    codex = {"originator": "codex_exec"}
    other = {"user-agent": "python-httpx/0.28"}
    # auto -> per-client; true/false -> unconditional.
    assert resolve_length_as_completed("auto", codex) is True
    assert resolve_length_as_completed("auto", other) is False
    assert resolve_length_as_completed("true", other) is True
    assert resolve_length_as_completed("false", codex) is False


# ---------------------------------------------------------------------------
# Lifecycle event size
# ---------------------------------------------------------------------------


async def test_opening_lifecycle_events_omit_the_request_echo():
    tools = [{"type": "function", "name": "shell", "parameters": {"type": "object"}}]
    chunks = [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    events, _ = await _run_stream(
        chunks, {"model": "m", "instructions": "SYS", "tools": tools}
    )
    by_type = {e["type"]: e for e in events if "response" in e}
    # Codex sends tens of KB of instructions + tools and reads neither field
    # from the opening events, so they carry an empty echo.
    for etype in ("response.created", "response.in_progress"):
        assert by_type[etype]["response"]["tools"] == []
        assert by_type[etype]["response"]["instructions"] is None
    # The terminal event stays spec-complete.
    terminal = by_type["response.completed"]["response"]
    assert terminal["tools"] == tools
    assert terminal["instructions"] == "SYS"


# ---------------------------------------------------------------------------
# Request: reasoning-effort clamp
# ---------------------------------------------------------------------------


def test_request_reasoning_effort_above_the_backend_vocabulary_is_clamped():
    """Codex's ladder reaches xhigh/max; the backend sees the value verbatim."""
    out = responses_request_to_chat_body({"model": "m", "input": "hi", "reasoning": {"effort": "xhigh"}})
    assert out["reasoning_effort"] == "high"
    assert out["allowed_openai_params"] == ["reasoning_effort"]


def test_request_reasoning_effort_off_the_ladder_is_dropped_entirely():
    out = responses_request_to_chat_body(
        {"model": "m", "input": "hi", "reasoning": {"effort": "disabled"}}
    )
    assert "reasoning_effort" not in out
    assert "allowed_openai_params" not in out


# ---------------------------------------------------------------------------
# Namespace tool flattening (Codex multi_agent_v1 sub-agents / image_gen)
# ---------------------------------------------------------------------------


def _namespace_tools_body():
    """A Codex-shaped request: a multi_agent_v1 namespace bundling two inner
    functions, plus a normal top-level function tool."""
    return {
        "model": "m",
        "input": "hi",
        "tools": [
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "description": "sub-agent controls",
                "tools": [
                    {"type": "function", "name": "spawn_agent", "description": "spawn",
                     "strict": False, "parameters": {"type": "object", "properties": {}}},
                    {"type": "function", "name": "wait_agent", "description": "wait",
                     "strict": False, "parameters": {"type": "object", "properties": {}}},
                ],
            },
            {"type": "function", "name": "shell", "description": "run",
             "parameters": {"type": "object"}},
        ],
    }


def test_request_namespace_inner_functions_flattened_to_top_level_functions():
    out = responses_request_to_chat_body(_namespace_tools_body())
    names = [t["function"]["name"] for t in out["tools"]]
    # both inner functions plus the top-level function reach chat/completions
    assert "spawn_agent" in names
    assert "wait_agent" in names
    assert "shell" in names
    assert len(out["tools"]) == 3
    # every flattened entry is a spec-clean chat function tool
    assert all(t["type"] == "function" and "function" in t for t in out["tools"])
    # the inner function's strict flag survives the flatten
    spawn = next(t for t in out["tools"] if t["function"]["name"] == "spawn_agent")
    assert spawn["function"]["strict"] is False


def test_namespace_map_from_tools_maps_inner_functions_to_namespace():
    body = _namespace_tools_body()
    assert namespace_map_from_tools(body["tools"]) == {
        "spawn_agent": "multi_agent_v1",
        "wait_agent": "multi_agent_v1",
    }


def test_namespace_map_from_tools_empty_without_a_namespace():
    tools = [{"type": "function", "name": "shell", "parameters": {"type": "object"}}]
    assert namespace_map_from_tools(tools) == {}
    # non-list / malformed inputs are handled gracefully
    assert namespace_map_from_tools(None) == {}
    assert namespace_map_from_tools([{"type": "namespace", "name": "ns", "tools": ["bad", {}]}]) == {}


def _chat_with_tool_calls(*names_and_ids):
    return {
        "model": "m",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
                    for name, cid in names_and_ids
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }


def test_non_stream_stamps_only_mapped_tool_calls_with_namespace():
    chat = _chat_with_tool_calls(("spawn_agent", "call_1"), ("shell", "call_2"))
    out = chat_response_to_responses_body(
        chat, {"model": "m"}, "resp_1", namespace_map={"spawn_agent": "multi_agent_v1"}
    )
    fcs = {it["name"]: it for it in out["output"] if it["type"] == "function_call"}
    assert fcs["spawn_agent"]["namespace"] == "multi_agent_v1"
    # a call not in the map stays spec-clean (no namespace key)
    assert "namespace" not in fcs["shell"]


def test_non_stream_no_namespace_map_leaves_calls_spec_clean():
    chat = _chat_with_tool_calls(("spawn_agent", "call_1"))
    out = chat_response_to_responses_body(chat, {"model": "m"}, "resp_1")
    fc = next(it for it in out["output"] if it["type"] == "function_call")
    assert "namespace" not in fc


async def test_stream_function_call_stamped_with_namespace():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "spawn_agent", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events, holder = await _run_stream(chunks, namespace_map={"spawn_agent": "multi_agent_v1"})
    added = next(e for e in events
                 if e["type"] == "response.output_item.added"
                 and e["item"]["type"] == "function_call")
    done = next(e for e in events
                if e["type"] == "response.output_item.done"
                and e["item"]["type"] == "function_call")
    # both the opening and the closing item carry the namespace
    assert added["item"]["namespace"] == "multi_agent_v1"
    assert done["item"]["namespace"] == "multi_agent_v1"
    # the terminal output[] entry keeps it too, for a non-streaming-parity reader
    fc = next(it for it in events[-1]["response"]["output"] if it["type"] == "function_call")
    assert fc["namespace"] == "multi_agent_v1"
    # the persisted chaining transcript stays name-only — the backend knows the
    # flattened name, not the namespace.
    tc = holder["assistant_message"]["tool_calls"][0]
    assert "namespace" not in tc
    assert "namespace" not in tc["function"]


async def test_stream_late_name_still_resolves_namespace():
    # The tool name can arrive on a later fragment; the namespace must be resolved
    # when the name is learned so the done item still stamps it.
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"arguments": '{"a":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "spawn_agent", "arguments": "1}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events, _ = await _run_stream(chunks, namespace_map={"spawn_agent": "multi_agent_v1"})
    done = next(e for e in events
                if e["type"] == "response.output_item.done"
                and e["item"]["type"] == "function_call")
    assert done["item"]["name"] == "spawn_agent"
    assert done["item"]["namespace"] == "multi_agent_v1"


async def test_stream_unmapped_tool_call_has_no_namespace_key():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "shell", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events, _ = await _run_stream(chunks, namespace_map={"spawn_agent": "multi_agent_v1"})
    for e in events:
        if e.get("type") in ("response.output_item.added", "response.output_item.done"):
            assert "namespace" not in e["item"]


def test_namespace_tools_dropped_when_flatten_disabled(monkeypatch):
    monkeypatch.setenv("CONVERTER_FLATTEN_NAMESPACE_TOOLS", "false")
    body = _namespace_tools_body()
    out = responses_request_to_chat_body(body)
    names = [t["function"]["name"] for t in out["tools"]]
    # namespace inner functions dropped (old behaviour); only the plain function
    # survives, and no mapping is produced for the response path.
    assert names == ["shell"]
    assert namespace_map_from_tools(body["tools"]) == {}
