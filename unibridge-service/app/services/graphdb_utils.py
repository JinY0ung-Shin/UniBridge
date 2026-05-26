from __future__ import annotations

from urllib.parse import quote

import httpx


class GraphDBResponseTooLarge(Exception):
    """Raised when an upstream GraphDB response exceeds the configured cap."""


def graphdb_repository_path(repository_id: str) -> str:
    """Return the URL path for GraphDB's read query endpoint.

    Repository IDs are path segments, not path/query fragments. Encoding the
    full value keeps slashes, dot-dot segments, and question marks inside the
    repository segment.
    """
    return f"/repositories/{quote(repository_id, safe='')}"


async def read_capped_response(resp: httpx.Response, max_bytes: int) -> bytes:
    content_length = resp.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise GraphDBResponseTooLarge(
                    "GraphDB response exceeded GRAPHDB_MAX_RESPONSE_BYTES"
                )
        except ValueError:
            pass

    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise GraphDBResponseTooLarge(
                "GraphDB response exceeded GRAPHDB_MAX_RESPONSE_BYTES"
            )
    return bytes(buf)
