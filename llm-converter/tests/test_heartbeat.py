"""Unit tests for app.sse.with_heartbeat.

The heartbeat keeps a streaming SSE response's byte flow alive during long
upstream silence so intermediaries (nginx/APISIX/LBs) with idle read timeouts
don't drop the socket. It must: forward real chunks unchanged, inject ``: ping``
only while idle, never lose/reorder a chunk, disable cleanly when interval<=0,
and tear down the source generator on early close.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from app.sse import _HEARTBEAT, with_heartbeat


async def _gen(items, delay=0.0) -> AsyncIterator[bytes]:
    for it in items:
        if delay:
            await asyncio.sleep(delay)
        yield it


async def test_passthrough_fast_stream_no_heartbeat():
    out = [c async for c in with_heartbeat(_gen([b"a", b"b", b"c"]), interval=0.05)]
    assert out == [b"a", b"b", b"c"]


async def test_disabled_when_interval_non_positive():
    out = [c async for c in with_heartbeat(_gen([b"a", b"b"]), interval=0)]
    assert out == [b"a", b"b"]


async def test_injects_heartbeat_while_idle_then_delivers_chunk():
    # One chunk after ~3 heartbeat intervals of silence.
    out = [c async for c in with_heartbeat(_gen([b"data"], delay=0.16), interval=0.05)]
    assert out[-1] == b"data"
    beats = out[:-1]
    assert beats and all(b == _HEARTBEAT for b in beats)
    # No chunk lost or duplicated.
    assert out.count(b"data") == 1


async def test_source_exception_propagates():
    class Boom(Exception):
        pass

    async def src() -> AsyncIterator[bytes]:
        yield b"a"
        raise Boom("upstream blew up")

    out = []
    with pytest.raises(Boom):
        async for c in with_heartbeat(src(), interval=0.05):
            out.append(c)
    assert out == [b"a"]


async def test_teardown_after_real_chunk_runs_source_finally():
    # Teardown in the window right after a chunk is delivered (pending is None):
    # the source generator is suspended at its yield, so with_heartbeat must
    # close it explicitly or its finally (upstream cleanup) would not run.
    closed = {"v": False}

    async def src() -> AsyncIterator[bytes]:
        try:
            yield b"first"
            while True:
                await asyncio.sleep(0.5)
                yield b"x"
        finally:
            closed["v"] = True

    agen = with_heartbeat(src(), interval=0.05)
    assert await agen.__anext__() == b"first"
    await agen.aclose()
    assert closed["v"] is True


async def test_source_is_torn_down_on_early_close():
    closed = {"v": False}

    async def src() -> AsyncIterator[bytes]:
        try:
            while True:
                await asyncio.sleep(0.5)
                yield b"x"
        finally:
            closed["v"] = True

    agen = with_heartbeat(src(), interval=0.05)
    # Pull one heartbeat (source is still idle), then close early.
    first = await agen.__anext__()
    assert first == _HEARTBEAT
    await agen.aclose()
    # Give the cancelled pending __anext__ a tick to run the source's finally.
    await asyncio.sleep(0.01)
    assert closed["v"] is True
