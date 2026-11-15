"""The shared APISIX admin client and the helpers next to it (app.services.apisix_client).

The list/get/put/delete wire shapes are covered in tests/test_services.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import settings
from app.services import apisix_client


def _status_error(status: int, url: str = "http://apisix/apisix/admin/routes/r1") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status, request=request)
    )


# ── Admin API ───────────────────────────────────────────────────────────────


async def test_the_client_is_shared_until_closed():
    await apisix_client.aclose()
    try:
        first = apisix_client._get_client()
        assert apisix_client._get_client() is first
        assert not first.is_closed
    finally:
        await apisix_client.aclose()
    assert apisix_client._client is None
    # Closing twice is harmless.
    await apisix_client.aclose()


async def test_patch_resource_sends_the_body_and_returns_the_value():
    body = {"timeout": {"read": 30}}
    instance = AsyncMock()
    instance.patch.return_value = httpx.Response(
        200, json={"value": {"id": "r1", **body}}, request=httpx.Request("PATCH", "http://test")
    )

    with patch("app.services.apisix_client._get_client", return_value=instance):
        result = await apisix_client.patch_resource("routes", "r1", body)

    url = instance.patch.call_args.args[0]
    assert url == "http://localhost:19180/apisix/admin/routes/r1"
    assert instance.patch.call_args.kwargs["json"] == body
    assert instance.patch.call_args.kwargs["headers"] == {"X-API-KEY": "test-apisix-key"}
    assert result == {"id": "r1", **body}


async def test_an_error_status_raises():
    instance = AsyncMock()
    instance.get.return_value = httpx.Response(503, request=httpx.Request("GET", "http://test"))

    with patch("app.services.apisix_client._get_client", return_value=instance):
        with pytest.raises(httpx.HTTPStatusError):
            await apisix_client.get_resource("routes", "r1")


@pytest.mark.parametrize(
    ("resource", "resource_id"),
    [("nodes", "r1"), ("routes", "../etc"), ("routes", "a/b")],
)
async def test_unsafe_resource_paths_are_refused_before_any_request(resource, resource_id):
    instance = AsyncMock()
    with patch("app.services.apisix_client._get_client", return_value=instance):
        with pytest.raises(ValueError):
            await apisix_client.delete_resource(resource, resource_id)
    instance.delete.assert_not_called()


# ── Missing resources ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "missing"),
    [
        (_status_error(404), True),
        # A status error answers by its status, even when its URL says 404.
        (_status_error(500, "http://apisix/apisix/admin/routes/err-404"), False),
        (_status_error(503), False),
        (RuntimeError("APISIX 404 not found: routes/x"), True),
        (RuntimeError("Route Not Found"), True),
        (httpx.ConnectError("All connection attempts failed"), False),
    ],
)
def test_is_not_found(exc, missing):
    assert apisix_client.is_not_found(exc) is missing


async def test_get_resource_or_none():
    route = {"id": "r1"}
    get = AsyncMock(side_effect=[route, _status_error(404), _status_error(500)])

    with patch("app.services.apisix_client.get_resource", get):
        assert await apisix_client.get_resource_or_none("routes", "r1") == route
        assert await apisix_client.get_resource_or_none("routes", "r1") is None
        with pytest.raises(httpx.HTTPStatusError):
            await apisix_client.get_resource_or_none("routes", "r1")


# ── Route paths ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("route", "paths"),
    [
        ({"uri": "/a/*"}, ["/a/*"]),
        ({"uris": ["/a", "/b/*"]}, ["/a", "/b/*"]),
        # Both count: APISIX can take either, and the shadowing guard reads all.
        ({"uri": "/a", "uris": ["/b"]}, ["/a", "/b"]),
        ({"uri": None, "uris": "/not-a-list"}, []),
        ({"uris": ["/a", 3, None]}, ["/a"]),
        ({}, []),
    ],
)
def test_route_paths(route, paths):
    assert apisix_client.route_paths(route) == paths


# ── Probing an upstream's nodes ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("upstream", "scheme"),
    [({"scheme": "https"}, "https"), ({"scheme": "grpc"}, "http"), ({}, "http")],
)
def test_upstream_scheme(upstream, scheme):
    assert apisix_client.upstream_scheme(upstream) == scheme


@pytest.mark.parametrize(
    ("addr", "host"),
    [("10.0.0.1:8080", "10.0.0.1"), ("[fe80::1]:80", "fe80::1"), ("svc", "svc")],
)
def test_node_host(addr, host):
    assert apisix_client.node_host(addr) == host


def test_upstream_host_header_follows_pass_host(monkeypatch):
    monkeypatch.setattr(settings, "HOST_IP", "192.0.2.10")

    assert apisix_client.upstream_host_header({"pass_host": "node"}, "svc:8080") == "svc"
    assert (
        apisix_client.upstream_host_header(
            {"pass_host": "rewrite", "upstream_host": "api.example.com"}, "svc:8080"
        )
        == "api.example.com"
    )
    # rewrite without a usable upstream_host, and the default "pass", send ours.
    assert apisix_client.upstream_host_header({"pass_host": "rewrite"}, "svc:8080") == "192.0.2.10"
    assert apisix_client.upstream_host_header({}, "svc:8080") == "192.0.2.10"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("10.0.0.1:9100", ("10.0.0.1", 9100)),
        (" [fe80::1]:9400 ", ("fe80::1", 9400)),
        ("[fe80::1]", ("fe80::1", None)),
        ("host.example.com", ("host.example.com", None)),
        ("host:http", ("host", None)),
        ("fe80::1", ("fe80::1", None)),
        (":80", None),
        ("", None),
        (None, None),
    ],
)
def test_split_host_port(address, expected):
    assert apisix_client.split_host_port(address) == expected
