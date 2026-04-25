"""Integration test: audit log responses serialize timestamps with UTC offset,
and legacy naive DB rows are tagged UTC by the UtcDateTime type decorator."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AuditLog
from tests.conftest import auth_header


UTC_ISO_SUFFIX = re.compile(r"(\+00:00|Z)$")


@pytest.fixture
async def _seed_mixed_audit_rows(app):
    """Insert one ORM row (aware UTC) and one raw naive row (legacy).

    Mirrors the session-acquisition / teardown pattern used by
    ``_seed_audit_logs`` in ``tests/test_admin.py``: grab a session from the
    overridden ``get_db`` async-generator dependency, and close the generator
    afterwards (the in-memory DB itself is torn down with the ``app`` fixture).
    """
    override = app.dependency_overrides[get_db]
    gen = override()
    session: AsyncSession = await gen.__anext__()

    # Row A: modern, via ORM (exercises UtcDateTime bind_param)
    aware = AuditLog(
        timestamp=datetime(2026, 4, 22, 0, 0, 0, tzinfo=timezone.utc),
        user="modern",
        database_alias="proddb",
        sql="SELECT 1",
        status="success",
        row_count=1,
        elapsed_ms=5,
    )
    session.add(aware)
    await session.flush()

    # Row B: legacy raw INSERT with a naive timestamp string (simulates
    # pre-fix rows persisted before UtcDateTime existed). On SELECT, the
    # UtcDateTime.process_result_value path must tag this as UTC.
    await session.execute(
        text(
            "INSERT INTO audit_logs "
            "(timestamp, user, database_alias, sql, status, row_count, elapsed_ms) "
            "VALUES (:ts, :user, :db, :sql, :status, :rc, :el)"
        ),
        {
            "ts": "2026-04-21T15:00:00",  # naive, as pre-fix code would have stored
            "user": "legacy",
            "db": "devdb",
            "sql": "SELECT 2",
            "status": "success",
            "rc": 1,
            "el": 10,
        },
    )
    await session.commit()

    yield

    try:
        await gen.aclose()
    except StopAsyncIteration:
        pass


class TestAuditLogTimezone:
    async def test_timestamps_have_utc_offset(
        self, client, admin_token, _seed_mixed_audit_rows
    ):
        resp = await client.get(
            "/admin/query/audit-logs",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        logs = resp.json()
        assert isinstance(logs, list)

        users = {log["user"]: log for log in logs}
        assert "modern" in users, "ORM-inserted row missing from response"
        assert "legacy" in users, "Raw-naive-inserted row missing from response"

        for key in ("modern", "legacy"):
            ts = users[key]["timestamp"]
            assert ts is not None, f"{key} row is missing 'timestamp'"
            assert UTC_ISO_SUFFIX.search(ts), (
                f"{key} row timestamp {ts!r} does not end with '+00:00' or 'Z' — "
                "UtcDateTime should force UTC offset on serialization"
            )
