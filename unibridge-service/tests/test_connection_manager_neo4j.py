from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from app.models import DBConnection
from app.services.connection_manager import ConnectionManager, encrypt_password


@pytest.fixture
def fake_neo4j(monkeypatch):
    """Install a fake neo4j module that records driver creation."""
    module = types.ModuleType("neo4j")
    driver = MagicMock()
    graph_database = MagicMock()
    graph_database.driver.return_value = driver
    module.GraphDatabase = graph_database
    monkeypatch.setitem(sys.modules, "neo4j", module)
    return graph_database, driver


@pytest.fixture
def manager():
    """Clear singleton registries so each test is isolated."""
    mgr = ConnectionManager()
    mgr._engines.clear()
    mgr._ch_clients.clear()
    mgr._neo4j_drivers.clear()
    mgr._db_types.clear()
    mgr._databases.clear()
    return mgr


def _make_neo4j_conn(**overrides) -> DBConnection:
    defaults = {
        "alias": "graph",
        "db_type": "neo4j",
        "host": "neo4j.internal",
        "port": 7687,
        "database": "neo4j",
        "username": "neo4j",
        "password_encrypted": encrypt_password("secret"),
        "protocol": "bolt",
    }
    defaults.update(overrides)
    return DBConnection(**defaults)


@pytest.mark.asyncio
async def test_add_connection_creates_neo4j_driver_and_registers_alias(manager, fake_neo4j):
    graph_database, driver = fake_neo4j

    await manager.add_connection(_make_neo4j_conn())

    graph_database.driver.assert_called_once_with(
        "bolt://neo4j.internal:7687",
        auth=("neo4j", "secret"),
    )
    assert manager.get_neo4j_driver("graph") is driver
    assert manager.get_db_type("graph") == "neo4j"
    assert manager.get_database_name("graph") == "neo4j"


@pytest.mark.asyncio
async def test_remove_connection_closes_neo4j_driver_and_removes_alias(manager, fake_neo4j):
    _, driver = fake_neo4j
    await manager.add_connection(_make_neo4j_conn())

    await manager.remove_connection("graph")

    driver.close.assert_called_once_with()
    assert manager.has_connection("graph") is False
    assert manager.get_db_type("graph") == "unknown"
    with pytest.raises(KeyError, match="No database registered for alias 'graph'"):
        manager.get_database_name("graph")


@pytest.mark.asyncio
async def test_test_connection_verifies_neo4j_connectivity(manager, fake_neo4j):
    _, driver = fake_neo4j
    await manager.add_connection(_make_neo4j_conn())

    result = await manager.test_connection("graph")

    driver.verify_connectivity.assert_called_once_with()
    assert result == (True, "Connection successful")


@pytest.mark.asyncio
async def test_get_status_returns_neo4j_registered_status(manager, fake_neo4j):
    await manager.add_connection(_make_neo4j_conn())

    assert manager.get_status("graph") == {
        "alias": "graph",
        "status": "registered",
        "driver": "neo4j",
    }
