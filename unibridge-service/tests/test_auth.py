"""Tests for app.auth and app.database modules."""
from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import patch

import pytest
import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import (
    ALL_PERMISSIONS,
    CurrentUser,
    _CACHE_TTL,
    create_token,
    get_current_user,
    get_role_permissions,
    invalidate_permission_cache,
    require_permission,
)
from app.config import settings
from app.models import Role, RolePermission

from tests.conftest import auth_header


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — ALL_PERMISSIONS
# ═══════════════════════════════════════════════════════════════════════════════


class TestAllPermissions:
    def test_all_permissions_has_expected_entries(self):
        assert len(ALL_PERMISSIONS) == 22

    def test_all_permissions_are_unique(self):
        assert len(ALL_PERMISSIONS) == len(set(ALL_PERMISSIONS))

    def test_all_permissions_are_strings(self):
        assert all(isinstance(p, str) for p in ALL_PERMISSIONS)

    def test_all_permissions_use_dot_notation(self):
        """Every permission should contain at least one dot separator."""
        for perm in ALL_PERMISSIONS:
            assert "." in perm, f"Permission '{perm}' missing dot separator"

    def test_expected_permission_categories(self):
        prefixes = {p.rsplit(".", 1)[0] for p in ALL_PERMISSIONS}
        assert "query.databases" in prefixes
        assert "admin.roles" in prefixes
        assert "gateway.routes" in prefixes


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — create_token
# ═══════════════════════════════════════════════════════════════════════════════


class TestCreateToken:
    def test_returns_string(self):
        token = create_token("alice", "admin")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_token_contains_username_and_role(self):
        token = create_token("alice", "admin")
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        assert payload["sub"] == "alice"
        assert payload["role"] == "admin"

    def test_token_has_exp_claim(self):
        token = create_token("alice", "admin")
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        assert "exp" in payload

    def test_default_expiry_is_8_hours(self):
        before = int(time.time())
        token = create_token("alice", "admin")
        after = int(time.time()) + 1
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        # Default expires_delta is 8 hours = 28800 seconds
        # JWT exp is an integer timestamp, so we compare with integer bounds
        assert before + 28800 <= payload["exp"] <= after + 28800

    def test_custom_expires_delta(self):
        before = int(time.time())
        token = create_token("alice", "admin", expires_delta=timedelta(minutes=5))
        after = int(time.time()) + 1
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        assert before + 300 <= payload["exp"] <= after + 300

    def test_custom_expires_delta_zero(self):
        """A zero timedelta should produce a token that expires immediately."""
        token = create_token("alice", "admin", expires_delta=timedelta(seconds=0))
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        assert payload["sub"] == "alice"

    def test_different_users_get_different_tokens(self):
        t1 = create_token("alice", "admin")
        t2 = create_token("bob", "admin")
        assert t1 != t2

    def test_different_roles_get_different_tokens(self):
        t1 = create_token("alice", "admin")
        t2 = create_token("alice", "viewer")
        assert t1 != t2

    def test_token_uses_hs256_algorithm(self):
        token = create_token("alice", "admin")
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "HS256"


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — get_current_user
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetCurrentUser:
    async def test_decodes_valid_token(self):
        token = create_token("alice", "admin")
        creds = _make_credentials(token)
        user = await get_current_user(credentials=creds)
        assert isinstance(user, CurrentUser)
        assert user.username == "alice"
        assert user.role == "admin"

    async def test_returns_correct_role(self):
        token = create_token("bob", "developer")
        creds = _make_credentials(token)
        user = await get_current_user(credentials=creds)
        assert user.role == "developer"

    async def test_raises_401_on_invalid_token(self):
        from fastapi import HTTPException

        creds = _make_credentials("not-a-valid-jwt")
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=creds)
        assert exc_info.value.status_code == 401

    async def test_raises_401_on_expired_token(self):
        from fastapi import HTTPException

        token = create_token("alice", "admin", expires_delta=timedelta(seconds=-10))
        creds = _make_credentials(token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=creds)
        assert exc_info.value.status_code == 401

    async def test_raises_401_on_wrong_secret(self):
        from fastapi import HTTPException

        payload = {"sub": "alice", "role": "admin", "exp": time.time() + 3600}
        token = jwt.encode(payload, "wrong-secret-for-testing-32bytes", algorithm="HS256")
        creds = _make_credentials(token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=creds)
        assert exc_info.value.status_code == 401

    async def test_raises_401_when_sub_missing(self):
        from fastapi import HTTPException

        payload = {"role": "admin", "exp": time.time() + 3600}
        token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
        creds = _make_credentials(token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=creds)
        assert exc_info.value.status_code == 401
        assert "missing subject or role" in exc_info.value.detail

    async def test_raises_401_when_role_missing(self):
        from fastapi import HTTPException

        payload = {"sub": "alice", "exp": time.time() + 3600}
        token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
        creds = _make_credentials(token)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=creds)
        assert exc_info.value.status_code == 401
        assert "missing subject or role" in exc_info.value.detail

    async def test_raises_401_on_empty_token(self):
        from fastapi import HTTPException

        creds = _make_credentials("")
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=creds)
        assert exc_info.value.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — get_role_permissions
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetRolePermissions:
    async def test_returns_admin_permissions(self, seeded_db):
        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            perms = await get_role_permissions(db, "admin")
            assert isinstance(perms, set)
            assert perms == set(ALL_PERMISSIONS)

    async def test_returns_developer_permissions(self, seeded_db):
        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            perms = await get_role_permissions(db, "developer")
            assert "query.execute" in perms
            assert "query.databases.read" in perms
            assert "query.databases.write" not in perms
            assert "admin.roles.write" not in perms

    async def test_returns_viewer_permissions(self, seeded_db):
        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            perms = await get_role_permissions(db, "viewer")
            assert perms == {"gateway.monitoring.read", "query.audit.read", "alerts.read"}

    async def test_returns_empty_set_for_unknown_role(self, seeded_db):
        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            perms = await get_role_permissions(db, "nonexistent_role")
            assert perms == set()

    async def test_caches_results(self, seeded_db):
        """After first call, cache should be populated and second call uses cache."""
        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            perms1 = await get_role_permissions(db, "admin")
            # Cache is now warm; verify _perm_cache is populated
            import app.auth as auth_mod
            assert len(auth_mod._perm_cache) > 0
            cached_ts = auth_mod._perm_cache_ts
            assert cached_ts > 0

            perms2 = await get_role_permissions(db, "admin")
            # Timestamp should not have changed (cache hit, not refreshed)
            assert auth_mod._perm_cache_ts == cached_ts
            assert perms1 == perms2


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — invalidate_permission_cache
# ═══════════════════════════════════════════════════════════════════════════════


