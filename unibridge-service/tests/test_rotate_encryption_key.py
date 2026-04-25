"""Tests for the metadata encryption key rotation script."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, DBConnection, S3Connection
from scripts.rotate_encryption_key import (
    _decrypt_value,
    _encrypt_value,
    rotate_database,
)


OLD_WEAK_KEY = "changeme"
NEW_STRONG_KEY = "new-super-strong-rotation-key-1234567890!"


@pytest.fixture
async def rotation_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


async def _seed_encrypted_rows(engine):
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        session.add(
            DBConnection(
                alias="main",
                db_type="postgres",
                host="db",
                port=5432,
                database="app",
                username="app",
                password_encrypted=_encrypt_value("db-secret", OLD_WEAK_KEY),
            )
        )
        session.add(
            S3Connection(
                alias="objects",
                endpoint_url="https://s3.example.com",
                region="us-east-1",
                access_key_id_encrypted=_encrypt_value("access-id", OLD_WEAK_KEY),
                secret_access_key_encrypted=_encrypt_value("secret-key", OLD_WEAK_KEY),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_rotate_database_reencrypts_db_and_s3_secrets(rotation_engine):
    await _seed_encrypted_rows(rotation_engine)

    result = await rotate_database(
        rotation_engine,
        old_key=OLD_WEAK_KEY,
        new_key=NEW_STRONG_KEY,
    )

    assert result.db_connections == 1
    assert result.s3_connections == 1
    assert result.fields_rotated == 3

    session_factory = async_sessionmaker(rotation_engine, class_=AsyncSession)
    async with session_factory() as session:
        db_conn = (await session.execute(select(DBConnection))).scalar_one()
        s3_conn = (await session.execute(select(S3Connection))).scalar_one()

    assert _decrypt_value(db_conn.password_encrypted, NEW_STRONG_KEY) == "db-secret"
    assert _decrypt_value(
        s3_conn.access_key_id_encrypted,
        NEW_STRONG_KEY,
    ) == "access-id"
    assert _decrypt_value(
        s3_conn.secret_access_key_encrypted,
        NEW_STRONG_KEY,
    ) == "secret-key"

    with pytest.raises(ValueError):
        _decrypt_value(db_conn.password_encrypted, OLD_WEAK_KEY)


@pytest.mark.asyncio
async def test_rotate_database_dry_run_does_not_rewrite(rotation_engine):
    await _seed_encrypted_rows(rotation_engine)

    result = await rotate_database(
        rotation_engine,
        old_key=OLD_WEAK_KEY,
        new_key=NEW_STRONG_KEY,
        dry_run=True,
    )

    assert result.fields_rotated == 3

    session_factory = async_sessionmaker(rotation_engine, class_=AsyncSession)
    async with session_factory() as session:
        db_conn = (await session.execute(select(DBConnection))).scalar_one()

    assert _decrypt_value(db_conn.password_encrypted, OLD_WEAK_KEY) == "db-secret"
    with pytest.raises(ValueError):
        _decrypt_value(db_conn.password_encrypted, NEW_STRONG_KEY)


@pytest.mark.asyncio
async def test_rotate_database_rejects_weak_new_key(rotation_engine):
    await _seed_encrypted_rows(rotation_engine)

    with pytest.raises(ValueError, match="NEW_ENCRYPTION_KEY"):
        await rotate_database(
            rotation_engine,
            old_key=OLD_WEAK_KEY,
            new_key="changeme",
        )
