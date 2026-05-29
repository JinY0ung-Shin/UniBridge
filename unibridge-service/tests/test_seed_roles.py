import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from app.models import Role, RolePermission
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
