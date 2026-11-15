"""Reading a body whole, up to a size limit."""

from __future__ import annotations

from collections.abc import AsyncIterator


async def read_capped(chunks: AsyncIterator[bytes], limit: int) -> bytes | None:
    """All of ``chunks`` as one bytes object, or None once they pass ``limit``.

    Counted as they arrive: an announced length is the sender's word. The
    chunks are kept as they come and joined once, so the body is copied a
    single time (a growing buffer would be copied again and again). Reading
    stops at the chunk that passes the limit.
    """
    parts: list[bytes] = []
    size = 0
    async for chunk in chunks:
        size += len(chunk)
        if size > limit:
            return None
        parts.append(chunk)
    return b"".join(parts)
