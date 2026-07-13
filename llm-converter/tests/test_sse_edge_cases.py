"""Malformed and unterminated SSE framing must fail soft, not poison a stream."""

from __future__ import annotations

import httpx

from app.sse import iter_openai_sse_chunks


async def _parse(body: bytes) -> list[dict]:
    response = httpx.Response(200, content=body)
    return [chunk async for chunk in iter_openai_sse_chunks(response)]


async def test_empty_non_json_and_non_object_frames_are_skipped(caplog):
    body = (
        b"\n"
        b"data: definitely-not-json\n\n"
        b"data: [1, 2, 3]\n\n"
        b"data: {\"ok\": true}\n\n"
        b"data: [DONE]\n\n"
    )

    assert await _parse(body) == [{"ok": True}]
    assert "skipping non-JSON" in caplog.text


async def test_trailing_frame_without_blank_line_is_emitted():
    assert await _parse(b'data: {"tail": 1}') == [{"tail": 1}]


async def test_trailing_done_and_invalid_payloads_are_dropped(caplog):
    assert await _parse(b"data: [DONE]") == []
    assert await _parse(b"data: broken") == []
    assert "dropping trailing non-JSON" in caplog.text
