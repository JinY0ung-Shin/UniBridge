"""ConnectionManager on its SQLAlchemy side (Postgres/MSSQL engines), on SQLite."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import DBConnection
from app.services.connection_manager import ConnectionManager


def _fresh_manager() -> ConnectionManager:
    """A manager outside the singleton, with every dict registry the class declares."""
    mgr = object.__new__(ConnectionManager)
    for name, annotation in ConnectionManager.__annotations__.items():
        if str(annotation).startswith("dict["):
            setattr(mgr, name, {})
    return mgr


@pytest.fixture
async def mgr():
    manager = _fresh_manager()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    manager._engines["main"] = engine
    manager._db_types["main"] = "postgres"
    yield manager
    await engine.dispose()


async def test_one_failing_connection_does_not_stop_the_rest(caplog):
    manager = _fresh_manager()
    broken = DBConnection(alias="broken", db_type="postgres")
    fine = DBConnection(alias="fine", db_type="postgres")
    add = AsyncMock(side_effect=[RuntimeError("bad password"), None])

    with patch.object(manager, "add_connection", add), caplog.at_level(logging.ERROR):
        await manager.initialize([broken, fine])

    assert [call.args[0].alias for call in add.await_args_list] == ["broken", "fine"]
    assert "Failed to initialize connection 'broken'" in caplog.text


async def test_test_connection_runs_a_query(mgr):
    assert await mgr.test_connection("main") == (True, "Connection successful")


async def test_status_reports_the_pool_or_not_registered(mgr):
    status = mgr.get_status("main")
    assert status["alias"] == "main"
    assert status["status"] == "registered"
    assert {"pool_size", "checked_in", "checked_out", "overflow"} <= set(status)

    assert mgr.get_status("ghost") == {"alias": "ghost", "status": "not_registered"}


async def test_remove_disposes_the_engine():
    manager = _fresh_manager()
    engine = AsyncMock()
    manager._engines["main"] = engine
    manager._db_types["main"] = "postgres"

    await manager.remove_connection("main")

    engine.dispose.assert_awaited_once()
    assert manager.get_db_type("main") == "unknown"
    assert not manager.has_connection("main")


@pytest.mark.parametrize(
    "getter,message",
    [
        ("get_engine", "No engine registered"),
        ("get_neo4j_driver", "No Neo4j driver registered"),
        ("get_graphdb_client", "No GraphDB client registered"),
    ],
)
def test_missing_aliases_raise_a_named_key_error(getter, message):
    with pytest.raises(KeyError, match=message):
        getattr(_fresh_manager(), getter)("ghost")
