"""Tests for SQL keyword blacklist validator."""
from __future__ import annotations

import pytest

from app.services.sql_validator import validate_sql


class TestValidateSql:
    """Test blocked keyword detection."""

    def test_allows_normal_select(self):
        errors = validate_sql("SELECT * FROM users")
        assert errors is None

    def test_allows_normal_insert(self):
        errors = validate_sql("INSERT INTO users (name) VALUES ('test')")
        assert errors is None

    def test_blocks_grant(self):
        errors = validate_sql("GRANT SELECT ON users TO public")
        assert errors is not None
        assert "GRANT" in errors

    def test_blocks_revoke(self):
        errors = validate_sql("REVOKE ALL ON users FROM public")
        assert errors is not None
        assert "REVOKE" in errors

    def test_blocks_create_user(self):
        errors = validate_sql("CREATE USER hacker WITH PASSWORD 'test'")
        assert errors is not None
        assert "CREATE USER" in errors

    def test_blocks_drop_user(self):
        errors = validate_sql("DROP USER admin")
        assert errors is not None
        assert "DROP USER" in errors

    def test_blocks_alter_user(self):
        errors = validate_sql("ALTER USER admin WITH SUPERUSER")
        assert errors is not None
        assert "ALTER USER" in errors

    def test_blocks_shutdown(self):
        errors = validate_sql("SHUTDOWN")
        assert errors is not None
        assert "SHUTDOWN" in errors

    def test_blocks_create_login(self):
        errors = validate_sql("CREATE LOGIN hacker WITH PASSWORD = 'test'")
        assert errors is not None
        assert "CREATE LOGIN" in errors

    def test_blocks_drop_login(self):
        errors = validate_sql("DROP LOGIN hacker")
        assert errors is not None
        assert "DROP LOGIN" in errors

    def test_blocks_backup(self):
        errors = validate_sql("BACKUP DATABASE mydb TO DISK = '/tmp/backup'")
        assert errors is not None
        assert "BACKUP" in errors

    def test_blocks_restore(self):
        errors = validate_sql("RESTORE DATABASE mydb FROM DISK = '/tmp/backup'")
        assert errors is not None
        assert "RESTORE" in errors

    def test_ignores_keyword_in_string(self):
        errors = validate_sql("SELECT * FROM users WHERE name = 'GRANT me access'")
        assert errors is None

    def test_ignores_keyword_in_comment(self):
        errors = validate_sql("SELECT * FROM users -- GRANT stuff\nWHERE id = 1")
        assert errors is None

    def test_ignores_keyword_in_block_comment(self):
        errors = validate_sql("SELECT * /* SHUTDOWN */ FROM users")
        assert errors is None

    def test_blocks_kill(self):
        errors = validate_sql("KILL 1234")
        assert errors is not None
        assert "KILL" in errors

    def test_custom_blocked_keywords(self):
        errors = validate_sql("VACUUM users", extra_blocked=["VACUUM"])
        assert errors is not None
        assert "VACUUM" in errors

    def test_custom_keyword_allows_normal(self):
        errors = validate_sql("SELECT * FROM users", extra_blocked=["VACUUM"])
        assert errors is None
