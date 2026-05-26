"""ConnectionManager graphdb branch tests.

Uses the shared singleton — each test acquires/releases the alias so order
doesn't matter. Avoids hitting the network by patching httpx.AsyncClient
to a fake.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from app.services import connection_manager as cm_mod
from app.services.connection_manager import ConnectionManager, encrypt_password


def _make_conn(alias: str = "kg"):
    """Build a minimal DBConnection-like object for ConnectionManager."""
    conn = MagicMock()
    conn.alias = alias
    conn.db_type = "graphdb"
    conn.host = "graphdb.local"
    conn.port = 7200
    conn.database = "my-repo"
    conn.username = "admin"
    conn.password_encrypted = encrypt_password("pw")
    conn.protocol = "http"
    conn.secure = None
    conn.pool_size = None
    conn.max_overflow = None
    conn.query_timeout = 30
    return conn


@pytest.mark.asyncio
async def test_add_remove_graphdb_round_trip(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.base_url = kwargs["base_url"]
            self.closed = False

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    cm = ConnectionManager()
    conn = _make_conn("kg-add")
    await cm.add_connection(conn)

    assert "kg-add" in cm.list_aliases()
    assert cm.has_connection("kg-add")
    assert cm.get_db_type("kg-add") == "graphdb"
    assert cm.get_database_name("kg-add") == "my-repo"
    assert cm.get_status("kg-add") == {
        "alias": "kg-add",
        "status": "registered",
        "driver": "httpx (graphdb)",
    }

    client = cm.get_graphdb_client("kg-add")
    assert str(client.base_url) == "http://graphdb.local:7200"

    await cm.remove_connection("kg-add")
    assert "kg-add" not in cm.list_aliases()
    assert not cm.has_connection("kg-add")
    assert client.closed is True


@pytest.mark.asyncio
async def test_dispose_all_closes_graphdb(monkeypatch):
    closed = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.base_url = kwargs["base_url"]

        async def aclose(self):
            closed.append(self.base_url)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    cm = ConnectionManager()
    await cm.add_connection(_make_conn("kg-dispose"))
    await cm.dispose_all()
    assert any("graphdb.local" in str(b) for b in closed)


@pytest.mark.asyncio
async def test_test_connection_uses_ask_filter_false(monkeypatch):
    captured = {}

    class FakeResponse:
        def __init__(self, status_code=200):
            self.status_code = status_code

    class FakeClient:
        def __init__(self, **kwargs):
            self.base_url = kwargs["base_url"]

        async def post(self, path, content, headers):
            captured["path"] = path
            captured["body"] = content
            captured["headers"] = dict(headers)
            return FakeResponse(200)

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    cm = ConnectionManager()
    await cm.add_connection(_make_conn("kg-ping"))
    ok, msg = await cm.test_connection("kg-ping")
    assert ok is True
    assert msg == "Connection successful"
    assert captured["path"] == "/repositories/my-repo"
    assert captured["body"] == "ASK { FILTER(false) }"
    assert captured["headers"]["Content-Type"] == "application/sparql-query"
    assert captured["headers"]["Accept"] == "application/sparql-results+json"
    await cm.remove_connection("kg-ping")


@pytest.mark.asyncio
async def test_test_connection_failure_returns_message(monkeypatch):
    class FakeResponse:
        status_code = 500

    class FakeClient:
        def __init__(self, **kwargs):
            self.base_url = kwargs["base_url"]

        async def post(self, *a, **k):
            return FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    cm = ConnectionManager()
    await cm.add_connection(_make_conn("kg-fail"))
    ok, msg = await cm.test_connection("kg-fail")
    assert ok is False
    assert "500" in msg
    await cm.remove_connection("kg-fail")


@pytest.mark.asyncio
async def test_test_connection_exception_returns_message(monkeypatch):
    """Verify that httpx exceptions during test_connection are caught and reported."""
    class ExplodingClient:
        def __init__(self, **kwargs):
            self.base_url = kwargs["base_url"]

        async def post(self, *a, **k):
            raise RuntimeError("network down")

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", ExplodingClient)

    cm = ConnectionManager()
    await cm.add_connection(_make_conn("kg-explode"))
    ok, msg = await cm.test_connection("kg-explode")
    assert ok is False
    assert "network down" in msg
    await cm.remove_connection("kg-explode")


@pytest.mark.asyncio
async def test_update_pool_metrics_skips_graphdb(monkeypatch):
    """graphdb has no SQLAlchemy pool — update_pool_metrics must not crash."""
    class FakeClient:
        def __init__(self, **kwargs):
            self.base_url = kwargs["base_url"]

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    cm = ConnectionManager()
    await cm.add_connection(_make_conn("kg-metrics"))
    # No exception:
    cm.update_pool_metrics("kg-metrics")
    cm.update_pool_metrics()
    await cm.remove_connection("kg-metrics")
