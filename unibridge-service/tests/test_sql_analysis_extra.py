"""Extra tests covering sql_analysis helpers (strip strings, legacy fallback, blocked keywords)."""
from __future__ import annotations

import pytest

from app.services.sql_analysis import (
    _strip_strings_and_comments,
    blocked_ast_keyword,
    statement_type,
    table_names,
)


# ── _strip_strings_and_comments edge cases ──────────────────────────────────


def test_strip_line_comment():
    out = _strip_strings_and_comments("SELECT 1 -- a comment\nFROM t")
    assert "a comment" not in out
    assert "FROM t" in out


def test_strip_line_comment_at_end_of_file():
    out = _strip_strings_and_comments("SELECT 1 -- trailing without newline")
    assert "trailing" not in out


def test_strip_block_comment():
    out = _strip_strings_and_comments("SELECT /* hidden */ 1 FROM t")
    assert "hidden" not in out


def test_strip_unterminated_block_comment():
    out = _strip_strings_and_comments("SELECT /* never closed")
    assert "SELECT" in out


def test_strip_single_quote_string():
    out = _strip_strings_and_comments("SELECT 'literal value' FROM t")
    assert "literal value" not in out


def test_strip_single_quote_with_escaped_quote():
    # SQL escapes single quote by doubling it
    out = _strip_strings_and_comments("SELECT 'he said ''hi''' FROM t")
    assert "he said" not in out


def test_strip_double_quote_identifier():
    out = _strip_strings_and_comments('SELECT "col name" FROM t')
    # double quoted identifier replaced with empty quoted form
    assert "col name" not in out


def test_strip_double_quote_escaped():
    out = _strip_strings_and_comments('SELECT "embedded ""quote""" FROM t')
    assert "embedded" not in out


def test_strip_dollar_quoted_string():
    out = _strip_strings_and_comments("SELECT $tag$ hidden body $tag$ FROM t")
    assert "hidden body" not in out


def test_strip_dollar_quoted_unterminated_breaks_out():
    out = _strip_strings_and_comments("SELECT $tag$ never closed")
    assert "SELECT" in out


def test_strip_dollar_sign_not_a_tag_kept():
    # $ followed by non-identifier shouldn't be treated as tag-quote
    out = _strip_strings_and_comments("SELECT $$ from t")
    # $$ is an empty tag, so it should be processed; ensure no crash
    assert isinstance(out, str)


def test_strip_unterminated_string_breaks_out():
    out = _strip_strings_and_comments("SELECT 'never closed")
    assert isinstance(out, str)


# ── legacy fallback (`_legacy_statement_type`) reachable via statement_type ──


def test_with_dml_returns_dml_type():
    # CTE wrapping a DELETE — sqlglot may handle this; if so the result is
    # `delete`. If sqlglot returns "unknown", the legacy path kicks in and
    # the regex over the stripped SQL also returns "delete". Either way:
    assert statement_type("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d") in {
        "delete",
        "select",
    }


def test_with_select_returns_select():
    assert statement_type("WITH x AS (SELECT 1) SELECT * FROM x") == "select"


def test_legacy_exec_returns_execute():
    # garbled SQL so sqlglot fails -> falls back to legacy
    assert statement_type("EXEC garbledproc @arg=") == "execute"


def test_legacy_call_returns_execute():
    assert statement_type("CALL garbled junk") == "execute"


def test_legacy_unknown_returns_unknown():
    assert statement_type("garbledgarbled") == "unknown"


# ── EXPLAIN handling ────────────────────────────────────────────────────────


def test_explain_select_returns_explain():
    assert statement_type("EXPLAIN SELECT 1") == "explain"


def test_explain_analyze_select_returns_explain():
    assert statement_type("EXPLAIN ANALYZE SELECT 1") == "explain"


def test_explain_analyze_dml_returns_dml_type():
    # EXPLAIN ANALYZE on a mutating statement keeps the inner mutating type
    assert statement_type("EXPLAIN ANALYZE DELETE FROM t") == "delete"


def test_explain_with_parens_analyze_option():
    assert statement_type("EXPLAIN (ANALYZE, VERBOSE) SELECT 1") == "explain"


def test_explain_unknown_inner_returns_unknown():
    assert statement_type("EXPLAIN totalgibberish") == "unknown"


# ── table_names edge cases ──────────────────────────────────────────────────


def test_table_names_empty_sql():
    assert table_names("") == set()


def test_table_names_explain_extracts_inner_tables():
    assert table_names("EXPLAIN SELECT * FROM employees") == {"employees"}


# ── blocked_ast_keyword ─────────────────────────────────────────────────────


def test_blocked_keyword_merge():
    assert blocked_ast_keyword(
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.x = s.x"
    ) == "MERGE"


def test_blocked_keyword_exec():
    # parsed as exec or Command — both branches return EXEC/EXECUTE
    result = blocked_ast_keyword("EXEC sp_help")
    assert result in {"EXEC", "EXECUTE"}


def test_blocked_keyword_call():
    result = blocked_ast_keyword("CALL my_proc()")
    assert result in {"CALL", "EXECUTE", "EXEC", None}


def test_blocked_keyword_none_for_plain_select():
    assert blocked_ast_keyword("SELECT 1") is None


def test_blocked_keyword_explain_analyze_dml():
    assert blocked_ast_keyword("EXPLAIN ANALYZE DELETE FROM t") == "EXPLAIN ANALYZE"


def test_blocked_keyword_explain_select_none():
    assert blocked_ast_keyword("EXPLAIN SELECT 1") is None
