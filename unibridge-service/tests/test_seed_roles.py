import hashlib
import json
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from app import database
from app.models import Role, RolePermission, SystemConfig
from app.auth import ALL_PERMISSIONS


@pytest.mark.asyncio
async def test_seed_creates_admin_and_user_only(seeded_db):
    Session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        names = (await s.execute(select(Role.name).order_by(Role.name))).scalars().all()
    assert list(names) == ["admin", "user"]


@pytest.mark.asyncio
async def test_user_role_has_exactly_two_permissions(seeded_db):
    Session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        role = (await s.execute(select(Role).where(Role.name == "user"))).scalar_one()
        perms = (await s.execute(
            select(RolePermission.permission).where(RolePermission.role_id == role.id)
        )).scalars().all()
    assert set(perms) == {"gateway.monitoring.self", "apikeys.self"}


@pytest.mark.asyncio
async def test_admin_role_has_all_permissions(seeded_db):
    Session = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        role = (await s.execute(select(Role).where(Role.name == "admin"))).scalar_one()
        perms = (await s.execute(
            select(RolePermission.permission).where(RolePermission.role_id == role.id)
        )).scalars().all()
    assert set(perms) == set(ALL_PERMISSIONS)


# ── Role seed version: an older image never rewrites a newer one's roles ─────

# Change _seed_role_definitions() → bump ROLE_SEED_VERSION, then update both pins.
PINNED_ROLE_SEED_VERSION = 1
PINNED_ROLE_DEFINITIONS_DIGEST = "027c4f8680bc25b9dbd90ac60ca3aadd249d751483d56a0f01ee154e10088568"


def _definitions_digest() -> str:
    canonical = {
        name: {"description": spec["description"], "permissions": sorted(spec["permissions"])}
        for name, spec in database._seed_role_definitions().items()
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def test_role_seed_version_is_bumped_with_the_role_definitions():
    digest = _definitions_digest()
    assert (database.ROLE_SEED_VERSION, digest) == (
        PINNED_ROLE_SEED_VERSION,
        PINNED_ROLE_DEFINITIONS_DIGEST,
    ), (
        "The system role definitions changed. Bump ROLE_SEED_VERSION in app/database.py "
        "(an older blue/green color restarting would otherwise rewrite the roles to its "
        f"own definitions), then set PINNED_ROLE_SEED_VERSION and "
        f"PINNED_ROLE_DEFINITIONS_DIGEST = {digest!r} here."
    )


@pytest.mark.asyncio
async def test_seed_records_its_version(engine):
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.database.async_session", Session):
        await database._seed_roles()

    async with Session() as s:
        row = await s.get(SystemConfig, database.ROLE_SEED_VERSION_KEY)
    assert row is not None and row.value == str(database.ROLE_SEED_VERSION)


@pytest.mark.asyncio
async def test_older_image_leaves_roles_written_by_a_newer_one(engine):
    """The old color restarting after a release that added a permission must not
    delete it from the shared database."""
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.database.async_session", Session):
        await database._seed_roles()

    async with Session() as s:
        admin = (await s.execute(select(Role).where(Role.name == "admin"))).scalar_one()
        s.add(RolePermission(role_id=admin.id, permission="feature.from.newer.release"))
        version = await s.get(SystemConfig, database.ROLE_SEED_VERSION_KEY)
        version.value = str(database.ROLE_SEED_VERSION + 1)
        await s.commit()

    with patch("app.database.async_session", Session):
        await database._seed_roles()

    async with Session() as s:
        admin = (await s.execute(select(Role).where(Role.name == "admin"))).scalar_one()
        perms = set((await s.execute(
            select(RolePermission.permission).where(RolePermission.role_id == admin.id)
        )).scalars().all())
        version = await s.get(SystemConfig, database.ROLE_SEED_VERSION_KEY)
    assert "feature.from.newer.release" in perms
    assert version.value == str(database.ROLE_SEED_VERSION + 1)


@pytest.mark.asyncio
async def test_same_version_still_resyncs_the_system_roles(engine):
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.database.async_session", Session):
        await database._seed_roles()
        async with Session() as s:
            user = (await s.execute(select(Role).where(Role.name == "user"))).scalar_one()
            s.add(RolePermission(role_id=user.id, permission="query.execute"))
            await s.commit()
        await database._seed_roles()

    async with Session() as s:
        user = (await s.execute(select(Role).where(Role.name == "user"))).scalar_one()
        perms = set((await s.execute(
            select(RolePermission.permission).where(RolePermission.role_id == user.id)
        )).scalars().all())
    assert perms == {"gateway.monitoring.self", "apikeys.self"}
