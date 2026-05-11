"""Data-level verification of migration 0005 (N-strike).

Seeds alert_state rows at revision 0004, upgrades to 0005, asserts fail_count
matches spec §6 matrix, then downgrades and verifies alert_notified restoration.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


# alembic.ini lives at unibridge-service/alembic.ini; resolve absolutely so the
# test is independent of pytest's working directory.
ALEMBIC_INI = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
)


@pytest.fixture
def alembic_sqlite_url():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        # env.py uses async_engine_from_config, so the URL must use an async
        # driver. We hand alembic the aiosqlite URL and use the equivalent
        # sync URL for direct seeding/assertions below.
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _alembic_cfg(db_path: str) -> Config:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def test_migration_0005_fail_count_backfill_and_downgrade(alembic_sqlite_url):
    db_path = alembic_sqlite_url
    cfg = _alembic_cfg(db_path)
    sync_url = f"sqlite:///{db_path}"

    # Bring schema up to revision 0004 (pre-N-strike).
    command.upgrade(cfg, "0004_alert_owner_routing")

    engine = create_engine(sync_url, future=True)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT OR REPLACE INTO alert_state "
            "(alert_type, target, status, since, display_target, alert_notified, updated_at) "
            "VALUES "
            "('db_health', 'a', 'alert', CURRENT_TIMESTAMP, 'a', 1, CURRENT_TIMESTAMP),"
            "('db_health', 'b', 'alert', CURRENT_TIMESTAMP, 'b', 0, CURRENT_TIMESTAMP),"
            "('db_health', 'c', 'ok',    CURRENT_TIMESTAMP, 'c', 1, CURRENT_TIMESTAMP)"
        ))

    command.upgrade(cfg, "0005_alert_trigger_after_failures")

    with engine.begin() as conn:
        rows = {
            row.target: row
            for row in conn.execute(text(
                "SELECT target, status, fail_count FROM alert_state ORDER BY target"
            ))
        }

    assert rows["a"].status == "alert"
    assert rows["a"].fail_count == 2
    assert rows["b"].status == "ok"
    assert rows["b"].fail_count == 1
    assert rows["c"].status == "ok"
    assert rows["c"].fail_count == 0

    command.downgrade(cfg, "0004_alert_owner_routing")

    with engine.begin() as conn:
        result = {
            row.target: row
            for row in conn.execute(text(
                "SELECT target, status, alert_notified FROM alert_state ORDER BY target"
            ))
        }

    # 'a' (originally alert+notified) round-trips cleanly.
    assert result["a"].status == "alert"
    assert result["a"].alert_notified in (1, True)
    # 'b' was originally (alert, alert_notified=FALSE). The upgrade
    # intentionally collapses that into (ok, fail_count=N-1); the downgrade
    # cannot reconstruct the original because (ok, fail_count>0) is a
    # legitimate new-model state too. Verify the documented one-way behavior
    # rather than a clean round-trip.
    assert result["b"].status == "ok"
    assert result["b"].alert_notified in (1, True)
    assert result["c"].status == "ok"
    assert result["c"].alert_notified in (1, True)

    engine.dispose()
