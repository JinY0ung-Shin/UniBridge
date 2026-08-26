"""Tests for the boot-time APISIX upstream provisioning gate (app.main)."""
from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

import app.main as app_main
from app.main import _put_upstream_if_unclaimed


BODY = {
    "name": "unibridge-service",
    "type": "roundrobin",
    "nodes": {"unibridge-service-blue:8000": 1},
}


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://apisix/apisix/admin/upstreams/unibridge-service")
    return httpx.HTTPStatusError(
        f"{status_code}",
        request=request,
        response=httpx.Response(status_code, request=request),
    )


@pytest.fixture
def apisix(monkeypatch):
    """apisix_client stub recording PUTs; ``get`` is set per test."""
    from app.services import apisix_client

    puts: list[tuple[str, str, dict]] = []

    async def _put(resource, resource_id, body):
        puts.append((resource, resource_id, body))
        return body

    monkeypatch.setattr(apisix_client, "put_resource", _put)

    def _on_get(handler):
        monkeypatch.setattr(apisix_client, "get_resource", handler)

    return puts, _on_get


async def test_skips_put_when_upstream_points_at_another_color(apisix):
    puts, on_get = apisix

    async def _get(kind, rid):
        assert (kind, rid) == ("upstreams", "unibridge-service")
        return {"nodes": {"unibridge-service-green:8000": 1}}

    on_get(_get)

    assert await _put_upstream_if_unclaimed("unibridge-service", BODY) is False
    assert puts == []


async def test_puts_when_upstream_is_absent(apisix):
    puts, on_get = apisix

    async def _get(kind, rid):
        raise _status_error(404)

    on_get(_get)

    assert await _put_upstream_if_unclaimed("unibridge-service", BODY) is True
    assert puts == [("upstreams", "unibridge-service", BODY)]


async def test_not_found_style_error_counts_as_absent(apisix):
    """Same "missing resource" convention as _preserve_consumer_restriction."""
    puts, on_get = apisix

    async def _get(kind, rid):
        raise RuntimeError("not found")

    on_get(_get)

    assert await _put_upstream_if_unclaimed("unibridge-service", BODY) is True
    assert puts == [("upstreams", "unibridge-service", BODY)]


async def test_refreshes_when_existing_nodes_match(apisix):
    puts, on_get = apisix

    async def _get(kind, rid):
        return {"nodes": {"unibridge-service-blue:8000": 1}}

    on_get(_get)

    assert await _put_upstream_if_unclaimed("unibridge-service", BODY) is True
    assert puts == [("upstreams", "unibridge-service", BODY)]


async def test_list_form_existing_nodes_are_normalized_before_comparing(apisix):
    puts, on_get = apisix

    async def _get(kind, rid):
        return {
            "nodes": [
                {"host": "unibridge-service-blue", "port": 8000, "weight": 1},
                {"host": "unibridge-service-green", "port": 8000, "weight": 0},
            ]
        }

    on_get(_get)

    assert await _put_upstream_if_unclaimed("unibridge-service", BODY) is True
    assert puts == [("upstreams", "unibridge-service", BODY)]


async def test_list_form_other_color_is_still_a_claim(apisix):
    puts, on_get = apisix

    async def _get(kind, rid):
        return {"nodes": [{"host": "unibridge-service-green", "port": 8000, "weight": 1}]}

    on_get(_get)

    assert await _put_upstream_if_unclaimed("unibridge-service", BODY) is False
    assert puts == []


async def test_upstream_without_usable_nodes_is_repaired(apisix):
    puts, on_get = apisix

    async def _get(kind, rid):
        return {"nodes": {"unibridge-service-green:8000": 0}}

    on_get(_get)

    assert await _put_upstream_if_unclaimed("unibridge-service", BODY) is True
    assert puts == [("upstreams", "unibridge-service", BODY)]


async def test_non_404_admin_error_propagates_instead_of_putting(apisix):
    puts, on_get = apisix

    async def _get(kind, rid):
        raise _status_error(503)

    on_get(_get)

    with pytest.raises(httpx.HTTPStatusError):
        await _put_upstream_if_unclaimed("unibridge-service", BODY)
    assert puts == []


async def test_transport_error_propagates_instead_of_putting(apisix):
    puts, on_get = apisix

    async def _get(kind, rid):
        raise httpx.ConnectError("apisix unreachable")

    on_get(_get)

    with pytest.raises(httpx.ConnectError):
        await _put_upstream_if_unclaimed("unibridge-service", BODY)
    assert puts == []


def test_lifespan_writes_only_the_colorless_upstream_unconditionally():
    """Color-pinned upstreams must go through the gate, never a bare PUT.

    ``litellm`` is colorless (always litellm:4000), so it stays unconditional;
    the one dynamic id is the gate's own PUT.
    """
    tree = ast.parse(Path(app_main.__file__).read_text(encoding="utf-8"))
    written = {
        node.args[1].value if isinstance(node.args[1], ast.Constant) else "<dynamic>"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "put_resource"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "upstreams"
    }

    assert written == {"litellm", "<dynamic>"}
