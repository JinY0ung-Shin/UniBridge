"""Unit tests for app.messages_bridge.

The bridge swaps in a hand-rolled Anthropic ↔ OpenAI translator in place of
the LiteLLM Anthropic adapter that mis-serializes tool calls and reasoning
content for the GaussO3.2 / vLLM stack. The conversion is purely structural,
so all tests work on plain Python dicts — no HTTP, no SSE encoding.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Iterable, List

from app.messages_bridge import (
    anthropic_request_to_openai_body,
    openai_response_to_anthropic_body,
    openai_stream_to_anthropic_events,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _as_async(items: Iterable[dict]) -> AsyncIterator[dict]:
    for it in items:
        yield it


async def _collect(aiter: AsyncIterator[dict]) -> List[dict]:
    return [e async for e in aiter]


# ---------------------------------------------------------------------------
# Request: Anthropic → OpenAI
# ---------------------------------------------------------------------------


class TestRequestConversion:
    def test_simple_user_text(self):
        body = {
            "model": "GaussO3.2-260402-vllm",
            "max_tokens": 1024,
            "stream": True,
            "messages": [{"role": "user", "content": "흐음"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["model"] == "GaussO3.2-260402-vllm"
        assert out["max_tokens"] == 1024
        assert out["stream"] is True
        assert out["messages"] == [{"role": "user", "content": "흐음"}]
        # Streaming requests must ask for inline usage so message_delta carries
        # real ``output_tokens`` instead of zero.
        assert out["stream_options"]["include_usage"] is True

    def test_system_top_level_becomes_first_message(self):
        body = {
            "model": "m",
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert out["messages"][1] == {"role": "user", "content": "hi"}

    def test_structured_system_blocks_flattened(self):
        body = {
            "model": "m",
            "system": [
                {"type": "text", "text": "Part A. "},
                {"type": "text", "text": "Part B."},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["messages"][0] == {"role": "system", "content": "Part A. Part B."}

    def test_assistant_history_with_tool_use(self):
        body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "what's in /tmp?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "I'll run ls."},
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {"command": "ls /tmp"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": "file1\nfile2",
                        }
                    ],
                },
            ],
        }
        out = anthropic_request_to_openai_body(body)
        # Thinking is dropped; text + tool_use collapse into one assistant
        # message; tool_result becomes a tool message.
        assert out["messages"] == [
            {"role": "user", "content": "what's in /tmp?"},
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [
                    {
                        "id": "tu_1",
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "arguments": json.dumps({"command": "ls /tmp"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tu_1", "content": "file1\nfile2"},
        ]

    def test_assistant_message_with_only_tool_use_has_empty_string_content(self):
        # vLLM (via LiteLLM ``hosted_vllm``) rejects assistant messages whose
        # ``content`` key is missing with a 422 "field required" — even when
        # ``tool_calls`` is set. ``None`` round-trips through LiteLLM's
        # pydantic ``exclude_none`` serialization as a missing key, so the
        # bridge sends an empty string instead.
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {},
                        }
                    ],
                }
            ],
        }
        out = anthropic_request_to_openai_body(body)
        msg = out["messages"][0]
        assert "content" in msg
        assert msg["content"] == ""
        assert "tool_calls" in msg
        # The whole point of this test: the key survives JSON round-trip.
        assert "content" in json.loads(json.dumps(msg))

    def test_tool_result_structured_content_flattened(self):
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [
                                {"type": "text", "text": "line1\n"},
                                {"type": "text", "text": "line2"},
                            ],
                        }
                    ],
                }
            ],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["messages"] == [
            {"role": "tool", "tool_call_id": "tu_1", "content": "line1\nline2"}
        ]

    def test_tool_result_image_content_forwarded_after_tool_message(self):
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [
                                {"type": "text", "text": "image loaded"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "iVBORw0KGgo=",
                                    },
                                },
                            ],
                        },
                    ],
                },
            ],
        }

        out = anthropic_request_to_openai_body(body)

        assert [m["role"] for m in out["messages"]] == ["assistant", "tool", "user"]
        assert out["messages"][1] == {
            "role": "tool",
            "tool_call_id": "tu_1",
            "content": "image loaded",
        }
        assert out["messages"][2] == {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        }

    def test_user_image_base64_converted_to_openai_multimodal_content(self):
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                }
            ],
        }

        out = anthropic_request_to_openai_body(body)

        assert out["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,iVBORw0KGgo=",
                        },
                    },
                ],
            }
        ]

    def test_user_image_url_converted_to_openai_multimodal_content(self):
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": "https://example.com/image.png",
                            },
                            "detail": "high",
                        },
                    ],
                }
            ],
        }

        out = anthropic_request_to_openai_body(body)

        assert out["messages"][0] == {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://example.com/image.png",
                        "detail": "high",
                    },
                },
            ],
        }

    def test_tools_definition_converted(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "Bash",
                    "description": "Run a shell command",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                }
            ],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "Bash",
                    "description": "Run a shell command",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
            }
        ]

    def test_tool_choice_variants(self):
        for ant, openai in (
            ({"type": "auto"}, "auto"),
            ({"type": "any"}, "required"),
            ({"type": "none"}, "none"),
            (
                {"type": "tool", "name": "Bash"},
                {"type": "function", "function": {"name": "Bash"}},
            ),
        ):
            body = {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": "Bash", "description": "", "input_schema": {}}],
                "tool_choice": ant,
            }
            out = anthropic_request_to_openai_body(body)
            assert out["tool_choice"] == openai, f"failed for {ant!r}"

    def test_thinking_field_is_dropped(self):
        """vLLM enables reasoning via chat template, not via a request flag —
        forwarding Anthropic's ``thinking`` would be a no-op at best and an
        unknown-field error at worst."""
        body = {
            "model": "m",
            "thinking": {"type": "enabled", "budget_tokens": 512},
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthropic_request_to_openai_body(body)
        assert "thinking" not in out

    def test_output_config_effort_becomes_reasoning_effort(self):
        """Anthropic ``output_config.effort`` maps to OpenAI ``reasoning_effort``
        (mirrors the Responses bridge)."""
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "high"},
        }
        out = anthropic_request_to_openai_body(body)
        assert out["reasoning_effort"] == "high"
        assert "output_config" not in out

    def test_output_config_without_effort_is_ignored(self):
        # A missing/empty effort must not emit a null reasoning_effort that
        # upstream might reject.
        for oc in ({}, {"effort": None}, {"format": {"type": "text"}}):
            body = {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "output_config": oc,
            }
            out = anthropic_request_to_openai_body(body)
            assert "reasoning_effort" not in out, f"failed for {oc!r}"

    def test_output_config_format_becomes_response_format(self):
        """Anthropic ``output_config.format`` (json_schema) maps to OpenAI
        ``response_format`` with the schema nested under ``json_schema``."""
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "name": "Person",
                    "schema": {"type": "object", "properties": {"n": {"type": "string"}}},
                    "strict": True,
                }
            },
        }
        out = anthropic_request_to_openai_body(body)
        assert out["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "Person",
                "schema": {"type": "object", "properties": {"n": {"type": "string"}}},
                "strict": True,
            },
        }
        assert "output_config" not in out

    def test_output_config_format_json_object_and_name_default(self):
        # json_object passes through; a json_schema without a name gets the
        # "response" default (OpenAI/vLLM require a name).
        obj = anthropic_request_to_openai_body(
            {"model": "m", "messages": [], "output_config": {"format": {"type": "json_object"}}}
        )
        assert obj["response_format"] == {"type": "json_object"}

        named = anthropic_request_to_openai_body(
            {"model": "m", "messages": [], "output_config": {"format": {"type": "json_schema", "schema": {}}}}
        )
        assert named["response_format"]["json_schema"]["name"] == "response"

    def test_output_config_effort_and_format_coexist(self):
        body = {
            "model": "m",
            "messages": [],
            "output_config": {"effort": "low", "format": {"type": "json_object"}},
        }
        out = anthropic_request_to_openai_body(body)
        assert out["reasoning_effort"] == "low"
        assert out["response_format"] == {"type": "json_object"}

    def test_metadata_user_id_becomes_user(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"user_id": "u-123"},
        }
        out = anthropic_request_to_openai_body(body)
        assert out["user"] == "u-123"
        assert "metadata" not in out

    def test_metadata_without_user_id_is_ignored(self):
        out = anthropic_request_to_openai_body(
            {"model": "m", "messages": [], "metadata": {"other": "x"}}
        )
        assert "user" not in out

    def test_tool_strict_flag_preserved(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"name": "Strict", "description": "", "input_schema": {}, "strict": True},
                {"name": "Loose", "description": "", "input_schema": {}},
            ],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["tools"][0]["function"].get("strict") is True
        # Tools without an explicit strict flag don't gain one.
        assert "strict" not in out["tools"][1]["function"]

    def test_tool_choice_disable_parallel_maps_to_parallel_tool_calls(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "Bash", "description": "", "input_schema": {}}],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }
        out = anthropic_request_to_openai_body(body)
        assert out["tool_choice"] == "auto"
        assert out["parallel_tool_calls"] is False

    def test_tool_choice_without_disable_parallel_omits_flag(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "auto"},
        }
        out = anthropic_request_to_openai_body(body)
        assert "parallel_tool_calls" not in out

    def test_stop_sequences_renamed(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "stop_sequences": ["\n\n", "END"],
        }
        out = anthropic_request_to_openai_body(body)
        assert out["stop"] == ["\n\n", "END"]
        assert "stop_sequences" not in out

    def test_user_turn_mixing_text_and_tool_result_keeps_tool_adjacent(self):
        # A single user turn carrying BOTH a text block and a tool_result must
        # convert to [tool, user] — the tool message has to immediately follow
        # the assistant tool_calls it answers, with no intervening user message
        # (OpenAI/vLLM reject that). claude_agent_sdk batches a follow-up prompt
        # together with tool_result blocks in one turn routinely.
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "also, hurry"},
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": "done"},
                    ],
                },
            ],
        }
        out = anthropic_request_to_openai_body(body)
        assert [m["role"] for m in out["messages"]] == ["assistant", "tool", "user"]
        # tool message sits immediately after the assistant tool_calls.
        assert out["messages"][1] == {"role": "tool", "tool_call_id": "tu_1", "content": "done"}
        assert out["messages"][2] == {"role": "user", "content": "also, hurry"}

    def test_user_turn_mixing_image_text_and_tool_result_keeps_tool_adjacent(self):
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "also inspect this"},
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": "done"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": "/9j/4AAQSkZJRg==",
                            },
                        },
                    ],
                },
            ],
        }

        out = anthropic_request_to_openai_body(body)

        assert [m["role"] for m in out["messages"]] == ["assistant", "tool", "user"]
        assert out["messages"][1] == {"role": "tool", "tool_call_id": "tu_1", "content": "done"}
        assert out["messages"][2] == {
            "role": "user",
            "content": [
                {"type": "text", "text": "also inspect this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQSkZJRg=="},
                },
            ],
        }


# ---------------------------------------------------------------------------
# Streaming response: OpenAI SSE → Anthropic SSE
# ---------------------------------------------------------------------------


class TestStreamConversion:
    async def test_renamed_reasoning_field_maps_to_thinking(self):
        """vLLM renamed ``reasoning_content`` → ``reasoning``; reading only the
        legacy key silently dropped thinking from streams."""
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "reasoning": "THINK-A "}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning": "THINK-B"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "ANSWER"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        out = await _collect(
            openai_stream_to_anthropic_events(_as_async(chunks), model="m")
        )
        thinking = [
            e["delta"]["thinking"]
            for e in out
            if e["type"] == "content_block_delta"
            and e["delta"]["type"] == "thinking_delta"
        ]
        assert thinking == ["THINK-A ", "THINK-B"]
        starts = [
            e["content_block"]["type"]
            for e in out
            if e["type"] == "content_block_start"
        ]
        assert starts == ["thinking", "text"]

    async def test_mirrored_reasoning_fields_emit_once(self):
        """Variants that mirror the same text to BOTH keys must not double-emit."""
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "T", "reasoning": "T"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        out = await _collect(
            openai_stream_to_anthropic_events(_as_async(chunks), model="m")
        )
        thinking = [
            e["delta"]["thinking"]
            for e in out
            if e["type"] == "content_block_delta"
            and e["delta"]["type"] == "thinking_delta"
        ]
        assert thinking == ["T"]

    async def test_reasoning_then_content_then_tool_call(self):
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": "The user"}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": " said hi."}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "!"}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "Bash", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"command":'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"ls"}'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 30}},
        ]

        out = await _collect(
            openai_stream_to_anthropic_events(_as_async(chunks), model="m")
        )
        types = [e["type"] for e in out]

        # Expected structure: message_start → thinking block → text block →
        # tool_use block → message_delta → message_stop.
        assert types == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

        starts = [e for e in out if e["type"] == "content_block_start"]
        assert [s["content_block"]["type"] for s in starts] == ["thinking", "text", "tool_use"]
        assert [s["index"] for s in starts] == [0, 1, 2]
        # The synthesized tool_use must carry the real upstream id/name.
        assert starts[2]["content_block"]["id"] == "call_1"
        assert starts[2]["content_block"]["name"] == "Bash"

        # finish_reason mapping.
        msg_delta = next(e for e in out if e["type"] == "message_delta")
        assert msg_delta["delta"]["stop_reason"] == "tool_use"
        assert msg_delta["usage"]["output_tokens"] == 30
        # Usage arrives on the final (empty-choices) chunk, after message_start
        # already went out with input_tokens=0 — the terminal message_delta must
        # restate the real prompt count so accounting isn't stuck at zero.
        assert msg_delta["usage"]["input_tokens"] == 100

    async def test_finish_reason_stop_maps_to_end_turn(self):
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        md = next(e for e in out if e["type"] == "message_delta")
        assert md["delta"]["stop_reason"] == "end_turn"

    async def test_finish_reason_length_maps_to_max_tokens(self):
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        md = next(e for e in out if e["type"] == "message_delta")
        assert md["delta"]["stop_reason"] == "max_tokens"

    async def test_finish_reason_content_filter_maps_to_end_turn(self):
        # content_filter has no Anthropic analogue; mapping it to 'stop_sequence'
        # (with a null stop_sequence field) would be self-contradictory, so it
        # maps to the always-valid 'end_turn'.
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "content_filter"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        md = next(e for e in out if e["type"] == "message_delta")
        assert md["delta"]["stop_reason"] == "end_turn"
        assert md["delta"]["stop_sequence"] is None

    async def test_upstream_error_chunk_becomes_terminal_error_event(self):
        # An upstream ``data: {"error": {...}}`` chunk must terminate with an
        # Anthropic error event, not be dropped and finish as an empty success.
        chunks = [
            {"choices": [{"delta": {"content": "partial"}}]},
            {"error": {"type": "rate_limit_error", "message": "slow down"}},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        assert out[-1] == {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "slow down"},
        }
        # No successful terminus was emitted after the error.
        assert not any(e["type"] in ("message_delta", "message_stop") for e in out)

    async def test_streaming_message_delta_restates_input_tokens(self):
        chunks = [
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            # include_usage delivers token counts only on the terminal chunk.
            {"choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 7}},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        md = next(e for e in out if e["type"] == "message_delta")
        assert md["usage"]["input_tokens"] == 42
        assert md["usage"]["output_tokens"] == 7

    async def test_empty_content_chunks_do_not_split(self):
        """An empty ``delta.content`` arriving mid-reasoning must not open a
        spurious text block — the bridge treats falsy payload values as
        no-ops, mirroring the upstream-side empty-delta drop sanitizer."""
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "a"}}]},
            {"choices": [{"delta": {"content": ""}}]},
            {"choices": [{"delta": {"reasoning_content": "b"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        starts = [e for e in out if e["type"] == "content_block_start"]
        # Only one thinking block — empty content was a no-op.
        assert [s["content_block"]["type"] for s in starts] == ["thinking"]

    async def test_empty_stream_still_emits_valid_frame(self):
        """A completely empty upstream stream must still produce a structurally
        valid Anthropic response so the downstream SDK can finalize cleanly."""
        out = await _collect(openai_stream_to_anthropic_events(_as_async([]), model="m"))
        types = [e["type"] for e in out]
        assert types == ["message_start", "message_delta", "message_stop"]

    async def test_two_parallel_tool_calls_get_separate_blocks(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "function": {"name": "ToolA", "arguments": "{"},
                                }
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
                                {"index": 0, "function": {"arguments": "}"}}
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
                                    "index": 1,
                                    "id": "call_b",
                                    "function": {"name": "ToolB", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert len(starts) == 2
        assert starts[0]["content_block"]["name"] == "ToolA"
        assert starts[0]["content_block"]["id"] == "call_a"
        assert starts[1]["content_block"]["name"] == "ToolB"
        assert starts[1]["content_block"]["id"] == "call_b"

    async def test_interleaved_parallel_tool_call_arguments_keep_metadata(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "function": {"name": "ToolA", "arguments": ""},
                                },
                                {
                                    "index": 1,
                                    "id": "call_b",
                                    "function": {"name": "ToolB", "arguments": ""},
                                },
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
                                {"index": 0, "function": {"arguments": '{"a":'}},
                                {"index": 1, "function": {"arguments": '{"b":'}},
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
                                {"index": 0, "function": {"arguments": "1}"}},
                                {"index": 1, "function": {"arguments": "2}"}},
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]

        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert [s["content_block"]["id"] for s in starts] == ["call_a", "call_b"]
        assert [s["content_block"]["name"] for s in starts] == ["ToolA", "ToolB"]

        args_by_index = {}
        for start in starts:
            idx = start["index"]
            args_by_index[start["content_block"]["id"]] = "".join(
                e["delta"]["partial_json"]
                for e in out
                if e["type"] == "content_block_delta"
                and e["index"] == idx
                and e["delta"]["type"] == "input_json_delta"
            )
        assert args_by_index == {"call_a": '{"a":1}', "call_b": '{"b":2}'}

    @staticmethod
    def _tool_args(out: List[dict]) -> dict:
        """Reconstruct {tool_use_id: concatenated_partial_json} from events."""
        result = {}
        for start in (e for e in out if e["type"] == "content_block_start"):
            if start["content_block"].get("type") != "tool_use":
                continue
            idx = start["index"]
            result[start["content_block"]["id"]] = "".join(
                e["delta"]["partial_json"]
                for e in out
                if e["type"] == "content_block_delta"
                and e["index"] == idx
                and e["delta"]["type"] == "input_json_delta"
            )
        return result

    async def test_vllm_full_args_restated_in_finish_chunk_not_duplicated(self):
        # vLLM/GLM dialect (#31437): the tool parser emits the COMPLETE call in
        # one delta, then the finish chunk re-emits the SAME full arguments.
        # Naively appending duplicates the JSON ('{...}{...}') and the SDK can't
        # parse the tool input — add_arguments must treat the restatement as a
        # replacement, not a continuation.
        full = '{"path":"skills/foo/SKILL.md","content":"hi"}'
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "mcp__repo__write_file",
                                        "arguments": full,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": full}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        args = self._tool_args(out)
        assert args == {"call_1": full}
        assert json.loads(args["call_1"]) == {
            "path": "skills/foo/SKILL.md",
            "content": "hi",
        }

    async def test_vllm_finish_chunk_strips_id_and_name_but_they_are_preserved(self):
        # The finish chunk drops id/type/name (#31437) and re-sends full args.
        # The id/name from the first delta must survive, and args must not double.
        full = '{"id":"r-1"}'
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_x",
                                    "type": "function",
                                    "function": {
                                        "name": "mcp__system__describe_system",
                                        "arguments": full,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": full}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        starts = [e for e in out if e["type"] == "content_block_start"]
        tool_start = next(s for s in starts if s["content_block"]["type"] == "tool_use")
        assert tool_start["content_block"]["id"] == "call_x"
        assert tool_start["content_block"]["name"] == "mcp__system__describe_system"
        assert self._tool_args(out) == {"call_x": full}

    async def test_tool_call_delta_without_index_attributed_to_last_call(self):
        # sglang #5661 / vLLM stripped chunks omit ``index`` on continuation
        # fragments. They must attach to the open call instead of being dropped
        # (which previously truncated the arguments).
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "Bash", "arguments": '{"command":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"function": {"arguments": '"ls -la"}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        out = await _collect(openai_stream_to_anthropic_events(_as_async(chunks), model="m"))
        args = self._tool_args(out)
        assert args == {"call_1": '{"command":"ls -la"}'}
        assert json.loads(args["call_1"]) == {"command": "ls -la"}


# ---------------------------------------------------------------------------
# Non-streaming response: OpenAI body → Anthropic body
# ---------------------------------------------------------------------------


class TestNonStreamingResponseConversion:
    def test_basic_text_response(self):
        body = {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello!",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        out = openai_response_to_anthropic_body(body)
        assert out["role"] == "assistant"
        assert out["model"] == "m"
        assert out["stop_reason"] == "end_turn"
        assert out["content"] == [{"type": "text", "text": "Hello!"}]
        assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_renamed_reasoning_field_becomes_thinking_block(self):
        body = {
            "id": "chatcmpl-2",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning": "Let me think.",
                        "content": "Hello!",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        out = openai_response_to_anthropic_body(body)
        assert out["content"][0] == {"type": "thinking", "thinking": "Let me think."}
        assert out["content"][1] == {"type": "text", "text": "Hello!"}

    def test_reasoning_plus_text_plus_tool(self):
        body = {
            "id": "chatcmpl-2",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "Let me think.",
                        "content": "Sure.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "Bash",
                                    "arguments": '{"command":"ls"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        out = openai_response_to_anthropic_body(body)
        assert out["stop_reason"] == "tool_use"
        # Order: thinking → text → tool_use.
        types = [b["type"] for b in out["content"]]
        assert types == ["thinking", "text", "tool_use"]
        tu = out["content"][2]
        assert tu["id"] == "call_1"
        assert tu["name"] == "Bash"
        assert tu["input"] == {"command": "ls"}

    def test_content_filter_finish_maps_to_end_turn(self):
        body = {
            "model": "m",
            "choices": [{"message": {"content": "redacted"}, "finish_reason": "content_filter"}],
        }
        out = openai_response_to_anthropic_body(body)
        assert out["stop_reason"] == "end_turn"
        assert out["stop_sequence"] is None

    def test_malformed_tool_arguments_fall_back_to_empty_input(self):
        body = {
            "id": "chatcmpl-3",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "X", "arguments": "{not valid"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        out = openai_response_to_anthropic_body(body)
        tu = next(b for b in out["content"] if b["type"] == "tool_use")
        assert tu["input"] == {}
