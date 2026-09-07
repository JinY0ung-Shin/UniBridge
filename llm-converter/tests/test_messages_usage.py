"""Cache accounting must survive both Messages conversion paths."""

import pytest

from app.messages_bridge import (
    openai_response_to_anthropic_body,
    openai_stream_to_anthropic_events,
)


@pytest.mark.parametrize("usage, expected", [
    ({"prompt_tokens": 1000, "completion_tokens": 50,
      "prompt_tokens_details": {"cached_tokens": 800}}, (200, 50, 800, 0)),
    ({"prompt_tokens": 1000, "completion_tokens": 50,
      "prompt_tokens_details": {"cached_tokens": 800, "cache_creation_tokens": 100},
      "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100},
     (100, 50, 800, 100)),
    ({"prompt_tokens": 1000, "completion_tokens": 50,
      "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100},
     (100, 50, 800, 100)),
    ({"prompt_tokens": 1000, "completion_tokens": 50,
      "prompt_tokens_details": {"cached_tokens": None, "cache_creation_tokens": None},
      "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100},
     (100, 50, 800, 100)),
    ({"prompt_tokens": 1000, "completion_tokens": 50,
      "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 100}},
     (100, 50, 800, 100)),
    ({"prompt_tokens": 1000, "completion_tokens": 50,
      "prompt_tokens_details": {"cached_tokens": 1000}}, (0, 50, 1000, 0)),
    ({"prompt_tokens": 1000, "completion_tokens": 50}, (1000, 50, 0, 0)),
    ({"prompt_tokens": None, "completion_tokens": None,
      "prompt_tokens_details": {"cached_tokens": None}}, (0, 0, 0, 0)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 200}},
     (0, 0, 200, 0)),
    ({"prompt_tokens": 100, "prompt_tokens_details": "invalid",
      "cache_read_input_tokens": -1}, (100, 0, 0, 0)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0},
      "cache_read_input_tokens": 80}, (100, 0, 0, 0)),
    (None, (0, 0, 0, 0)),
])
async def test_cache_accounting_in_both_paths(usage, expected):
    expected_usage = dict(zip(
        ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"),
        expected,
    ))
    response = openai_response_to_anthropic_body({"usage": usage})
    assert response["usage"] == expected_usage

    async def chunks():
        yield {"choices": [{"delta": {"content": "OK"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        yield {"choices": [], "usage": usage}

    events = [e async for e in openai_stream_to_anthropic_events(chunks(), model="test")]
    assert events[-2]["type"] == "message_delta"
    assert events[-2]["usage"] == expected_usage


async def test_cumulative_usage_preserves_cache_counts_across_partial_chunks():
    async def chunks():
        yield {"choices": [{"delta": {"content": "OK"}}], "usage": {
            "prompt_tokens": 1000, "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 800},
        }}
        yield {"choices": [], "usage": {
            "prompt_tokens_details": {"cached_tokens": None, "cache_creation_tokens": 100},
        }}
        yield {"choices": [], "usage": {
            "completion_tokens": 50, "prompt_tokens": None, "prompt_tokens_details": None,
        }}

    events = [e async for e in openai_stream_to_anthropic_events(chunks(), model="test")]
    assert events[0]["message"]["usage"] == {
        "input_tokens": 200, "output_tokens": 0,
        "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0,
    }
    assert events[-2]["usage"] == {
        "input_tokens": 100, "output_tokens": 50,
        "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100,
    }


@pytest.mark.parametrize("nested", [False, True])
async def test_cache_creation_ttl_breakdown(nested):
    creation = {"ephemeral_5m_input_tokens": 60, "ephemeral_1h_input_tokens": 40}
    usage = {"prompt_tokens": 1000, "cache_creation_input_tokens": 100}
    if nested:
        usage["prompt_tokens_details"] = {"cache_creation_token_details": creation}
    else:
        usage["cache_creation"] = creation
    assert openai_response_to_anthropic_body({"usage": usage})["usage"]["cache_creation"] == creation

    async def chunks():
        yield {"choices": [{"delta": {"content": "OK"}}], "usage": usage}

    events = [e async for e in openai_stream_to_anthropic_events(chunks(), model="test")]
    assert events[-2]["usage"]["cache_creation"] == creation
