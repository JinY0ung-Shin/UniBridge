"""One-off migration: copy the meta store from SQLite to PostgreSQL.

Context
-------
The meta store defaults to a file-based SQLite DB (``data/meta.db``). Blue-green
deployment (``scripts/deploy-bluegreen.sh``) requires a *networked* meta store
because two app containers run side-by-side and SQLite cannot be safely written
by two processes at once. This script moves an existing SQLite meta store into a
PostgreSQL database so the cutover keeps all registered connections, API keys,
roles, audit logs, etc.

What it does
------------
1. Creates the schema on the (empty) Postgres target from the current ORM models
   and stamps ``alembic_version`` to head — exactly how the app bootstraps a
   fresh DB. (Skipped if the target already has the schema.)
2. Copies every table in FK-dependency order, streaming in batches. Values pass
   through the SQLAlchemy column types, so ``UtcDateTime`` timestamps are read
   back as UTC-aware and re-stored as ``timestamptz`` correctly, and booleans
   convert from SQLite 0/1 to real Postgres booleans.
3. Resets each table's identity/serial sequence to ``max(id)`` so the next
   INSERT in the live app does not collide with a copied primary key.

Encrypted columns (``*_encrypted``) are copied verbatim — they stay valid as
long as the target deployment uses the **same ``ENCRYPTION_KEY``** as the
source. Verify this before cutting over, or the app cannot decrypt credentials.

Usage
-----
Run from the ``unibridge-service`` directory::

    # source defaults to settings.META_DB_URL when it is SQLite
    python -m scripts.migrate_sqlite_to_postgres \
        --target postgresql+asyncpg://unibridge:PASS@db:5432/unibridge

    # explicit source, wipe a non-empty target first, preview only
    python -m scripts.migrate_sqlite_to_postgres \
        --source sqlite+aiosqlite:///data/meta.db \
        --target postgresql+asyncpg://unibridge:PASS@db:5432/unibridge \
        --truncate --dry-run

The target may also be supplied via the ``TARGET_META_DB_URL`` env var. The
script refuses to write into a target that already contains data unless
``--truncate`` is given, so a re-run does not duplicate or collide. After a
successful run, point ``META_DB_URL`` at the Postgres URL and deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings
from app.database import ALEMBIC_HEAD_REVISION
from app.models import Base

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("migrate_sqlite_to_postgres")

# Tables in FK-dependency order (parents first). Reverse for deletes.
TABLES = list(Base.metadata.sorted_tables)


def _normalize_source(url: str) -> str:
    """Coerce a SQLite URL to the async ``aiosqlite`` driver."""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        raise SystemExit(f"--source must be a SQLite URL, got: {parsed.drivername}")
    if parsed.drivername == "sqlite":
        parsed = parsed.set(drivername="sqlite+aiosqlite")
    return parsed.render_as_string(hide_password=False)


def _normalize_target(url: str) -> str:
    """Coerce a Postgres URL to the async ``asyncpg`` driver."""
    parsed = make_url(url)
    if not parsed.get_backend_name().startswith("postgresql"):
        raise SystemExit(f"--target must be a PostgreSQL URL, got: {parsed.drivername}")
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.render_as_string(hide_password=False)


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )


async def _ensure_target_schema(engine: AsyncEngine) -> None:
    """Create the schema + stamp alembic head if the target is empty.

    Mirrors the in-memory bootstrap path in ``app.database`` so the app will
    not try to re-run migrations against an already-populated schema.
    """
    existing = await _table_names(engine)
    if existing - {"alembic_version"}:
        log.info("target already has schema (%d tables) — skipping create_all", len(existing))
        return

    log.info("creating schema on target from ORM models")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # NB: revision slugs in alembic/versions/ run up to 34 chars, longer than
        # Alembic's default VARCHAR(32) version_num. SQLite ignores VARCHAR length
        # so it never bit there, but Postgres rejects the overflow — widen it here.
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(255) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
            {"rev": ALEMBIC_HEAD_REVISION},
        )
    log.info("schema created, stamped alembic_version=%s", ALEMBIC_HEAD_REVISION)


async def _target_row_counts(engine: AsyncEngine) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with engine.connect() as conn:
        for table in TABLES:
            total = await conn.scalar(select(func.count()).select_from(table))
            if total:
                counts[table.name] = total
    return counts


async def _truncate_target(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table in reversed(TABLES):
            await conn.execute(table.delete())
    log.info("truncated %d target tables", len(TABLES))


async def _copy_table(
    src: AsyncEngine, tgt: AsyncEngine, table, batch_size: int, dry_run: bool
) -> int:
    copied = 0
    insert_stmt = table.insert()
    async with src.connect() as src_conn:
        result = await src_conn.stream(table.select())
        async with tgt.begin() as tgt_conn:
            async for chunk in result.partitions(batch_size):
                rows = [dict(row._mapping) for row in chunk]
                if not rows:
                    continue
                copied += len(rows)
                if not dry_run:
                    await tgt_conn.execute(insert_stmt, rows)
    return copied


async def _reset_sequences(engine: AsyncEngine) -> None:
    """Advance each serial/identity sequence past the largest copied PK."""
    async with engine.begin() as conn:
        for table in TABLES:
            pk_cols = [c for c in table.primary_key.columns if c.autoincrement is not False]
            for col in pk_cols:
                seq = await conn.scalar(
                    text("SELECT pg_get_serial_sequence(:t, :c)"),
                    {"t": table.name, "c": col.name},
                )
                if not seq:
                    continue
                max_id = await conn.scalar(select(func.max(col)))
                if max_id is None:
                    continue
                await conn.execute(
                    text("SELECT setval(:seq, :val, true)"),
                    {"seq": seq, "val": int(max_id)},
                )
                log.info("  sequence %s -> %s", seq, max_id)


async def migrate(source: str, target: str, batch_size: int, truncate: bool, dry_run: bool) -> None:
    src_engine = create_async_engine(source)
    tgt_engine = create_async_engine(target)
    try:
        src_tables = await _table_names(src_engine)
        if not src_tables - {"alembic_version"}:
            raise SystemExit(f"source has no data tables: {source}")

        await _ensure_target_schema(tgt_engine)

        existing = await _target_row_counts(tgt_engine)
        if existing and not truncate:
            summary = ", ".join(f"{t}={n}" for t, n in existing.items())
            raise SystemExit(
                f"target is not empty ({summary}). Re-run with --truncate to overwrite."
            )
        if existing and truncate and not dry_run:
            await _truncate_target(tgt_engine)

        log.info("%scopying %d tables (batch=%d)", "[dry-run] " if dry_run else "", len(TABLES), batch_size)
        grand_total = 0
        for table in TABLES:
            if table.name not in src_tables:
                log.info("  %-22s skipped (absent in source)", table.name)
                continue
            n = await _copy_table(src_engine, tgt_engine, table, batch_size, dry_run)
            grand_total += n
            log.info("  %-22s %d rows", table.name, n)

        if not dry_run:
            log.info("resetting Postgres sequences")
            await _reset_sequences(tgt_engine)

        log.info("%sdone — %d rows total", "[dry-run] " if dry_run else "", grand_total)
    finally:
        await src_engine.dispose()
        await tgt_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy the meta store from SQLite to PostgreSQL.")
    parser.add_argument(
        "--source",
        default=settings.META_DB_URL,
        help="SQLite source URL (default: settings.META_DB_URL)",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("TARGET_META_DB_URL"),
        help="PostgreSQL target URL (default: $TARGET_META_DB_URL)",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--truncate", action="store_true", help="wipe target tables before copying")
    parser.add_argument("--dry-run", action="store_true", help="report row counts without writing")
    args = parser.parse_args()

    if not args.target:
        parser.error("a Postgres target is required (--target or $TARGET_META_DB_URL)")

    source = _normalize_source(args.source)
    target = _normalize_target(args.target)
    log.info("source: %s", make_url(source).render_as_string(hide_password=True))
    log.info("target: %s", make_url(target).render_as_string(hide_password=True))

    asyncio.run(migrate(source, target, args.batch_size, args.truncate, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
