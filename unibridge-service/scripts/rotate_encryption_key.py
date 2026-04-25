"""Rotate metadata secrets from one ENCRYPTION_KEY to another.

Usage:

    OLD_ENCRYPTION_KEY=old-value \
    NEW_ENCRYPTION_KEY=new-strong-value \
    python -m scripts.rotate_encryption_key

The old key is allowed to be weak so operators can migrate away from legacy
deployments. The new key must pass the same strength checks used at startup.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Sequence

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.models import DBConnection, S3Connection
from app.services.connection_manager import _validate_encryption_key_strength


@dataclass(frozen=True)
class RotationResult:
    db_connections: int = 0
    s3_connections: int = 0
    fields_rotated: int = 0


def _fernet_for_key(key: str) -> Fernet:
    if len(key) != 44:
        digest = hashlib.sha256(key.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def _validate_new_key(new_key: str) -> None:
    try:
        _validate_encryption_key_strength(new_key)
        _fernet_for_key(new_key)
    except ValueError as exc:
        message = str(exc).replace("ENCRYPTION_KEY", "NEW_ENCRYPTION_KEY")
        raise ValueError(message) from exc


def _encrypt_value(plain: str, key: str) -> str:
    return _fernet_for_key(key).encrypt(plain.encode()).decode()


def _decrypt_value(encrypted: str, key: str) -> str:
    try:
        return _fernet_for_key(key).decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt secret with OLD_ENCRYPTION_KEY") from exc


def _rotate_value(encrypted: str, old_key: str, new_key: str) -> str:
    return _encrypt_value(_decrypt_value(encrypted, old_key), new_key)


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )


async def rotate_database(
    engine: AsyncEngine,
    *,
    old_key: str,
    new_key: str,
    dry_run: bool = False,
) -> RotationResult:
    """Re-encrypt DB and S3 metadata secrets with ``new_key``.

    Returns counts for affected rows and encrypted fields. The operation is
    atomic for the supplied database engine: any decrypt/encrypt failure rolls
    back all updates.
    """
    if not old_key:
        raise ValueError("OLD_ENCRYPTION_KEY is required")
    if not new_key:
        raise ValueError("NEW_ENCRYPTION_KEY is required")
    if old_key == new_key:
        raise ValueError("OLD_ENCRYPTION_KEY and NEW_ENCRYPTION_KEY must differ")
    _validate_new_key(new_key)

    existing_tables = await _table_names(engine)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    db_rows = 0
    s3_rows = 0
    fields = 0

    async with session_factory() as session:
        try:
            if "db_connections" in existing_tables:
                result = await session.execute(select(DBConnection))
                for conn in result.scalars():
                    rotated = _rotate_value(
                        conn.password_encrypted,
                        old_key,
                        new_key,
                    )
                    db_rows += 1
                    fields += 1
                    if not dry_run:
                        conn.password_encrypted = rotated

            if "s3_connections" in existing_tables:
                result = await session.execute(select(S3Connection))
                for conn in result.scalars():
                    access_key = _rotate_value(
                        conn.access_key_id_encrypted,
                        old_key,
                        new_key,
                    )
                    secret_key = _rotate_value(
                        conn.secret_access_key_encrypted,
                        old_key,
                        new_key,
                    )
                    s3_rows += 1
                    fields += 2
                    if not dry_run:
                        conn.access_key_id_encrypted = access_key
                        conn.secret_access_key_encrypted = secret_key

            if dry_run:
                await session.rollback()
            else:
                await session.commit()
        except Exception:
            await session.rollback()
            raise

    return RotationResult(
        db_connections=db_rows,
        s3_connections=s3_rows,
        fields_rotated=fields,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rotate encrypted metadata secrets to a new ENCRYPTION_KEY."
    )
    parser.add_argument(
        "--old-key",
        default=os.getenv("OLD_ENCRYPTION_KEY"),
        help="Previous key, or OLD_ENCRYPTION_KEY from the environment.",
    )
    parser.add_argument(
        "--new-key",
        default=os.getenv("NEW_ENCRYPTION_KEY"),
        help="Replacement key, or NEW_ENCRYPTION_KEY from the environment.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("META_DB_URL", settings.META_DB_URL),
        help="Metadata database URL. Defaults to META_DB_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Decrypt and count affected fields without writing updates.",
    )
    return parser.parse_args(argv)


async def _run(argv: Sequence[str] | None = None) -> RotationResult:
    args = _parse_args(argv)
    engine = create_async_engine(args.database_url)
    try:
        result = await rotate_database(
            engine,
            old_key=args.old_key or "",
            new_key=args.new_key or "",
            dry_run=args.dry_run,
        )
    finally:
        await engine.dispose()

    action = "Would rotate" if args.dry_run else "Rotated"
    print(
        f"{action} {result.fields_rotated} encrypted field(s) "
        f"across {result.db_connections} DB connection(s) "
        f"and {result.s3_connections} S3 connection(s)."
    )
    return result


if __name__ == "__main__":
    asyncio.run(_run())
