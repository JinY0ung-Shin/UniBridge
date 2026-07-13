"""Small sanitizer branches that guard empty signatures and stray stops."""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.stream_sanitizer import sanitize_events


async def _events() -> AsyncIterator[dict]:
    yield {"type": "content_block_stop", "index": 99}
    yield {"type": "ping"}


async def test_empty_signature_is_dropped_but_real_signature_is_preserved():
    async def source() -> AsyncIterator[dict]:
        yield {"type": "message_start", "message": {"id": "msg_1"}}
        yield {
            "type": "content_block_delta",
            "delta": {"type": "signature_delta", "signature": ""},
        }
        yield {
            "type": "content_block_delta",
            "delta": {"type": "signature_delta", "signature": "sig"},
        }
        yield {"type": "message_stop"}

    output = [event async for event in sanitize_events(source())]
    deltas = [event for event in output if event["type"] == "content_block_delta"]
    assert [event["delta"]["signature"] for event in deltas] == ["sig"]
    assert any(
        event.get("content_block", {}).get("type") == "thinking" for event in output
    )


async def test_stray_content_stop_is_dropped_but_unknown_events_pass_through():
    assert [event async for event in sanitize_events(_events())] == [{"type": "ping"}]
