"""Shared meta-DB helpers: advisory critical sections and JSON settings rows."""

import asyncio
import logging

from pydantic import BaseModel

from app.models import SystemConfig
from app.services import mcp_artifacts, upload_relay
from app.services.meta_store import (
    advisory_section,
    load_config_model,
    save_config_model,
    section_lock,
)


class _FakeSession:
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.executed: list[tuple[str, dict | None]] = []

    def get_bind(self):
        return type("Bind", (), {"dialect": type("Dialect", (), {"name": self.dialect})()})()

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))


async def test_each_section_keeps_its_wire_key():
    """The keys are what the other color's process locks on: never change them."""
    assert upload_relay._STORE_LOCK_KEY == int.from_bytes(b"uprelay1", "big")
    assert mcp_artifacts._STORE_LOCK_KEY == int.from_bytes(b"mcpartf1", "big")
    session = _FakeSession("postgresql")
    async with advisory_section(session, mcp_artifacts._STORE_LOCK_KEY):
        assert section_lock(mcp_artifacts._STORE_LOCK_KEY).locked()
    assert session.executed == [
        ("SELECT pg_advisory_xact_lock(:key)", {"key": mcp_artifacts._STORE_LOCK_KEY})
    ]


async def test_sections_with_different_keys_do_not_wait_on_each_other():
    session = _FakeSession("sqlite")
    async with advisory_section(session, 1):
        assert section_lock(1).locked()
        assert not section_lock(2).locked()
        async with advisory_section(session, 2):
            assert section_lock(2).locked()
    assert not section_lock(1).locked() and not section_lock(2).locked()
    assert session.executed == []


async def test_one_key_admits_one_caller_at_a_time():
    session = _FakeSession("sqlite")
    order: list[str] = []
    inside = asyncio.Event()

    async def first():
        async with advisory_section(session, 7):
            order.append("first in")
            inside.set()
            await asyncio.sleep(0.01)
            order.append("first out")

    async def second():
        await inside.wait()
        async with advisory_section(session, 7):
            order.append("second in")

    await asyncio.gather(first(), second())
    assert order == ["first in", "first out", "second in"]


class _Sample(BaseModel):
    count: int = 3
    name: str = "x"


async def test_a_missing_row_reads_as_the_defaults(db_session):
    log = logging.getLogger("test.meta_store")
    assert await load_config_model(
        db_session, "sample", _Sample, log=log, unreadable_message="bad %s"
    ) == _Sample()


async def test_a_saved_model_reads_back_and_is_overwritten(db_session):
    log = logging.getLogger("test.meta_store")
    await save_config_model(db_session, "sample", _Sample(count=5))
    await save_config_model(db_session, "sample", _Sample(count=6, name="y"))
    row = await db_session.get(SystemConfig, "sample")
    assert row.value == '{"count":6,"name":"y"}'
    assert await load_config_model(
        db_session, "sample", _Sample, log=log, unreadable_message="bad %s"
    ) == _Sample(count=6, name="y")


async def test_an_unreadable_row_reads_as_the_defaults_and_is_logged(db_session, caplog):
    log = logging.getLogger("test.meta_store")
    for value in ("not json", '{"count": "many"}'):
        row = await db_session.get(SystemConfig, "sample")
        if row is None:
            db_session.add(SystemConfig(key="sample", value=value))
        else:
            row.value = value
        await db_session.commit()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="test.meta_store"):
            loaded = await load_config_model(
                db_session, "sample", _Sample, log=log, unreadable_message="Unreadable %s here"
            )
        assert loaded == _Sample()
        assert [(r.name, r.getMessage()) for r in caplog.records] == [
            ("test.meta_store", "Unreadable sample here")
        ]