class TestInvalidatePermissionCache:
    async def test_invalidate_clears_cache(self, seeded_db):
        import app.auth as auth_mod

        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            # Warm the cache
            await get_role_permissions(db, "admin")
            assert len(auth_mod._perm_cache) > 0

            # Invalidate
            await invalidate_permission_cache()
            assert auth_mod._perm_cache == {}
            assert auth_mod._perm_cache_ts == 0.0

    async def test_invalidate_forces_refresh_on_next_call(self, seeded_db):
        import app.auth as auth_mod

        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            await get_role_permissions(db, "admin")
            first_ts = auth_mod._perm_cache_ts

            await invalidate_permission_cache()
            assert auth_mod._perm_cache_ts == 0.0

            await get_role_permissions(db, "admin")
            # A fresh timestamp should be set
            assert auth_mod._perm_cache_ts > 0.0
            assert auth_mod._perm_cache_ts >= first_ts


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — Cache TTL / double-check locking
# ═══════════════════════════════════════════════════════════════════════════════


class TestCacheTTLBehavior:
    async def test_cache_refreshes_after_ttl_expires(self, seeded_db):
        """When the TTL has elapsed, a new DB fetch should occur."""
        import app.auth as auth_mod

        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            await get_role_permissions(db, "admin")
            first_ts = auth_mod._perm_cache_ts

            # Simulate TTL expiry by backdating the timestamp
            auth_mod._perm_cache_ts = time.time() - _CACHE_TTL - 1

            await get_role_permissions(db, "admin")
            # Cache should have been refreshed with a newer timestamp
            assert auth_mod._perm_cache_ts > first_ts - 1  # new ts from time.time()

    async def test_cache_does_not_refresh_within_ttl(self, seeded_db):
        """Within the TTL window, the cache should not be refreshed."""
        import app.auth as auth_mod

        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            await get_role_permissions(db, "admin")
            ts_after_first = auth_mod._perm_cache_ts

            # Call again immediately (well within TTL)
            await get_role_permissions(db, "admin")
            assert auth_mod._perm_cache_ts == ts_after_first

    async def test_double_check_locking_prevents_redundant_refresh(self, seeded_db):
        """
        After the lock is acquired, the inner time check should prevent a second
        refresh when another coroutine already refreshed.
        """
        import app.auth as auth_mod

        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            # Warm cache
            await get_role_permissions(db, "admin")

            # Simulate expired outer check but fresh inner check:
            # set _perm_cache_ts to just before TTL boundary, then patch time.time
            # so the outer check sees expiry but the inner check sees fresh.
            # Force outer check to fail (stale)
            auth_mod._perm_cache_ts = time.time() - _CACHE_TTL - 1

            call_count = 0
            original_refresh = auth_mod._refresh_cache

            async def counting_refresh(db_session):
                nonlocal call_count
                call_count += 1
                await original_refresh(db_session)

            with patch.object(auth_mod, "_refresh_cache", counting_refresh):
                await get_role_permissions(db, "admin")

            # Should have been called exactly once
            assert call_count == 1

    async def test_refresh_failure_sets_short_backoff_and_uses_stale_cache(self, seeded_db):
        """A failed refresh should not make every concurrent request retry DB I/O."""
        import app.auth as auth_mod

        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            await get_role_permissions(db, "admin")
            auth_mod._perm_cache_ts = time.time() - _CACHE_TTL - 1

            call_count = 0

            async def failing_refresh(_db_session):
                nonlocal call_count
                call_count += 1
                raise RuntimeError("database unavailable")

            with patch.object(auth_mod, "_refresh_cache", failing_refresh):
                with pytest.raises(RuntimeError, match="database unavailable"):
                    await get_role_permissions(db, "admin")

                perms = await get_role_permissions(db, "admin")

            assert perms == set(ALL_PERMISSIONS)
            assert call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — require_permission
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequirePermission:
    async def test_returns_user_when_permission_exists(self, seeded_db):
        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            token = create_token("testadmin", "admin")
            creds = _make_credentials(token)
            user = await get_current_user(credentials=creds)

            checker = require_permission("admin.roles.read")
            result = await checker(user=user, db=db)
            assert isinstance(result, CurrentUser)
            assert result.username == "testadmin"
            assert result.role == "admin"

    async def test_raises_403_when_permission_missing(self, seeded_db):
        from fastapi import HTTPException

        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            token = create_token("testviewer", "viewer")
            creds = _make_credentials(token)
            user = await get_current_user(credentials=creds)

            checker = require_permission("admin.roles.write")
            with pytest.raises(HTTPException) as exc_info:
                await checker(user=user, db=db)
            assert exc_info.value.status_code == 403

    async def test_passes_if_any_permission_matches(self, seeded_db):
        """When multiple permissions are required, passing ANY one suffices."""
        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            token = create_token("testviewer", "viewer")
            creds = _make_credentials(token)
            user = await get_current_user(credentials=creds)

            # viewer has "query.audit.read" but not "admin.roles.write"
            checker = require_permission("admin.roles.write", "query.audit.read")
            result = await checker(user=user, db=db)
            assert result.username == "testviewer"

    async def test_raises_403_when_none_of_multiple_perms_match(self, seeded_db):
        from fastapi import HTTPException

        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            token = create_token("testviewer", "viewer")
            creds = _make_credentials(token)
            user = await get_current_user(credentials=creds)

            # viewer has neither of these
            checker = require_permission("admin.roles.write", "query.databases.write")
            with pytest.raises(HTTPException) as exc_info:
                await checker(user=user, db=db)
            assert exc_info.value.status_code == 403
            assert "Required permission" in exc_info.value.detail

    async def test_developer_has_execute_but_not_write(self, seeded_db):
        await invalidate_permission_cache()
        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            token = create_token("testdev", "developer")
            creds = _make_credentials(token)
            user = await get_current_user(credentials=creds)

            # developer has query.execute
            checker_exec = require_permission("query.execute")
            result = await checker_exec(user=user, db=db)
            assert result.role == "developer"

            # developer does NOT have query.databases.write
            from fastapi import HTTPException
            checker_write = require_permission("query.databases.write")
            with pytest.raises(HTTPException) as exc_info:
                await checker_write(user=user, db=db)
            assert exc_info.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════════
