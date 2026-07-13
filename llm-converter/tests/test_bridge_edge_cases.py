"""Defensive bridge-shape tests for malformed and uncommon provider payloads."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from app.messages_bridge import (
    anthropic_request_to_openai_body,
    openai_response_to_anthropic_body,
    openai_stream_to_anthropic_events,
)
from app.responses_bridge import (
    chat_response_to_responses_body,
    chat_stream_to_responses_events,
    responses_request_to_chat_body,
)


async def _as_async(items: Iterable[dict]) -> AsyncIterator[dict]:
    for item in items:
        yield item


async def _collect(items) -> list[dict]:
    return [item async for item in items]


def test_messages_request_rejects_malformed_content_without_crashing():
    converted = anthropic_request_to_openai_body(
        {
            "model": "m",
            "system": [None, {"type": "text", "text": "top"}],
            "messages": [
                {"role": "system", "content": "plain"},
                {"role": "system", "content": 123},
                {"role": "assistant", "content": "answer"},
                {"role": "assistant", "content": [None]},
                {"role": "user", "content": 123},
                {
                    "role": "user",
                    "content": [
                        None,
                        {"type": "image", "source": "bad"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "",
                            },
                        },
                        {"type": "tool_result", "tool_use_id": "a", "content": [None]},
                        {
                            "type": "tool_result",
                            "tool_use_id": "b",
                            "content": {"ok": True},
                        },
                    ],
                },
            ],
            "tool_choice": {"type": "unsupported"},
        }
    )

    assert converted["messages"] == [
        {"role": "system", "content": "top"},
        {"role": "system", "content": "plain"},
        {"role": "system", "content": ""},
        {"role": "assistant", "content": "answer"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "a", "content": ""},
        {"role": "tool", "tool_call_id": "b", "content": '{"ok": true}'},
    ]
    assert "tool_choice" not in converted


def test_messages_request_preserves_all_supported_optional_fields_and_skips_junk():
    result = anthropic_request_to_openai_body(
        {
            "model": "m",
            "temperature": 0.2,
            "top_p": 0.9,
            "stream": True,
            "stream_options": {"include_usage": False, "custom": True},
            "messages": [
                None,
                {"role": "system", "content": [{"type": "text", "text": "late"}]},
            ],
            "tools": [None, {"name": ""}, {"name": "valid", "input_schema": {}}],
        }
    )

    assert result["temperature"] == 0.2
    assert result["top_p"] == 0.9
    assert result["stream_options"] == {"include_usage": False, "custom": True}
    assert result["messages"] == [{"role": "system", "content": "late"}]
    assert [tool["function"]["name"] for tool in result["tools"]] == ["valid"]


async def test_messages_stream_new_complete_restatement_replaces_old_arguments():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ""}}]}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"city":"Seoul"}'}}
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"Busan"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]

    events = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), "m"))
    start = next(
        event
        for event in events
        if event.get("content_block", {}).get("type") == "tool_use"
    )
    argument_fragments = [
        event["delta"]["partial_json"]
        for event in events
        if event.get("delta", {}).get("type") == "input_json_delta"
    ]
    assert start["content_block"]["id"] == "call_1"
    assert start["content_block"]["name"] == "weather"
    assert argument_fragments == ['{"city":"Busan"}']


async def test_messages_error_and_nonstream_tool_junk_fall_back_safely():
    stream_events = await _collect(
        openai_stream_to_anthropic_events(_as_async([{"error": "bad"}]), "m")
    )
    assert stream_events == [
        {"type": "error", "error": {"type": "api_error", "message": "upstream error"}}
    ]
    converted = openai_response_to_anthropic_body(
        {
            "id": "chat_1",
            "model": "m",
            "choices": [
                {
                    "message": {"tool_calls": [None]},
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )
    assert converted["content"] == []
    assert converted["stop_reason"] == "tool_use"


async def test_messages_stream_tolerates_malformed_chunks_and_flushes_before_new_kinds():
    chunks = [
        {"choices": [None]},
        {"choices": [{"delta": "invalid"}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            None,
                            {
                                "id": "call_0",
                                "function": {"name": "first", "arguments": "{}"},
                            },
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"reasoning_content": "think"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_1",
                                "function": {"name": "second", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {"reasoning_content": "late"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]

    events = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), "m"))
    starts = [
        event["content_block"]["type"]
        for event in events
        if event["type"] == "content_block_start"
    ]
    assert starts == ["tool_use", "thinking", "tool_use", "text", "thinking"]
    assert events[-2]["delta"]["stop_reason"] == "end_turn"
    assert events[-1]["type"] == "message_stop"


def test_responses_request_public_boundary_handles_fallback_shapes():
    converted = responses_request_to_chat_body(
        {
            "model": "m",
            "input": [
                None,
                {"type": "message", "role": "developer", "content": None},
                {"type": "message", "role": "assistant", "content": 123},
                {
                    "type": "message",
                    "role": "unexpected",
                    "content": [
                        None,
                        {"type": "input_image", "image_url": {}},
                        {
                            "type": "input_image",
                            "image_url": {"url": "https://images.test/a.png"},
                            "detail": "low",
                        },
                        {"type": "refusal", "refusal": "no"},
                    ],
                },
                {"type": "function_call", "name": "f", "arguments": {"a": 1}},
                {
                    "type": "function_call_output",
                    "call_id": "c",
                    "output": {"ok": True},
                },
            ],
            "tools": [
                None,
                {"type": "web_search"},
                {"type": "function", "function": {}},
                {"type": "function", "function": {"name": "nested"}},
            ],
            "tool_choice": {"type": "auto"},
            "text": {"format": {"type": "json_object"}},
        }
    )

    assert [message["role"] for message in converted["messages"]] == [
        "system",
        "assistant",
        "user",
        "assistant",
        "tool",
    ]
    assert converted["messages"][2]["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://images.test/a.png", "detail": "low"},
        }
    ]
    assert converted["messages"][3]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert converted["messages"][4]["content"] == '{"ok": true}'
    assert converted["tools"][0]["function"]["name"] == "nested"
    assert converted["tool_choice"] == "auto"
    assert converted["response_format"] == {"type": "json_object"}

    invalid = responses_request_to_chat_body(
        {
            "input": 123,
            "tools": "bad",
            "tool_choice": "invalid",
            "text": {"format": "invalid"},
        }
    )
    assert invalid["messages"] == []
    assert "tools" not in invalid
    assert "tool_choice" not in invalid
    assert "response_format" not in invalid

    unsupported = responses_request_to_chat_body(
        {
            "input": [],
            "tool_choice": {"type": "unsupported"},
            "text": {"format": {"type": "text"}},
        }
    )
    assert "tool_choice" not in unsupported
    assert "response_format" not in unsupported


def test_responses_request_forwards_sampling_metadata_and_user():
    result = responses_request_to_chat_body(
        {
            "model": "m",
            "input": "hi",
            "temperature": 0.1,
            "top_p": 0.8,
            "parallel_tool_calls": False,
            "metadata": {"tenant": "a"},
            "user": "user-1",
        }
    )
    assert result["temperature"] == 0.1
    assert result["top_p"] == 0.8
    assert result["parallel_tool_calls"] is False
    assert result["metadata"] == {"tenant": "a"}
    assert result["user"] == "user-1"
    filtered = chat_response_to_responses_body(
        {
            "model": "m",
            "choices": [
                {"message": {"content": "blocked"}, "finish_reason": "content_filter"}
            ],
        },
        {"model": "m"},
        "resp_filtered",
    )
    assert filtered["status"] == "incomplete"
    assert filtered["incomplete_details"] == {"reason": "content_filter"}


def test_responses_nonstream_reasoning_skips_junk_and_synthesizes_call_id():
    chat = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "reasoning_content": "thought",
                    "tool_calls": [
                        None,
                        {"function": {"name": "tool", "arguments": {"a": 1}}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    result = chat_response_to_responses_body(chat, {"model": "m"}, "resp_1")
    assert [item["type"] for item in result["output"]] == ["reasoning", "function_call"]
    call = result["output"][1]
    assert call["call_id"].startswith("call_")
    assert chat["choices"][0]["message"]["tool_calls"][1]["id"] == call["call_id"]


async def test_responses_stream_scalar_error_uses_safe_fallback():
    holder = {}
    events = await _collect(
        chat_stream_to_responses_events(
            _as_async([{"error": "bad"}]),
            response_id="resp_error",
            request_body={"model": "m"},
            holder=holder,
        )
    )
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"] == {
        "code": "server_error",
        "message": "upstream error",
    }


async def test_responses_stream_tolerates_shape_junk_and_switches_output_kinds():
    holder = {}
    chunks = [
        {"choices": []},
        {"choices": [{"delta": "invalid"}]},
        {"choices": [{"delta": {"reasoning_content": "think"}}]},
        {"choices": [{"delta": {"refusal": "no"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            None,
                            {"index": "bad"},
                            {"index": 0, "id": "call_0", "function": {"arguments": "{"}},
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "late-name", "arguments": "}"}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"refusal": "after tool"}, "finish_reason": "stop"}]},
    ]

    events = await _collect(
        chat_stream_to_responses_events(
            _as_async(chunks),
            response_id="resp_1",
            request_body={"model": "m"},
            holder=holder,
        )
    )
    assert events[-1]["type"] == "response.completed"
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert any(event["type"] == "response.refusal.done" for event in events)
    calls = [item for item in events[-1]["response"]["output"] if item["type"] == "function_call"]
    assert calls[0]["name"] == "late-name"


async def test_responses_stream_closes_reasoning_before_tool_and_at_stream_end():
    holder = {}
    reasoning_then_tool = [
        {"choices": [{"delta": {"reasoning_content": "think"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "function": {"name": "tool", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    events = await _collect(
        chat_stream_to_responses_events(
            _as_async(reasoning_then_tool),
            response_id="resp_2",
            request_body={"model": "m"},
            holder=holder,
        )
    )
    assert events[-1]["type"] == "response.completed"
    reasoning_done = next(
        event["sequence_number"]
        for event in events
        if event["type"] == "response.reasoning_text.done"
    )
    tool_added = next(
        event["sequence_number"]
        for event in events
        if event["type"] == "response.output_item.added"
        and event["item"]["type"] == "function_call"
    )
    assert reasoning_done < tool_added

    trailing_holder = {}
    trailing_reasoning = [{"choices": [{"delta": {"reasoning_content": "only"}}]}]
    trailing_events = await _collect(
        chat_stream_to_responses_events(
            _as_async(trailing_reasoning),
            response_id="resp_3",
            request_body={"model": "m"},
            holder=trailing_holder,
        )
    )
    assert any(event["type"] == "response.reasoning_text.done" for event in trailing_events)
