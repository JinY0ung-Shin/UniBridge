"""Helpers the converter tests share."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterable

import httpx

import app.main as converter_main


def mock_upstream(
    monkeypatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.AsyncClient]:
    """Answer every upstream request the converter makes with *handler*.

    Patched through *monkeypatch*, so the real client factory is back after the
    test. Returns the clients the converter makes, in order, for tests that
    check they were closed.
    """
    transport = httpx.MockTransport(handler)
    clients: list[httpx.AsyncClient] = []

    def factory(timeout):
        client = httpx.AsyncClient(transport=transport, timeout=timeout)
        clients.append(client)
        return client

    monkeypatch.setattr(converter_main, "_make_client", factory)
    return clients


def openai_sse(chunks: Iterable[dict]) -> bytes:
    """A chat/completions SSE body: one ``data:`` frame per chunk, then ``[DONE]``."""
    frames = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def parse_sse(raw: str) -> list[dict]:
    """The JSON payload of every ``data:`` line, in order."""
    return [
        json.loads(line[5:].lstrip())
        for line in raw.splitlines()
        if line.startswith("data:")
    ]


async def as_async(items: Iterable[dict]) -> AsyncIterator[dict]:
    for item in items:
        yield item


async def collect(aiter: AsyncIterator[dict]) -> list[dict]:
    return [item async for item in aiter]