# database.py — _seed_roles
# ═══════════════════════════════════════════════════════════════════════════════


class TestSeedRoles:
    async def test_seed_creates_three_roles(self, engine):
        """_seed_roles should create admin, developer, viewer roles."""
        # We need to point the database module's session factory at our test engine
        from app.database import _seed_roles

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        async with session_factory() as db:
            result = await db.execute(select(Role).order_by(Role.name))
            roles = result.scalars().all()
            role_names = [r.name for r in roles]
            assert "admin" in role_names
            assert "developer" in role_names
            assert "viewer" in role_names
            assert len(role_names) == 3

    async def test_seed_admin_has_all_permissions(self, engine):
        from app.database import _seed_roles

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        async with session_factory() as db:
            result = await db.execute(select(Role).where(Role.name == "admin"))
            admin_role = result.scalar_one()
            perm_result = await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == admin_role.id)
            )
            perms = {row[0] for row in perm_result.all()}
            assert perms == set(ALL_PERMISSIONS)

    async def test_seed_developer_permissions(self, engine):
        from app.database import _seed_roles

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        async with session_factory() as db:
            result = await db.execute(select(Role).where(Role.name == "developer"))
            dev_role = result.scalar_one()
            perm_result = await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == dev_role.id)
            )
            perms = {row[0] for row in perm_result.all()}
            expected = {
                "query.databases.read", "query.permissions.read", "query.audit.read",
                "query.execute",
                "gateway.routes.read", "gateway.upstreams.read",
                "gateway.monitoring.read",
                "apikeys.read",
                "alerts.read",
                "s3.connections.read", "s3.browse",
            }
            assert perms == expected

    async def test_seed_viewer_permissions(self, engine):
        from app.database import _seed_roles

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        async with session_factory() as db:
            result = await db.execute(select(Role).where(Role.name == "viewer"))
            viewer_role = result.scalar_one()
            perm_result = await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == viewer_role.id)
            )
            perms = {row[0] for row in perm_result.all()}
            assert perms == {"gateway.monitoring.read", "query.audit.read", "alerts.read"}

    async def test_seed_roles_are_marked_as_system(self, engine):
        from app.database import _seed_roles

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        async with session_factory() as db:
            result = await db.execute(select(Role))
            roles = result.scalars().all()
            assert all(r.is_system for r in roles)

    async def test_seed_is_idempotent(self, engine):
        """Running _seed_roles twice should not create duplicate roles."""
        from app.database import _seed_roles

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        with patch("app.database.async_session", session_factory):
            await _seed_roles()
            await _seed_roles()

        async with session_factory() as db:
            result = await db.execute(select(Role))
            roles = result.scalars().all()
            assert len(roles) == 3

            # Verify no duplicate permissions
            for role in roles:
                perm_result = await db.execute(
                    select(RolePermission.permission).where(RolePermission.role_id == role.id)
                )
                perms = [row[0] for row in perm_result.all()]
                assert len(perms) == len(set(perms)), (
                    f"Duplicate permissions found for role '{role.name}'"
                )

    async def test_seed_upsert_restores_permissions(self, engine):
        """
        If permissions are manually modified, re-seeding should restore
        the original set (because _seed_roles deletes and re-adds for existing roles).
        """
        from app.database import _seed_roles

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        # Manually remove some permissions from the viewer role
        async with session_factory() as db:
            result = await db.execute(select(Role).where(Role.name == "viewer"))
            viewer = result.scalar_one()
            await db.execute(
                delete(RolePermission).where(RolePermission.role_id == viewer.id)
            )
            await db.commit()

        # Verify permissions are gone
        async with session_factory() as db:
            result = await db.execute(select(Role).where(Role.name == "viewer"))
            viewer = result.scalar_one()
            perm_result = await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == viewer.id)
            )
            assert len(perm_result.all()) == 0

        # Re-seed
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        # Verify permissions are restored
        async with session_factory() as db:
            result = await db.execute(select(Role).where(Role.name == "viewer"))
            viewer = result.scalar_one()
            perm_result = await db.execute(
                select(RolePermission.permission).where(RolePermission.role_id == viewer.id)
            )
            perms = {row[0] for row in perm_result.all()}
            assert perms == {"gateway.monitoring.read", "query.audit.read", "alerts.read"}

    async def test_seed_upsert_restores_description(self, engine):
        """Re-seeding should restore the description to the seed value."""
        from app.database import _seed_roles

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        # Modify description
        async with session_factory() as db:
            result = await db.execute(select(Role).where(Role.name == "admin"))
            admin_role = result.scalar_one()
            admin_role.description = "Tampered description"
            await db.commit()

        # Re-seed
        with patch("app.database.async_session", session_factory):
            await _seed_roles()

        async with session_factory() as db:
            result = await db.execute(select(Role).where(Role.name == "admin"))
            admin_role = result.scalar_one()
            assert admin_role.description == "Full access to all features"


