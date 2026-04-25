"""Tests for the backfill_utc_timestamps migration script.

SQLAlchemy's SQLite ``DateTime(timezone=True)`` backend stores tz-aware
datetimes WITHOUT an offset suffix (the offset is stripped on write and
re-applied by ``UtcDateTime.process_result_value``). So the observable
difference between pre-fix and post-fix rows on SQLite is:

    pre-fix  :  'YYYY-MM-DD HH:MM:SS'           (19 chars, no microseconds)
    post-fix :  'YYYY-MM-DD HH:MM:SS.ffffff'    (26 chars, with microseconds)

The lexicographic ``>=`` comparison still misbehaves at the boundary because
the shorter pre-fix string sorts before the longer post-fix bound. Backfill
normalizes legacy values to microsecond precision so lexicographic order
matches chronological order.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AuditLog
from scripts.backfill_utc_timestamps import backfill_database


# Canonical post-backfill shape: 'YYYY-MM-DD HH:MM:SS.ffffff'
CANONICAL_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}$")


@pytest.fixture
async def _mixed_audit_rows(app):
    """Seed audit_logs with one legacy naive row and one aware row."""
    override = app.dependency_overrides[get_db]
    gen = override()
    session: AsyncSession = await gen.__anext__()

    # Modern row (goes through UtcDateTime bind_param → microsecond precision)
    session.add(
        AuditLog(
            timestamp=datetime(2026, 4, 21, 16, 0, 0, tzinfo=timezone.utc),
            user="modern",
            database_alias="proddb",
            sql="SELECT 1",
            status="success",
            row_count=1,
            elapsed_ms=1,
        )
    )
    # Legacy row via raw INSERT — naive, second precision (what pre-fix
    # CURRENT_TIMESTAMP / func.now() would have written).
    await session.execute(
        text(
            "INSERT INTO audit_logs (timestamp, user, database_alias, sql, status, row_count, elapsed_ms) "
            "VALUES (:ts, :u, :d, :s, :st, :rc, :el)"
        ),
        {
            "ts": "2026-04-21 15:00:00",
            "u": "legacy",
            "d": "devdb",
            "s": "SELECT 2",
            "st": "success",
            "rc": 1,
            "el": 2,
        },
    )
    await session.commit()

    yield

    try:
        await gen.aclose()
    except Exception:
        pass


class TestBackfillUtcTimestamps:
    async def test_normalizes_legacy_rows(self, app, seeded_db, _mixed_audit_rows):
        """After backfill, every stored timestamp matches the canonical shape."""
        updated = await backfill_database(engine=seeded_db)
        # At least the one legacy row should have been rewritten.
        assert updated >= 1

        override = app.dependency_overrides[get_db]
        gen = override()
        session: AsyncSession = await gen.__anext__()
        try:
            result = await session.execute(
                text("SELECT user, timestamp FROM audit_logs WHERE user IN ('modern', 'legacy')")
            )
            rows = dict(result.all())
            assert "legacy" in rows and "modern" in rows
            for user, ts in rows.items():
                assert CANONICAL_SHAPE.match(ts), (
                    f"{user} row {ts!r} does not match canonical "
                    f"'YYYY-MM-DD HH:MM:SS.ffffff' shape"
                )
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass

    async def test_idempotent(self, app, seeded_db, _mixed_audit_rows):
        """Second run finds nothing to update."""
        first = await backfill_database(engine=seeded_db)
        assert first >= 1
        second = await backfill_database(engine=seeded_db)
        assert second == 0

    async def test_filter_inclusive_after_backfill(self, app, seeded_db, _mixed_audit_rows):
        """After backfill, a WHERE timestamp >= bound that chronologically equals
        the legacy value actually includes the legacy row.

        Uses the ORM path so the bound is compiled exactly the way production
        code compiles it (which is where the lexicographic bug shows up).
        """
        from sqlalchemy import select

        # Before backfill: the boundary filter EXCLUDES the legacy row because
        # '2026-04-21 15:00:00' < '2026-04-21 15:00:00.000000' lexicographically.
        override = app.dependency_overrides[get_db]
        gen = override()
        session: AsyncSession = await gen.__anext__()
        try:
            bound = datetime(2026, 4, 21, 15, 0, 0, tzinfo=timezone.utc)
            pre = await session.execute(
                select(AuditLog.user)
                .where(AuditLog.timestamp >= bound)
                .where(AuditLog.user == "legacy")
            )
            assert "legacy" not in [r[0] for r in pre.all()], (
                "precondition: legacy row must be excluded by >= bound before backfill"
            )
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass

        # Run backfill.
        await backfill_database(engine=seeded_db)

        # After backfill: the same filter INCLUDES the legacy row.
        override = app.dependency_overrides[get_db]
        gen = override()
        session: AsyncSession = await gen.__anext__()
        try:
            bound = datetime(2026, 4, 21, 15, 0, 0, tzinfo=timezone.utc)
            post = await session.execute(
                select(AuditLog.user)
                .where(AuditLog.timestamp >= bound)
                .where(AuditLog.user == "legacy")
            )
            users = [r[0] for r in post.all()]
            assert "legacy" in users, (
                "legacy row was excluded from boundary >= filter after backfill"
            )
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass
