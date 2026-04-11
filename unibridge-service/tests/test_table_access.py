"""Tests for table-level access control."""
from __future__ import annotations

import pytest

from app.services.table_access import extract_tables, check_table_access


class TestExtractTables:
    """Test table name extraction from SQL."""

    def test_simple_select(self):
        tables = extract_tables("SELECT * FROM users")
        assert tables == {"users"}

    def test_select_with_schema(self):
        tables = extract_tables("SELECT * FROM public.users")
        assert tables == {"users"}

    def test_select_with_mssql_brackets(self):
        tables = extract_tables("SELECT * FROM [dbo].[users]")
        assert tables == {"users"}

    def test_join(self):
        tables = extract_tables("SELECT u.name FROM users u JOIN orders o ON u.id = o.user_id")
        assert tables == {"users", "orders"}

    def test_left_join(self):
        tables = extract_tables("SELECT * FROM users LEFT JOIN orders ON users.id = orders.user_id")
        assert tables == {"users", "orders"}

    def test_multiple_joins(self):
        tables = extract_tables(
            "SELECT * FROM users u "
            "INNER JOIN orders o ON u.id = o.user_id "
            "LEFT JOIN products p ON o.product_id = p.id"
        )
        assert tables == {"users", "orders", "products"}

    def test_insert_into(self):
        tables = extract_tables("INSERT INTO users (name) VALUES ('test')")
        assert tables == {"users"}

    def test_update(self):
        tables = extract_tables("UPDATE users SET name = 'test' WHERE id = 1")
        assert tables == {"users"}

    def test_delete_from(self):
        tables = extract_tables("DELETE FROM users WHERE id = 1")
        assert tables == {"users"}

    def test_subquery(self):
        tables = extract_tables(
            "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)"
        )
        assert tables == {"users", "orders"}

    def test_cte(self):
        tables = extract_tables(
            "WITH active AS (SELECT * FROM users WHERE active = true) "
            "SELECT * FROM active JOIN orders ON active.id = orders.user_id"
        )
        assert "users" in tables
        assert "orders" in tables

    def test_ignores_table_in_string(self):
        tables = extract_tables("SELECT * FROM users WHERE name = 'FROM orders'")
        assert tables == {"users"}

    def test_ignores_table_in_comment(self):
        tables = extract_tables("SELECT * FROM users -- FROM orders\nWHERE id = 1")
        assert tables == {"users"}

    def test_case_insensitive(self):
        tables = extract_tables("select * from Users")
        assert tables == {"users"}

    def test_cross_join(self):
        tables = extract_tables("SELECT * FROM users CROSS JOIN orders")
        assert tables == {"users", "orders"}

    def test_comma_separated_from(self):
        tables = extract_tables("SELECT * FROM users, orders WHERE users.id = orders.user_id")
        assert tables == {"users", "orders"}


class TestCheckTableAccess:
    """Test whitelist checking."""

    def test_all_allowed_returns_none(self):
        result = check_table_access({"users", "orders"}, ["users", "orders", "products"])
        assert result is None

    def test_denied_returns_error(self):
        result = check_table_access({"users", "secrets"}, ["users", "orders"])
        assert result is not None
        assert "secrets" in result

    def test_empty_allowed_denies_all(self):
        result = check_table_access({"users"}, [])
        assert result is not None

    def test_empty_tables_allowed(self):
        result = check_table_access(set(), ["users"])
        assert result is None

    def test_none_allowed_tables_allows_all(self):
        result = check_table_access({"users", "anything"}, None)
        assert result is None