# ═══════════════════════════════════════════════════════════════════════════════
# database.py — init_db
# ═══════════════════════════════════════════════════════════════════════════════


class TestInitDb:
    async def test_init_db_creates_tables(self):
        """init_db should create all tables defined in Base.metadata."""
        from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

        from app.database import init_db

        test_engine = _create_engine("sqlite+aiosqlite:///:memory:", echo=False)
        test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

        with patch("app.database.engine", test_engine), \
             patch("app.database.async_session", test_session_factory):
            await init_db()

        # Verify tables exist by querying them
        async with test_session_factory() as db:
            # These should not raise
            await db.execute(select(Role))
            await db.execute(select(RolePermission))

            from app.models import DBConnection, Permission, AuditLog
            await db.execute(select(DBConnection))
            await db.execute(select(Permission))
            await db.execute(select(AuditLog))

        await test_engine.dispose()

    async def test_init_db_seeds_roles(self):
        """init_db should also seed default roles."""
        from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

        from app.database import init_db

        test_engine = _create_engine("sqlite+aiosqlite:///:memory:", echo=False)
        test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

        with patch("app.database.engine", test_engine), \
             patch("app.database.async_session", test_session_factory):
            await init_db()

        async with test_session_factory() as db:
            result = await db.execute(select(Role))
            roles = result.scalars().all()
            role_names = sorted([r.name for r in roles])
            assert role_names == ["admin", "developer", "viewer"]

        await test_engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# Token endpoint: POST /auth/token
