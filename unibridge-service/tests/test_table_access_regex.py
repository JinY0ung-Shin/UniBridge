"""Tests for the regex-based fallback in table_access (when sqlglot fails)."""
from __future__ import annotations

from app.services.table_access import (
    _clean_name,
    _extract_tables_with_regex,
    extract_tables,
)


def test_clean_name_strips_brackets_and_lowercases():
    assert _clean_name("[Users]") == "users"
    assert _clean_name("USERS") == "users"


def test_regex_fallback_simple_from():
    assert _extract_tables_with_regex("SELECT * FROM users") == {"users"}


def test_regex_fallback_with_schema():
    assert _extract_tables_with_regex("SELECT * FROM public.users") == {"users"}


def test_regex_fallback_join():
    out = _extract_tables_with_regex(
        "SELECT * FROM u JOIN o ON u.id = o.uid"
    )
    assert out == {"u", "o"}


def test_regex_fallback_update_and_into():
    assert _extract_tables_with_regex("UPDATE users SET x=1") == {"users"}
    assert _extract_tables_with_regex("INSERT INTO orders VALUES (1)") == {"orders"}


def test_regex_fallback_mssql_brackets():
    assert _extract_tables_with_regex("SELECT * FROM [dbo].[Users]") == {"users"}


def test_regex_fallback_comma_separated():
    out = _extract_tables_with_regex(
        "SELECT * FROM users, orders, products"
    )
    assert out == {"users", "orders", "products"}


def test_regex_fallback_comma_with_schemas():
    out = _extract_tables_with_regex(
        "SELECT * FROM public.users, sales.orders"
    )
    assert out == {"users", "orders"}


def test_extract_tables_falls_back_when_parser_returns_empty(monkeypatch):
    """When sqlglot returns an empty set, the regex fallback should run."""
    import app.services.table_access as ta

    monkeypatch.setattr(ta, "table_names", lambda sql, db_type="postgres": set())

    assert ta.extract_tables("SELECT * FROM users JOIN orders ON 1=1") == {
        "users",
        "orders",
    }


def test_extract_tables_returns_empty_when_neither_finds_tables(monkeypatch):
    import app.services.table_access as ta

    monkeypatch.setattr(ta, "table_names", lambda sql, db_type="postgres": set())
    assert ta.extract_tables("garbled garbled garbled") == set()


def test_extract_tables_uses_parser_when_it_finds_tables(monkeypatch):
    """If sqlglot finds tables, the regex fallback must not run."""
    import app.services.table_access as ta

    monkeypatch.setattr(
        ta, "table_names", lambda sql, db_type="postgres": {"from_parser"}
    )
    assert ta.extract_tables("anything goes here") == {"from_parser"}


def test_extract_tables_passes_db_type(monkeypatch):
    import app.services.table_access as ta

    received = {}

    def fake(sql, db_type="postgres"):
        received["db_type"] = db_type
        return {"x"}

    monkeypatch.setattr(ta, "table_names", fake)
    extract_tables("SELECT 1", db_type="mssql")
    assert received["db_type"] == "mssql"
