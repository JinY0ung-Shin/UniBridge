"""One-time migration: normalize legacy naive timestamps to the canonical
SQLAlchemy SQLite ``DateTime(timezone=True)`` storage format.

Context
-------
Before the timezone-consistency fix landed, timestamp columns used
``CURRENT_TIMESTAMP`` / ``func.now()`` defaults on SQLite which stored values as
``'YYYY-MM-DD HH:MM:SS'`` (second precision, no offset). After the fix, writes
go through ``UtcDateTime.process_bind_param`` which hands SQLAlchemy a
tz-aware ``datetime``; SQLAlchemy's SQLite backend strips the offset and
writes microsecond precision — ``'YYYY-MM-DD HH:MM:SS.ffffff'`` (26 chars, no
offset).

SQLite compares TEXT columns lexicographically. A bound value
``'2026-04-21 15:00:00.000000'`` sorts AFTER a legacy row
``'2026-04-21 15:00:00'``, so ``timestamp >= bound`` incorrectly excludes
rows at the exact boundary.

This script rewrites every legacy (no-microsecond) value in every
``UtcDateTime`` column to the canonical microsecond form so lexicographic
order matches chronological order.

Usage
-----
::

    python -m scripts.backfill_utc_timestamps

Idempotent: re-running finds 0 rows to update.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database import engine as default_engine
from app.db_types import UtcDateTime
from app.models import Base

logger = logging.getLogger(__name__)


def _datetime_columns() -> Iterable[tuple[str, str]]:
    """Yield (table_name, column_name) for every ``UtcDateTime`` column."""
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, UtcDateTime):
                yield table.name, col.name


def _canonical_format(dt: datetime) -> str:
    """Format a UTC-aware datetime the way SQLAlchemy's SQLite
    ``DateTime(timezone=True)`` backend stores it.

    Empirically verified: SQLAlchemy strips the offset on write and emits
    microsecond precision. Example:

        datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            → '2026-01-01 12:00:00.000000'
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


async def backfill_database(engine: AsyncEngine | None = None) -> int:
    """Rewrite every legacy (no-microsecond) value in every ``UtcDateTime``
    column to the canonical microsecond form. Returns total rows updated.

    A value is considered legacy if it is not NULL and does not contain a
    ``'.'`` (which indicates microseconds and the post-fix format). Post-fix
    rows — ``'YYYY-MM-DD HH:MM:SS.ffffff'`` — are skipped, making the script
    idempotent.

    Accepts an optional ``engine`` override for testing; defaults to the
    production meta-DB engine from ``app.database``.

    Raises ``RuntimeError`` on non-SQLite backends: the lexicographic-compare
    bug this script fixes is SQLite-specific (PostgreSQL/MSSQL use native
    timestamp types), and the script relies on SQLite ``rowid``.
    """
    eng = engine if engine is not None else default_engine
    if eng.dialect.name != "sqlite":
        raise RuntimeError(
            f"backfill_utc_timestamps only supports SQLite "
            f"(got dialect={eng.dialect.name!r}). Other backends store "
            f"datetimes as native timestamp types and do not need this fix."
        )
    total = 0
    async with eng.begin() as conn:
        existing_tables = set(
            await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        )
        for table_name, col_name in _datetime_columns():
            if table_name not in existing_tables:
                # Model declares the table but the DB hasn't created it yet
                # (e.g., older deployment, lazy migration). Skip safely.
                logger.info("%s: table not present; skipping", table_name)
                continue
            # Select only legacy rows: non-NULL and no '.' (no microseconds).
            # rowid is SQLite-specific; the dialect guard above ensures we only
            # reach this code on SQLite.
            select_sql = text(
                f"SELECT rowid, {col_name} FROM {table_name} "
                f"WHERE {col_name} IS NOT NULL "
                f"AND {col_name} NOT LIKE '%.%'"
            )
            result = await conn.execute(select_sql)
            rows = result.fetchall()
            if not rows:
                continue

            col_updated = 0
            for rowid, value in rows:
                try:
                    # datetime.fromisoformat handles both 'T' and ' ' separators
                    # on Python 3.11+.
                    dt = datetime.fromisoformat(value)
                except ValueError:
                    logger.warning(
                        "skipping unparseable value in %s.%s rowid=%s: %r",
                        table_name,
                        col_name,
                        rowid,
                        value,
                    )
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                canonical = _canonical_format(dt)
                await conn.execute(
                    text(
                        f"UPDATE {table_name} SET {col_name} = :v WHERE rowid = :rid"
                    ),
                    {"v": canonical, "rid": rowid},
                )
                col_updated += 1

            total += col_updated
            logger.info("%s.%s: backfilled %d rows", table_name, col_name, col_updated)

    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    n = asyncio.run(backfill_database())
    cols = sum(1 for _ in _datetime_columns())
    print(f"Updated {n} rows across {cols} UtcDateTime columns.")
