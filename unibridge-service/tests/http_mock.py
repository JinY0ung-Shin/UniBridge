"""Answer the app's outbound HTTP from registered responses, on ``httpx.MockTransport``.

The app builds its clients itself (``httpx.AsyncClient(timeout=..., verify=...)``), so
there is no transport to pass in: while ``mock_http()`` is open, every request the real
``httpx.AsyncHTTPTransport``/``httpx.HTTPTransport`` would send (proxy mounts included)
goes to one ``MockTransport`` instead, and nothing reaches the network.

The rules are part of what the tests check:

- a registered response answers one request; several that match the same request
  answer in the order they were registered;
- a request no response answers raises ``httpx.TimeoutException`` in the client (so
  the app's own error handling runs) and fails the block on exit, even when the app
  swallowed the error;
- a registered response that no request used fails the block on exit.

A URL matches on everything but its query, and on the query as a set of parameters
(order does not matter).
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest


def _url_matches(expected: httpx.URL, received: httpx.URL) -> bool:
    return dict(received.params) == dict(expected.params) and (
        received.copy_with(query=None) == expected.copy_with(query=None)
    )


class _Expected:
    def __init__(
        self,
        respond: Callable[[httpx.Request], httpx.Response] | None,
        *,
        url: str | httpx.URL | None,
        method: str | None,
    ) -> None:
        self.respond = respond
        self.url = httpx.URL(url) if url is not None else None
        self.method = method.upper() if method else None
        self.calls = 0

    def matches(self, request: httpx.Request) -> bool:
        return (self.url is None or _url_matches(self.url, request.url)) and (
            self.method is None or request.method == self.method
        )

    def __str__(self) -> str:
        return f"{self.method or 'any'} request on {self.url or 'any URL'}"


class HTTPMock:
    def __init__(self) -> None:
        self.transport = httpx.MockTransport(self._handle)
        self._expected: list[_Expected] = []
        self._requests: list[httpx.Request] = []
        self._unexpected: list[httpx.Request] = []

    def add_response(
        self,
        *,
        url: str | httpx.URL | None = None,
        method: str | None = None,
        status_code: int = 200,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        json = copy.deepcopy(json)

        def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, headers=headers, json=json)

        self._expected.append(_Expected(respond, url=url, method=method))

    def add_exception(
        self,
        exception: Exception,
        *,
        url: str | httpx.URL | None = None,
        method: str | None = None,
    ) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if isinstance(exception, httpx.RequestError):
                exception.request = request
            raise exception

        self._expected.append(_Expected(respond, url=url, method=method))

    def get_requests(
        self, *, url: str | httpx.URL | None = None, method: str | None = None
    ) -> list[httpx.Request]:
        """Every request sent, answered or not, that matches ``url``/``method``."""
        probe = _Expected(None, url=url, method=method)
        return [request for request in self._requests if probe.matches(request)]

    def get_request(
        self, *, url: str | httpx.URL | None = None, method: str | None = None
    ) -> httpx.Request | None:
        requests = self.get_requests(url=url, method=method)
        assert len(requests) <= 1, (
            f"{len(requests)} requests matched; use get_requests or narrow the match"
        )
        return requests[0] if requests else None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self._requests.append(request)
        for expected in self._expected:
            if not expected.calls and expected.matches(request):
                expected.calls += 1
                return expected.respond(request)
        self._unexpected.append(request)
        registered = "".join(f"\n- {expected}" for expected in self._expected)
        raise httpx.TimeoutException(
            f"No response registered for {request.method} {request.url}; "
            f"registered (each answers once):{registered or ' none'}",
            request=request,
        )

    def verify(self) -> None:
        unused = [str(expected) for expected in self._expected if not expected.calls]
        assert not unused, "Registered but never requested:\n- " + "\n- ".join(unused)
        assert not self._unexpected, "Requests no registered response answered:\n- " + (
            "\n- ".join(f"{request.method} {request.url}" for request in self._unexpected)
        )


@contextmanager
def mock_http() -> Iterator[HTTPMock]:
    """Route the app's HTTP into an ``HTTPMock``; on a clean exit, ``verify()`` it."""
    mock = HTTPMock()

    async def handle_async_request(
        _transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await mock.transport.handle_async_request(request)

    def handle_request(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return mock.transport.handle_request(request)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
        patch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
        yield mock
    mock.verify()
