"""read_capped: a whole body up to a limit, and the readers built on it."""

import httpx
import pytest

from app.services import mcp_remote
from app.services.capped_read import read_capped
from app.services.graphdb_utils import GraphDBResponseTooLarge, read_capped_response


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


class _Counting:
    """Chunks that record how many were taken."""

    def __init__(self, *parts: bytes) -> None:
        self.parts = parts
        self.taken = 0

    async def __aiter__(self):
        for part in self.parts:
            self.taken += 1
            yield part


async def test_a_body_up_to_the_limit_comes_back_whole():
    assert await read_capped(_chunks(b"ab", b"cd"), 4) == b"abcd"
    assert await read_capped(_chunks(), 0) == b""


async def test_a_body_past_the_limit_is_refused_and_reading_stops():
    chunks = _Counting(b"ab", b"cde", b"never read")
    assert await read_capped(chunks.__aiter__(), 4) is None
    assert chunks.taken == 2


def _streamed(body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))


async def test_graphdb_reader_refuses_a_large_answer():
    assert await read_capped_response(_streamed(b"12345"), 5) == b"12345"
    with pytest.raises(GraphDBResponseTooLarge):
        await read_capped_response(_streamed(b"123456"), 5)
    # An announced length over the cap is refused before reading.
    with pytest.raises(GraphDBResponseTooLarge):
        await read_capped_response(_streamed(b"1", {"content-length": "9"}), 5)


async def test_remote_mcp_reader_refuses_a_large_answer():
    assert await mcp_remote._read_capped(_streamed(b"12345"), 5) == b"12345"
    with pytest.raises(mcp_remote.RemoteError, match="The answer is over 5 bytes"):
        await mcp_remote._read_capped(_streamed(b"123456"), 5)
