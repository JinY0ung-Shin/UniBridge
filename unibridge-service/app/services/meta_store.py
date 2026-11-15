"""Small helpers over the meta DB that several services share.

Critical sections that hold across both blue/green colors
(:func:`advisory_section`), and settings kept as one JSON model in a
``system_config`` row (:func:`load_config_model`, :func:`save_config_model`).
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SystemConfig

ConfigModel = TypeVar("ConfigModel", bound=BaseModel)

# One lock per event loop and key: the service has a single loop, but an
# asyncio.Lock belongs to the loop that first waits on it (each test runs its own).
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


def section_lock(key: int) -> asyncio.Lock:
    """The in-process lock of the section ``key`` on the running loop."""
    return _locks.setdefault(asyncio.get_running_loop(), {}).setdefault(key, asyncio.Lock())


@asynccontextmanager
async def advisory_section(db: AsyncSession, key: int) -> AsyncIterator[None]:
    """One caller at a time inside the section ``key`` (a Postgres bigint).

    An asyncio lock within this process and, on Postgres, a transaction-level
    advisory lock across processes (the other blue/green color). The advisory
    lock goes with the transaction, so the block must end in a commit or a
    rollback; the caller does that. SQLite, the legacy single-process store,
    has only the in-process lock.
    """
    async with section_lock(key):
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        yield


async def load_config_model(
    db: AsyncSession,
    key: str,
    model: type[ConfigModel],
    *,
    log: logging.Logger,
    unreadable_message: str,
) -> ConfigModel:
    """The ``model`` stored under ``key``; its defaults when the row is missing
    or does not validate (then ``unreadable_message % key`` is logged)."""
    row = await db.get(SystemConfig, key)
    if row is None:
        return model()
    try:
        return model.model_validate_json(row.value)
    except ValueError:  # pydantic's ValidationError, bad JSON included
        log.warning(unreadable_message, key)
        return model()


async def save_config_model(db: AsyncSession, key: str, value: BaseModel) -> None:
    """Store ``value`` as JSON under ``key`` and commit."""
    row = await db.get(SystemConfig, key)
    payload = value.model_dump_json()
    if row is None:
        db.add(SystemConfig(key=key, value=payload))
    else:
        row.value = payload
    await db.commit()
