"""Tests for execute_graphdb_query.

Uses httpx.MockTransport to keep the suite hermetic. The transport responds
to ``POST /repositories/{repo}`` with a fixture chosen by the test.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import httpx

from app.services.query_executor import execute_graphdb_query


SELECT_JSON = {
    "head": {"vars": ["s", "o", "n"]},
    "results": {
        "bindings": [
            {
                "s": {"type": "uri", "value": "http://ex/a"},
                "o": {"type": "bnode", "value": "b0"},
                "n": {
                    "type": "literal",
                    "value": "42",
                    "datatype": "http://www.w3.org/2001/XMLSchema#integer",
                },
            },
            {
                "s": {"type": "uri", "value": "http://ex/b"},
                # 'o' missing intentionally
                "n": {
                    "type": "literal",
                    "value": "true",
                    "datatype": "http://www.w3.org/2001/XMLSchema#boolean",
                },
            },
        ],
    },
}


def _client_with_response(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(base_url="http://gdb:7200", transport=transport)


@pytest.mark.asyncio
async def test_select_maps_bindings_and_coerces_types():
    def handler(request):
        assert request.url.path == "/repositories/repo1"
        assert request.headers["Accept"] == "application/sparql-results+json"
        assert request.headers["Content-Type"] == "application/sparql-query"
        return httpx.Response(200, json=SELECT_JSON)

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client,
            repo="repo1",
            sparql="SELECT ?s ?o ?n WHERE { ?s ?p ?o }",
            statement_type="select",
            limit=100,
        )

    assert resp.columns == ["s", "o", "n"]
    assert resp.rows == [
        ["http://ex/a", "_:b0", 42],
        ["http://ex/b", None, True],
    ]
    assert resp.row_count == 2
    assert resp.truncated is False
    assert resp.graph is None


@pytest.mark.asyncio
async def test_select_truncates_to_limit():
    body = {
        "head": {"vars": ["s"]},
        "results": {"bindings": [
            {"s": {"type": "uri", "value": f"http://ex/{i}"}} for i in range(5)
        ]},
    }
    def handler(_):
        return httpx.Response(200, json=body)

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client, repo="r", sparql="SELECT ?s WHERE {}",
            statement_type="select", limit=3,
        )
    assert resp.row_count == 3
    assert resp.truncated is True


@pytest.mark.asyncio
async def test_ask_returns_boolean_table():
    def handler(_):
        return httpx.Response(200, json={"head": {}, "boolean": True})

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client, repo="r", sparql="ASK { ?s ?p ?o }",
            statement_type="ask", limit=100,
        )
    assert resp.columns == ["boolean"]
    assert resp.rows == [[True]]
    assert resp.row_count == 1


@pytest.mark.asyncio
async def test_ask_defaults_to_false_if_boolean_missing():
    def handler(_):
        return httpx.Response(200, json={"head": {}})

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client, repo="r", sparql="ASK { }",
            statement_type="ask", limit=100,
        )
    assert resp.rows == [[False]]


@pytest.mark.asyncio
async def test_ask_handles_string_boolean_safely():
    """A non-spec server returning {"boolean": "false"} must not be misread as True."""
    def handler(_):
        return httpx.Response(200, json={"head": {}, "boolean": "false"})

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client, repo="r", sparql="ASK { }",
            statement_type="ask", limit=100,
        )
    assert resp.rows == [[False]]


@pytest.mark.asyncio
async def test_ask_handles_string_true():
    def handler(_):
        return httpx.Response(200, json={"head": {}, "boolean": "TRUE"})

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client, repo="r", sparql="ASK { }",
            statement_type="ask", limit=100,
        )
    assert resp.rows == [[True]]


@pytest.mark.asyncio
async def test_construct_returns_graph_field():
    turtle = "@prefix ex: <http://ex/> .\nex:a ex:b ex:c .\n"
    def handler(request):
        assert request.headers["Accept"] == "text/turtle"
        return httpx.Response(200, text=turtle, headers={"Content-Type": "text/turtle"})

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client, repo="r",
            sparql="CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
            statement_type="construct", limit=100,
        )
    assert resp.graph == turtle
    assert resp.columns == []
    assert resp.rows == []
    assert resp.row_count == 0


@pytest.mark.asyncio
async def test_describe_returns_graph_field():
    turtle = "<http://ex/x> <http://ex/p> <http://ex/y> .\n"
    def handler(_):
        return httpx.Response(200, text=turtle)

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client, repo="r", sparql="DESCRIBE <http://ex/x>",
            statement_type="describe", limit=100,
        )
    assert resp.graph == turtle


@pytest.mark.asyncio
async def test_413_when_content_length_exceeds_limit(monkeypatch):
    from app import config as cfg_mod
    monkeypatch.setattr(cfg_mod.settings, "GRAPHDB_MAX_RESPONSE_BYTES", 100)

    big = "x" * 500
    def handler(_):
        return httpx.Response(200, text=big, headers={"Content-Length": "500"})

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r", sparql="DESCRIBE <http://ex/x>",
                statement_type="describe", limit=100,
            )
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_413_when_streamed_body_exceeds_limit(monkeypatch):
    """Force chunked transfer (no Content-Length) so the streaming guard fires."""
    from app import config as cfg_mod
    monkeypatch.setattr(cfg_mod.settings, "GRAPHDB_MAX_RESPONSE_BYTES", 100)

    async def body_gen():
        for _ in range(5):
            yield b"y" * 100

    def handler(_):
        # async generator content forces chunked encoding (no Content-Length)
        return httpx.Response(200, content=body_gen())

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r", sparql="DESCRIBE <http://ex/x>",
                statement_type="describe", limit=100,
            )
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_413_with_misleading_small_content_length(monkeypatch):
    """If Content-Length lies (claims small body, actual body is huge),
    the streaming guard must still fire 413."""
    from app import config as cfg_mod
    monkeypatch.setattr(cfg_mod.settings, "GRAPHDB_MAX_RESPONSE_BYTES", 100)

    async def body_gen():
        for _ in range(5):
            yield b"z" * 100

    def handler(_):
        # Server claims tiny CL but sends a lot — streaming guard must catch.
        return httpx.Response(200, content=body_gen(), headers={"Content-Length": "10"})

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r", sparql="DESCRIBE <http://ex/x>",
                statement_type="describe", limit=100,
            )
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_401_maps_to_502():
    def handler(_):
        return httpx.Response(401, text="Unauthorized")

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r", sparql="SELECT ?s WHERE {}",
                statement_type="select", limit=100,
            )
    assert exc.value.status_code == 502
    assert "authentication" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_4xx_passes_400_with_body_preview():
    def handler(_):
        return httpx.Response(400, text="Lexical error at line 1, column 7.")

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r", sparql="SELECT broken",
                statement_type="select", limit=100,
            )
    assert exc.value.status_code == 400
    assert "Lexical" in exc.value.detail


@pytest.mark.asyncio
async def test_4xx_strips_control_chars_from_preview():
    """Control chars in the upstream body must not appear in the response detail."""
    def handler(_):
        return httpx.Response(400, text="Bad\x00query\x07\x1bhere")

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r", sparql="SELECT broken",
                statement_type="select", limit=100,
            )
    assert exc.value.status_code == 400
    assert "\x00" not in exc.value.detail
    assert "\x07" not in exc.value.detail
    assert "\x1b" not in exc.value.detail
    assert "Badqueryhere" in exc.value.detail


@pytest.mark.asyncio
async def test_404_unknown_repository():
    def handler(_):
        return httpx.Response(404, text="Unknown repository: ghost")

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="ghost", sparql="SELECT ?s WHERE {}",
                statement_type="select", limit=100,
            )
    assert exc.value.status_code == 404
    assert "repository" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_5xx_maps_to_502():
    def handler(_):
        return httpx.Response(503, text="overloaded")

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r", sparql="SELECT ?s WHERE {}",
                statement_type="select", limit=100,
            )
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_timeout_maps_to_504():
    def handler(_):
        raise httpx.TimeoutException("slow")

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r", sparql="SELECT ?s WHERE {}",
                statement_type="select", limit=100,
            )
    assert exc.value.status_code == 504


@pytest.mark.asyncio
async def test_non_utf8_body_maps_to_502():
    invalid = b"\xff\xfe\x00\x01 some bytes"
    def handler(_):
        return httpx.Response(200, content=invalid, headers={"Content-Type": "text/turtle"})

    async with _client_with_response(handler) as client:
        with pytest.raises(HTTPException) as exc:
            await execute_graphdb_query(
                client=client, repo="r",
                sparql="CONSTRUCT { } WHERE { }",
                statement_type="construct", limit=100,
            )
    assert exc.value.status_code == 502
    assert "utf-8" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_xsd_datatypes_coerced():
    body = {
        "head": {"vars": ["i", "f", "b", "s"]},
        "results": {"bindings": [{
            "i": {"type": "literal", "value": "-7",
                  "datatype": "http://www.w3.org/2001/XMLSchema#int"},
            "f": {"type": "literal", "value": "3.14",
                  "datatype": "http://www.w3.org/2001/XMLSchema#double"},
            "b": {"type": "literal", "value": "FALSE",
                  "datatype": "http://www.w3.org/2001/XMLSchema#boolean"},
            "s": {"type": "literal", "value": "hello",
                  "datatype": "http://www.w3.org/2001/XMLSchema#string"},
        }]},
    }
    def handler(_):
        return httpx.Response(200, json=body)

    async with _client_with_response(handler) as client:
        resp = await execute_graphdb_query(
            client=client, repo="r", sparql="SELECT * WHERE {}",
            statement_type="select", limit=100,
        )
    assert resp.rows == [[-7, 3.14, False, "hello"]]
