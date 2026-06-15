from __future__ import annotations

import asyncio
from logging.config import fileConfig
import os
from pathlib import Path
import sys

from alembic import context
from alembic.ddl.impl import DefaultImpl
from sqlalchemy import String, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Base  # noqa: E402


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Our revision slugs (e.g. "0013_resource_owner_alerts_enabled") run to 34 chars,
# longer than Alembic's default VARCHAR(32) version_num column. SQLite ignores
# the length so it never bit, but Postgres/MSSQL reject the overflow at boot.
# Alembic 1.17 hard-codes String(32) in DefaultImpl.version_table_impl and the
# `version_table_column_type` configure option does not exist, so widen the
# column by overriding the hook. Idempotent: existing version tables are reused
# as-is (Alembic only consults this when creating the table).
_VERSION_NUM_LEN = 255
_orig_version_table_impl = DefaultImpl.version_table_impl


def _widened_version_table_impl(self, **kw):
    table = _orig_version_table_impl(self, **kw)
    table.c.version_num.type = String(_VERSION_NUM_LEN)
    return table


DefaultImpl.version_table_impl = _widened_version_table_impl

if (
    os.environ.get("META_DB_URL")
    and config.get_main_option("sqlalchemy.url") == "sqlite+aiosqlite:///data/meta.db"
):
    config.set_main_option("sqlalchemy.url", os.environ["META_DB_URL"])


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