# ═══════════════════════════════════════════════════════════════════════════════


class TestTokenEndpoint:
    async def test_issue_token_with_valid_role(self, client):
        resp = await client.post("/auth/token", json={"username": "testuser", "role": "admin"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        # Verify the token is decodable
        payload = jwt.decode(
            data["access_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        assert payload["sub"] == "testuser"
        assert payload["role"] == "admin"

    async def test_issue_token_with_developer_role(self, client):
        resp = await client.post("/auth/token", json={"username": "dev", "role": "developer"})
        assert resp.status_code == 200
        payload = jwt.decode(
            resp.json()["access_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        assert payload["role"] == "developer"

    async def test_issue_token_with_viewer_role(self, client):
        resp = await client.post("/auth/token", json={"username": "viewer", "role": "viewer"})
        assert resp.status_code == 200

    async def test_issue_token_default_role_is_viewer(self, client):
        """The schema default for role is 'viewer'."""
        resp = await client.post("/auth/token", json={"username": "someone"})
        assert resp.status_code == 200
        payload = jwt.decode(
            resp.json()["access_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        assert payload["role"] == "viewer"

    async def test_issue_token_nonexistent_role_returns_400(self, client):
        resp = await client.post("/auth/token", json={"username": "test", "role": "superadmin"})
        assert resp.status_code == 400
        assert "does not exist" in resp.json()["detail"]

    async def test_issue_token_missing_username_returns_422(self, client):
        resp = await client.post("/auth/token", json={"role": "admin"})
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# GET /auth/me
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuthMeEndpoint:
    async def test_get_me_with_admin_token(self, client, admin_token):
        resp = await client.get("/auth/me", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testadmin"
        assert data["role"] == "admin"
        assert isinstance(data["permissions"], list)
        # admin should have all permissions
        assert set(data["permissions"]) == set(ALL_PERMISSIONS)

    async def test_get_me_with_developer_token(self, client, developer_token):
        resp = await client.get("/auth/me", headers=auth_header(developer_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testdev"
        assert data["role"] == "developer"
        assert "query.execute" in data["permissions"]
        assert "admin.roles.write" not in data["permissions"]

    async def test_get_me_with_viewer_token(self, client, viewer_token):
        resp = await client.get("/auth/me", headers=auth_header(viewer_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testviewer"
        assert data["role"] == "viewer"
        assert set(data["permissions"]) == {"gateway.monitoring.read", "query.audit.read", "alerts.read"}

    async def test_get_me_without_token_returns_401(self, client):
        resp = await client.get("/auth/me")
        assert resp.status_code in (401, 403)

    async def test_get_me_with_invalid_token_returns_401(self, client):
        resp = await client.get("/auth/me", headers=auth_header("bogus-token"))
        assert resp.status_code == 401

    async def test_get_me_permissions_are_sorted(self, client, admin_token):
        resp = await client.get("/auth/me", headers=auth_header(admin_token))
        assert resp.status_code == 200
        perms = resp.json()["permissions"]
        assert perms == sorted(perms)


# ═══════════════════════════════════════════════════════════════════════════════
# GET /auth/roles
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuthRolesEndpoint:
    async def test_list_roles_requires_auth(self, client):
        """GET /auth/roles requires authentication."""
        resp = await client.get("/auth/roles")
        assert resp.status_code == 403

    async def test_list_roles_returns_role_names(self, client, admin_token):
        resp = await client.get("/auth/roles", headers=auth_header(admin_token))
        data = resp.json()
        assert isinstance(data, list)
        assert "admin" in data
        assert "developer" in data
        assert "viewer" in data

    async def test_list_roles_returns_sorted(self, client, admin_token):
        resp = await client.get("/auth/roles", headers=auth_header(admin_token))
        data = resp.json()
        assert data == sorted(data)

    async def test_list_roles_returns_strings(self, client, viewer_token):
        """Any authenticated user can list roles."""
        resp = await client.get("/auth/roles", headers=auth_header(viewer_token))
        assert resp.status_code == 200
        data = resp.json()
        assert all(isinstance(name, str) for name in data)


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/permissions
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdminPermissionsEndpoint:
    async def test_list_permissions_as_admin(self, client, admin_token):
        resp = await client.get("/admin/permissions", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert set(data) == set(ALL_PERMISSIONS)
        assert len(data) == len(ALL_PERMISSIONS)

    async def test_list_permissions_as_developer_forbidden(self, client, developer_token):
        """developer role does not have admin.roles.read."""
        resp = await client.get("/admin/permissions", headers=auth_header(developer_token))
        assert resp.status_code == 403

    async def test_list_permissions_as_viewer_forbidden(self, client, viewer_token):
        resp = await client.get("/admin/permissions", headers=auth_header(viewer_token))
        assert resp.status_code == 403

    async def test_list_permissions_without_auth_returns_401(self, client):
        resp = await client.get("/admin/permissions")
        assert resp.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


class _FakeCredentials:
    """Mimics HTTPAuthorizationCredentials for unit-testing get_current_user."""

    def __init__(self, token: str):
        self.credentials = token
        self.scheme = "Bearer"


def _make_credentials(token: str) -> _FakeCredentials:
    return _FakeCredentials(token)


# ═══════════════════════════════════════════════════════════════════════════════
# auth.py — get_current_user_or_apikey (APISIX header-based auth)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_apikey_user_from_apisix_header():
    """When X-Consumer-Username header is present, return ApiKeyUser."""
    from unittest.mock import AsyncMock, MagicMock
    from app.auth import get_current_user_or_apikey, ApiKeyUser

    mock_request = MagicMock()
    mock_request.headers = {"x-consumer-username": "my-app-key"}

    mock_access = MagicMock()
    mock_access.consumer_name = "my-app-key"
    mock_access.allowed_databases = '["mydb"]'
    mock_access.allowed_routes = '["route-1"]'

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_access
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    user = await get_current_user_or_apikey(
        request=mock_request, credentials=None, db=mock_db
    )
    assert isinstance(user, ApiKeyUser)
    assert user.consumer_name == "my-app-key"
    assert user.allowed_databases == ["mydb"]


@pytest.mark.asyncio
async def test_apikey_user_unknown_consumer_returns_401():
    """When X-Consumer-Username header has unknown consumer, raise 401."""
    from unittest.mock import AsyncMock, MagicMock
    from fastapi import HTTPException
    from app.auth import get_current_user_or_apikey

    mock_request = MagicMock()
    mock_request.headers = {"x-consumer-username": "unknown-key"}

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_apikey(
            request=mock_request, credentials=None, db=mock_db
        )
    assert exc_info.value.status_code == 401
