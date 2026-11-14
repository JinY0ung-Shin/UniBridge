"""The SQL lexer shared by the query gates reads text the way each engine does.

A gate that reads a quote or comment the database does not see lets the rest
of the text through unchecked. Each case below once passed every gate
(statement separators, the keyword and function deny-lists, statement types
and table access) while the database ran a write or an outside call.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app
from app.auth import ApiKeyUser, invalidate_permission_cache
from app.models import Permission
from app.routers.query import _detect_statement_type
from app.services import mcp_tools, sql_analysis
from app.services.query_executor import (
    check_multi_statement,
    execute_clickhouse_json,
    execute_clickhouse_query,
    execute_query,
)
from app.services.sql_analysis import (
    blocked_ast_keyword,
    keyword_texts,
    sql_code_text,
    sql_lexings,
    statement_type,
    strict_statement_type,
    table_names,
)
from app.services.sql_validator import validate_sql
from app.services.table_access import check_table_access, extract_tables
from tests.conftest import auth_header


def _code_text(sql: str, db_type: str, **kwargs) -> str:
    return sql_code_text(sql, sql_lexings(db_type)[0], **kwargs)


# ── `$` inside a name is not a dollar quote ─────────────────────────────────


@pytest.mark.parametrize(
    ("db_type", "sql", "blocked"),
    [
        ("mssql", "SELECT 1 AS a$b$c; DROP TABLE t; COMMIT", "DROP TABLE"),
        ("mssql", "SELECT [a$b$c]; SHUTDOWN", "SHUTDOWN"),
        ("postgres", "SELECT 1 AS a$b$c; DROP TABLE t; COMMIT", "DROP TABLE"),
        ("clickhouse", "SELECT 1 AS a$b$c; DROP TABLE t", "DROP TABLE"),
        # `$` inside quoted names of each engine
        ("mssql", "SELECT 1 AS [$x$]; DROP TABLE t; SELECT 1 AS [$x$]", "DROP TABLE"),
        ("postgres", 'SELECT 1 AS "$x$"; DROP TABLE t; SELECT 1 AS "$x$"', "DROP TABLE"),
        ("clickhouse", "SELECT 1 AS `$x$`; DROP TABLE t; SELECT 1 AS `$x$`", "DROP TABLE"),
        # T-SQL has no dollar quotes at all: `$` is part of names and money
        ("mssql", "SELECT $x$; DROP TABLE t; SELECT $x$", "DROP TABLE"),
        ("mssql", "SELECT $a$; DROP TABLE t", "DROP TABLE"),
    ],
)
def test_dollar_in_a_name_does_not_hide_the_next_statement(db_type, sql, blocked):
    assert check_multi_statement(sql, db_type) is True
    assert validate_sql(sql, db_type=db_type) == f"Blocked SQL keyword: {blocked}"
    assert strict_statement_type(sql, db_type) is None


@pytest.mark.parametrize(
    ("db_type", "sql"),
    [
        ("mssql", "SELECT 1 AS a$b$c; DROP TABLE t; COMMIT"),
        ("mssql", "SELECT 1 AS a$b$c DELETE FROM t"),
        ("mssql", "SELECT 1 AS [$x$] DELETE FROM t SELECT 1 AS [$x$]"),
        ("postgres", "SELECT 1 AS a$b$c; DROP TABLE t; COMMIT"),
        ("clickhouse", "SELECT 1 AS `$x$`; DELETE FROM t; SELECT 1 AS `$x$`"),
    ],
)
def test_dollar_in_a_name_does_not_turn_a_write_into_a_read(db_type, sql):
    assert _detect_statement_type(sql, db_type) == "execute"


# ── Parsers of other engines ────────────────────────────────────────────────

# A Postgres or ClickHouse parser reads `$x$ ... $x$` as one string; SQL Server
# runs the three statements, with no semicolon to give them away.
_HEREDOC_BATCHES = [
    "SELECT $x$ DELETE FROM t SELECT $x$",
    "SELECT 1[$x$] DELETE FROM t COMMIT SELECT 1[$x$]",
]


@pytest.mark.parametrize("sql", _HEREDOC_BATCHES)
def test_another_engines_parser_does_not_read_a_tsql_batch_as_one_select(sql):
    assert check_multi_statement(sql, "mssql") is False  # no semicolon to catch
    assert _detect_statement_type(sql, "mssql") == "execute"
    assert strict_statement_type(sql, "mssql") is None


def test_another_engines_parser_does_not_hide_a_tsql_table():
    sql = "SELECT * FROM allowed[$x$] SELECT * FROM secret SELECT 1[$x$]"
    assert "secret" in extract_tables(sql, db_type="mssql")
    sql = "SELECT * FROM allowed WHERE 1 = $x$ SELECT * FROM secret SELECT $x$"
    assert "secret" in extract_tables(sql, db_type="mssql")


# The parser also takes a reserved word for an alias (`SELECT 1 AS DELETE FROM
# users AS COMMIT`); SQL Server reserves it and runs a DELETE.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 DELETE FROM users COMMIT",
        "SELECT [a$b$c] DELETE FROM t COMMIT",
        "INSERT INTO t SELECT 1 DELETE FROM u",
        "INSERT INTO t EXEC proc",
    ],
)
def test_reserved_words_the_parser_reads_as_aliases_still_count_in_tsql(sql):
    assert _detect_statement_type(sql, "mssql") == "execute"
    assert strict_statement_type(sql, "mssql") is None


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT [delete], deleted, 'drop table' AS note FROM t -- update", "select"),
        ("UPDATE t SET x = 1 OUTPUT deleted.x, inserted.x", "update"),
        (
            "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = 1 "
            "WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id);",
            "merge",
        ),
    ],
)
def test_tsql_statements_keep_their_type(sql, kind):
    assert _detect_statement_type(sql, "mssql") == kind


def test_fallback_parsers_still_read_text_every_engine_splits_alike():
    # sqlglot's T-SQL parser does not take TOP; the Postgres one does.
    assert strict_statement_type("SELECT TOP 5 * FROM users", "mssql") == "select"
    assert _detect_statement_type("SELECT TOP 5 [name] FROM [dbo].[users]", "mssql") == "select"
    assert extract_tables("SELECT TOP 5 [name] FROM [dbo].[users]", db_type="mssql") == {"users"}
    # A caller that names no engine gets every parser, as before.
    assert statement_type("SELECT $x$ DELETE FROM t SELECT $x$") == "select"


# ── Real dollar quotes, parameters and quoted text keep working ─────────────


@pytest.mark.parametrize("db_type", ["postgres", "clickhouse"])
@pytest.mark.parametrize("sql", ["SELECT $$a;b$$", "SELECT $tag$a; DROP TABLE t$tag$"])
def test_semicolons_in_real_dollar_quotes_are_text(db_type, sql):
    assert check_multi_statement(sql, db_type) is False
    assert validate_sql(sql, db_type=db_type) is None
    assert _detect_statement_type(sql, db_type) == "select"


def test_positional_parameters_are_not_dollar_quotes():
    assert check_multi_statement("SELECT * FROM t WHERE a = $1 AND b = $2", "postgres") is False
    assert check_multi_statement("SELECT $1; DROP TABLE t", "postgres") is True
    assert validate_sql("SELECT $1, $2$ DROP TABLE t $2$", db_type="postgres") is not None


def test_unclosed_dollar_quotes_follow_each_engine():
    # Postgres rejects the text; ClickHouse reads `$tag$` as a name and runs on.
    assert check_multi_statement("SELECT $tag$a;b", "postgres") is False
    assert check_multi_statement("SELECT $tag$a;b", "clickhouse") is True


def test_dollar_quotes_are_not_tsql():
    assert check_multi_statement("SELECT $$a;b$$", "mssql") is True


@pytest.mark.parametrize(
    ("db_type", "sql"),
    [
        ("mssql", "SELECT [a;b], [it's], [x]]; y] FROM t"),
        ("mssql", "SELECT \"a;b\", N'c;d' FROM t"),
        ("postgres", "SELECT E'it\\'s; ok', 'a;b', \"c;d\" FROM t"),
        ("clickhouse", "SELECT 'it\\'s; ok', `a;b`, \"c;d\" FROM t"),
        ("postgres", "SELECT 1 /* outer /* inner; */ still; */ FROM t"),
        ("clickhouse", "SELECT 1 # comment; here\nFROM t"),
        ("postgres", "SELECT 1;  \n"),
    ],
)
def test_semicolons_in_quotes_and_comments_are_not_separators(db_type, sql):
    assert check_multi_statement(sql, db_type) is False


# ── Comments read as each engine reads them ─────────────────────────────────


@pytest.mark.parametrize(
    ("db_type", "sql", "blocked"),
    [
        # a comment separates words like a space
        ("mssql", "SELECT 1 CREATE/**/LOGIN evil WITH PASSWORD = 'x'", "CREATE LOGIN"),
        ("mssql", "SELECT 1 DROP--x\nTABLE t", "DROP TABLE"),
        # block comments nest
        ("mssql", "SELECT 1 /* /* */ ' */ ; SHUTDOWN; -- '", "SHUTDOWN"),
    ],
)
def test_comments_do_not_hide_keywords(db_type, sql, blocked):
    error = validate_sql(sql, db_type=db_type) or ""
    assert error.removeprefix("Blocked SQL keyword: ").split() == blocked.split()


def test_a_lone_carriage_return_ends_a_tsql_comment_or_not():
    # Undocumented in T-SQL, so either reading counts.
    assert check_multi_statement("SELECT 1 --x\r; SHUTDOWN", "mssql") is True
    assert check_multi_statement("SELECT 1 --x\r'\n; SHUTDOWN --'", "mssql") is True


# ── Disguised calls to outside-reaching functions ───────────────────────────


@pytest.mark.parametrize(
    ("db_type", "sql"),
    [
        # Postgres: E'' takes backslash escapes, a plain string does not
        ("postgres", "SELECT E'\\'' , 'a\\' , pg_read_file('x') , 'z'"),
        # Postgres: a lone CR ends a -- comment
        ("postgres", "SELECT 1 --x\r, pg_read_file('x')"),
        # Postgres: every non-ASCII character belongs to a name, and may tag a quote
        ("postgres", "SELECT 1 AS →$a$, pg_read_file('x') AS →$a$"),
        ("postgres", "SELECT $→$ ' $→$, pg_read_file('x') -- '"),
        # ClickHouse: heredoc tags may start with a digit
        ("clickhouse", "SELECT $1$ ' $1$ FROM url('http://x', CSV) -- '"),
        # ClickHouse: a non-ASCII space starts a token
        ("clickhouse", "SELECT $a$ ' $a$ FROM url('http://x', CSV) -- '"),
        # ClickHouse: "# " starts a comment
        ("clickhouse", "SELECT 1 # '\n FROM url('http://x', CSV) -- '"),
        # ClickHouse: an unclosed heredoc is a name
        ("clickhouse", "SELECT $a$, x FROM url('http://x', CSV)"),
    ],
)
def test_lexing_tricks_do_not_hide_calls(db_type, sql):
    assert "Blocked SQL function" in (validate_sql(sql, db_type=db_type) or "")


@pytest.mark.parametrize(
    ("db_type", "sql"),
    [
        ("postgres", "SELECT $→$ pg_read_file('x') $→$"),
        ("postgres", "SELECT E'it\\'s pg_read_file(x)'"),
        ("clickhouse", "SELECT $1$ url('x') $1$"),
        ("clickhouse", "SELECT 1 # url('x')\n"),
        ("clickhouse", "SELECT 'it\\'s url(x)'"),
    ],
)
def test_calls_inside_real_literals_and_comments_are_text(db_type, sql):
    assert validate_sql(sql, db_type=db_type) is None


# ── The shared code text ────────────────────────────────────────────────────


def test_code_text_blanks_or_unwraps_quoted_names():
    sql = "SELECT [a b], \"c\"\"d\", `e`, 'f' /* g */ -- h\nFROM t"
    assert _code_text(sql, "mssql") == "SELECT \"\", \"\", \"\", ''    \nFROM t"
    assert _code_text(sql, "mssql", unwrap_identifiers=True) == "SELECT a b, c\"d, e, ''    \nFROM t"


def test_code_text_reads_postgres_unicode_names_only_in_postgres():
    sql = 'SELECT U&"\\0041"'
    assert _code_text(sql, "postgres", unwrap_identifiers=True) == "SELECT A"
    assert _code_text(sql, "mssql", unwrap_identifiers=True) == "SELECT U&\\0041"


def test_unknown_engines_get_every_reading():
    assert sql_lexings(None) == sql_lexings("neo4j")
    assert len(sql_lexings(None)) == 5
    assert check_multi_statement("SELECT $$a;b$$", None) is True


# ── Through the gates ───────────────────────────────────────────────────────


async def _grant_select_only(seeded_db) -> None:
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(
            Permission(
                role="querier",
                db_alias="sales",
                allow_select=True,
                allow_insert=False,
                allow_update=False,
                allow_delete=False,
            )
        )
        await db.commit()
    await invalidate_permission_cache()


@pytest.mark.parametrize(
    "sql",
    [
        *_HEREDOC_BATCHES,
        "SELECT 1 AS a$b$c DELETE FROM t COMMIT",
        "SELECT 1 AS [$x$] DELETE FROM t SELECT 1 AS [$x$]",
    ],
)
async def test_select_only_role_cannot_write_to_sql_server(client, seeded_db, querier_token, sql):
    await _grant_select_only(seeded_db)
    executor = AsyncMock()
    with (
        patch("app.routers.query.connection_manager.get_db_type", return_value="mssql"),
        patch("app.routers.query.connection_manager.get_engine", return_value=MagicMock()),
        patch("app.routers.query.execute_query", executor),
        patch("app.routers.query.log_query", new_callable=AsyncMock),
    ):
        response = await client.post(
            "/query/execute",
            json={"database": "sales", "sql": sql},
            headers=auth_header(querier_token),
        )
    assert response.status_code == 403, response.text
    assert "EXECUTE" in response.json()["detail"]
    executor.assert_not_awaited()


@pytest.mark.parametrize("sql", _HEREDOC_BATCHES)
async def test_mcp_run_query_refuses_a_tsql_batch(sql):
    user = ApiKeyUser(consumer_name="agent", allowed_databases=["sales"], allowed_routes=[])
    ctx = SimpleNamespace(user=user)
    gateway = AsyncMock()
    with (
        patch.object(mcp_tools.connection_manager, "get_db_type", return_value="mssql"),
        patch.object(mcp_tools, "_gateway", gateway),
        pytest.raises(mcp_tools.ToolError, match="read-only"),
    ):
        await mcp_tools._run_query(ctx, {"database": "sales", "sql": sql})
    gateway.assert_not_awaited()


async def test_executors_check_separators_for_their_engine():
    with pytest.raises(ValueError, match="Multi-statement SQL is not allowed"):
        await execute_query(MagicMock(), "SELECT $$a$$; DROP TABLE t", db_type="mssql")
    with pytest.raises(ValueError, match="Multi-statement SQL is not allowed"):
        await execute_clickhouse_query(MagicMock(), "SELECT $tag$a; DROP TABLE t")
    with pytest.raises(ValueError, match="Multi-statement SQL is not allowed"):
        await execute_clickhouse_json(MagicMock(), "SELECT $tag$a; DROP TABLE t")


@pytest.mark.parametrize(
    ("db_type", "sql"),
    [
        ("postgres", "SELECT 1 - 2 / 3"),
        ("clickhouse", "SELECT 1 #x"),  # not a comment: ClickHouse rejects it
    ],
)
def test_lone_comment_characters_are_code(db_type, sql):
    assert _code_text(sql, db_type) == sql


def test_clickhouse_names_take_backslash_escapes():
    assert validate_sql("SELECT * FROM `ur\\l`('http://x', 'CSV')", db_type="clickhouse") is not None


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("WITH x AS (SELECT 1) SELECT * FROM x WHERE", "select"),
        ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d WHERE", "delete"),
        ("WITH d AS (DELETE FROM t RETURNING *) INSERT INTO u SELECT * FROM d WHERE", "execute"),
    ],
)
def test_regex_fallback_for_ctes(sql, kind):
    assert statement_type(sql, "postgres") == kind


# ── Round 2: keywords glued to numbers, hints, names ────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1DELETE FROM users COMMIT",
        "SELECT 1UPDATE users SET a = 1 COMMIT",
        "SELECT 1INSERT INTO users VALUES (1) COMMIT",
        "SELECT 1exec sp_who",  # 1 and EXEC, or the float 1e and xec
        "SELECT 1edelete FROM users",
        "SELECT $1DELETE FROM users",  # a money literal
    ],
)
def test_a_number_does_not_glue_onto_the_next_tsql_statement(sql):
    assert _detect_statement_type(sql, "mssql") == "execute"
    assert strict_statement_type(sql, "mssql") is None


@pytest.mark.parametrize(
    ("sql", "blocked"),
    [
        ("SELECT 1DROP TABLE users", "DROP TABLE"),
        ("SELECT 1TRUNCATE TABLE users", "TRUNCATE"),
        ("SELECT 1GRANT SELECT ON t TO public", "GRANT"),
    ],
)
def test_blocked_keywords_glued_to_numbers(sql, blocked):
    assert validate_sql(sql, db_type="mssql") == f"Blocked SQL keyword: {blocked}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a.id FROM dbo.a INNER MERGE JOIN dbo.b ON a.id = b.id",
        "SELECT a.id FROM dbo.a LEFT OUTER MERGE JOIN dbo.b ON a.id = b.id",
        "SELECT a.id FROM dbo.a JOIN dbo.b ON a.id = b.id OPTION (MERGE JOIN)",
        "SELECT id FROM dbo.a UNION SELECT id FROM dbo.b OPTION (MERGE UNION)",
        "SELECT * FROM t OPTION (USE HINT ('FORCE_LEGACY_CARDINALITY_ESTIMATION'))",
        "SELECT * FROM #update JOIN ##delete ON 1 = 1",
        "SELECT @insert AS i, col1update, x.[delete] FROM x",
    ],
)
def test_tsql_hints_and_prefixed_names_are_not_statements(sql):
    assert _detect_statement_type(sql, "mssql") == "select"
    assert validate_sql(sql, db_type="mssql") is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS a ENABLE TRIGGER tr ON t",
        "SELECT 1 AS a DISABLE TRIGGER ALL ON DATABASE",
        "SELECT 1 AS a WRITETEXT t.c @p 'x'",
        "SELECT 1 AS a UPDATETEXT t.c @p 0 NULL 'x'",
        "SELECT 1 AS a RECEIVE TOP (1) * FROM q",
        "SELECT 1 AS a RECEIVE message_body FROM q",
        "SELECT 1 AS a RECEIVE CAST(message_body AS varchar(10)) FROM q",
        "SELECT 1 AS a SEND ON CONVERSATION @h ('x')",
        "SELECT 1 AS a RECONFIGURE",
        # session and batch control stays on the pooled connection
        "SELECT 1 AS a USE master",
        "SELECT 1 AS a SET ROWCOUNT 1",
        "SELECT 1 AS a WAITFOR DELAY '01:00'",
        "SELECT 1 AS a DECLARE @x int",
        "SELECT 1 AS a BEGIN TRAN",
        "SELECT 1 AS a SETUSER 'dbo'",
    ],
)
def test_tsql_statements_without_reserved_writes_still_count(sql):
    assert _detect_statement_type(sql, "mssql") == "execute"
    assert strict_statement_type(sql, "mssql") is None


def test_a_set_beyond_the_updates_is_a_statement():
    assert _detect_statement_type("UPDATE t SET a = 1 WHERE id = 2", "mssql") == "update"
    assert _detect_statement_type("UPDATE t SET a = 1 SET ROWCOUNT 1", "mssql") == "execute"


@pytest.mark.parametrize(
    ("db_type", "sql"),
    [
        ("postgres", "SELECT * INTO new_t FROM t"),
        ("mssql", "SELECT * INTO #tmp FROM t"),
        ("mssql", "SELECT a INTO #tmp FROM t UNION SELECT b FROM u"),
    ],
)
def test_select_into_creates_a_table(db_type, sql):
    assert _detect_statement_type(sql, db_type) == "create"
    assert strict_statement_type(sql, db_type) == "create"


def test_select_into_in_the_regex_fallback():
    assert statement_type("SELECT * INTO new_t FROM t WHERE", "postgres") == "execute"


def test_set_operations_of_selects_are_reads():
    assert strict_statement_type("SELECT 1 UNION SELECT 2", "postgres") == "select"
    assert strict_statement_type("(SELECT 1) EXCEPT (SELECT 2) INTERSECT SELECT 3", "postgres") == "select"
    deep = " UNION ALL ".join(["SELECT 1"] * 3000)
    assert strict_statement_type(deep, "postgres") in ("select", None)  # no RecursionError


# ── Round 2: spaces, escapes, EXPLAIN ───────────────────────────────────────


@pytest.mark.parametrize("space", ["\u200b", "\u200c", "\u200d", "\u2060", "\u180e", "\ufeff"])
def test_zero_width_spaces_do_not_hide_clickhouse_calls(space):
    error = validate_sql(f"SELECT * FROM file{space}('/etc/passwd')", db_type="clickhouse")
    assert "file()" in (error or "")


@pytest.mark.parametrize(
    "name",
    ["oss", "cosn", "arrowFlight", "paimonS3", "paimonLocal", "hive", "ytsaurus", "prometheusQuery",
     "kafka", "s3Queue", "distributed"],
)
def test_more_clickhouse_functions_that_reach_out(name):
    assert validate_sql(f"SELECT * FROM {name}('x')", db_type="clickhouse") is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT U&\"pg!005fread!005ffile\" UESCAPE '!'('/etc/passwd')",
        "SELECT U&\"dblink!005fexec\" UESCAPE '!'('h', 'drop table t')",
        "SELECT U&\"pg!005fread!005ffile\" /* c */ UESCAPE /* d */ '!'('/etc/passwd')",
    ],
)
def test_uescape_names_still_read_as_calls(sql):
    assert "Blocked SQL function" in (validate_sql(sql, db_type="postgres") or "")


def test_escapes_past_unicode_are_read_as_written():
    assert validate_sql('SELECT U&"\\+FFFFFF"', db_type="postgres") is None
    assert _code_text('SELECT U&"\\+FFFFFF"', "postgres", unwrap_identifiers=True) == "SELECT \\+FFFFFF"


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN (ANALYSE) DELETE FROM t",
        "EXPLAIN (U&\"\\0061nalyze\") DELETE FROM t",
        "EXPLAIN (\"analyze\", VERBOSE) DELETE FROM t",
        "EXPLAIN (VERBOSE, /* ) */ ANALYZE) DELETE FROM t",
        "EXPLAIN /* c */ ANALYZE DELETE FROM t",
        "EXPLAIN ANALYZE\nDELETE FROM t",
        "EXPLAIN ANALYSE VERBOSE DELETE FROM t",
    ],
)
def test_every_spelling_of_explain_analyze_runs_the_statement(sql):
    assert _detect_statement_type(sql, "postgres") == "delete"
    assert validate_sql(sql, db_type="postgres") == "Blocked SQL keyword: EXPLAIN ANALYZE"


def test_explain_without_analyze_only_plans():
    assert _detect_statement_type("EXPLAIN VERBOSE DELETE FROM t", "postgres") == "explain"
    assert _detect_statement_type("EXPLAIN (COSTS false) DELETE FROM t", "postgres") == "explain"


def test_postgres_dollar_quote_after_a_number_or_parameter():
    # Postgres: 1 then a dollar quote; a1$q$ is one name.
    assert _code_text("SELECT 1$q$;$q$", "postgres") == "SELECT 1''"
    assert _code_text("SELECT $1$q$;$q$", "postgres") == "SELECT $1''"
    assert _code_text("SELECT a1$q$, 1 FROM t", "postgres") == "SELECT a1$q$, 1 FROM t"
    # ClickHouse too ends a number there, and reads a1$q$ on as one word
    assert _code_text("SELECT 1$q$;$q$", "clickhouse") == "SELECT 1''"
    assert _code_text("SELECT a1$q$;$q$", "clickhouse") == "SELECT a1$q$;$q$"


def test_backslash_reading_does_not_refuse_postgres_reads():
    # A string ending in a backslash reads differently with
    # standard_conforming_strings off; one statement still runs either way.
    sql = "SELECT replace(path, '\\', '/') AS p, 'drop table; x' AS note FROM files"
    assert validate_sql(sql, db_type="postgres") is None
    assert check_multi_statement(sql, "postgres") is False


# ── Round 2: table access ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("db_type", "sql", "hidden"),
    [
        ("postgres", "SELECT a FROM allowed UNION ALL TABLE secret", "secret"),
        ("postgres", "SELECT * FROM allowed WHERE (a) IN (TABLE secret)", "secret"),
        ("postgres", "TABLE ONLY public.secret", "secret"),
        ("postgres", 'TABLE "Secret"', "table()"),
        ("clickhouse", "SELECT * FROM allowed, merge(currentDatabase(), '^secret$')", "merge()"),
        ("clickhouse", "SELECT 1 FROM allowed WHERE x IN secret", "secret"),
        ("clickhouse", "SELECT 1 FROM allowed WHERE x GLOBAL NOT IN db.secret", "secret"),
        ("clickhouse", "SELECT joinGet('db.secret', 'v', 1) FROM allowed", "secret"),
        ("clickhouse", "SELECT joinGet(name, 'v', 1) FROM allowed", "joinget()"),
        ("clickhouse", "SELECT * FROM allowed, loop(db, secret) LIMIT 1", "loop()"),
    ],
)
def test_tables_read_without_from_count(db_type, sql, hidden):
    tables = extract_tables(sql, db_type=db_type)
    assert hidden in tables
    assert check_table_access(tables, ["allowed"]) is not None


def test_table_after_if_exists_is_the_name():
    assert extract_tables("DROP TABLE IF EXISTS t", db_type="postgres") == {"t"}


# ── Round 2: lexing stays linear ────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 " + "".join(f"$a{i}$ " for i in range(20000)),
        "SELECT 1 " + "--\n" * 40000,
        "SELECT 1 " + "--x\r" * 40000,
        "SELECT 1 " + "/*" * 40000,
        "SELECT " + "a$" * 40000,
        "SELECT " + 'U&"a" UESCAPE \'!\', ' * 5000,
    ],
)
def test_pathological_text_lexes_in_linear_time(sql):
    for db_type in ("postgres", "mssql", "clickhouse"):
        sql_analysis._code.cache_clear()
        start = time.perf_counter()
        for lexing in sql_lexings(db_type):
            sql_code_text(sql, lexing)
            sql_code_text(sql, lexing, unwrap_identifiers=True)
        check_multi_statement(sql, db_type)
        assert time.perf_counter() - start < 2.0, db_type


def test_lexing_edges():
    # a number at the very start, and a name of non-ASCII letters and digits
    assert _code_text("1$q$;$q$", "postgres") == "1''"
    assert _code_text("SELECT é1$q$, 1", "postgres") == "SELECT é1$q$, 1"
    assert _code_text("SELECT é1$q$x$q$", "clickhouse") == "SELECT é1''"  # é is not a name to ClickHouse
    # a doubled UESCAPE character stands for itself
    assert _code_text("SELECT U&\"a!!b\" UESCAPE '!'", "postgres", unwrap_identifiers=True) == "SELECT a!b"
    # comments before ANALYZE, and an EXPLAIN whose options never close
    assert statement_type("EXPLAIN -- c\nANALYZE DELETE FROM t", "postgres") == "delete"
    assert statement_type("EXPLAIN (ANALYZE DELETE FROM t", "postgres") == "unknown"
    # a set operation over something other than SELECTs is not a plain read
    assert strict_statement_type("SELECT 1 UNION VALUES (2)", "postgres") is None


def test_text_too_deep_for_the_parser_is_unreadable():
    calls = []

    def too_deep(sql, read, error_level):
        calls.append(read)
        raise RecursionError

    sql_analysis._parsed.cache_clear()
    with patch.object(sql_analysis.sqlglot, "parse", too_deep):
        assert strict_statement_type("SELECT 1", "clickhouse") is None
        assert statement_type("SELECT 1", "clickhouse") == "select"  # the regex fallback
    sql_analysis._parsed.cache_clear()
    # one parse for both checks, and no other parser tried after it
    assert calls == ["clickhouse"]


# ── Round 3 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 0xUPDATE t SET a = 1 COMMIT",  # an empty binary constant, then UPDATE
        "SELECT 0x1FINSERT INTO t VALUES (1) COMMIT",
        "SELECT 0b1DELETE FROM t",
    ],
)
def test_hex_and_binary_literals_end_before_a_keyword(sql):
    assert _detect_statement_type(sql, "mssql") == "execute"
    assert strict_statement_type(sql, "mssql") is None


def test_hex_digits_belong_to_the_literal():
    # 0xDE then LETE, as SQL Server reads it: no DELETE there
    assert keyword_texts("SELECT 0xDELETE") == ["SELECT 0xDE LETE"]
    assert _detect_statement_type("SELECT 0xDELETE AS d", "mssql") == "select"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT " + "[a] " * 1000,  # 3KB: a subscript chain the parser reads in quadratic time
        "SELECT a" + "[1]" * 1000,
        "SELECT a" + "[1].b" * 200,
        "SELECT " + "1-" * 60000 + "1",  # more tokens than one check should parse
    ],
)
def test_text_too_complex_to_parse_is_checked_without_the_parser(sql):
    sql_analysis._parsed.cache_clear()
    with patch.object(sql_analysis.sqlglot, "parse", side_effect=AssertionError("parsed")):
        assert statement_type(sql, "postgres") == "select"  # the regex fallback
        assert strict_statement_type(sql, "postgres") is None
        assert extract_tables(sql, db_type="postgres") == set()
    sql_analysis._parsed.cache_clear()


def test_too_complex_text_still_meets_the_blocked_statements():
    sql = "EXEC sp_who " + "-1" * 60000
    assert validate_sql(sql, db_type="mssql") == "Blocked SQL keyword: EXEC"
    assert _detect_statement_type(sql, "mssql") == "execute"
    sql = "CALL p(" + ", ".join(["1"] * 30000) + ")"
    assert validate_sql(sql, db_type="postgres") == "Blocked SQL keyword: CALL"
    assert validate_sql("SELECT " + "1-" * 60000 + "1", db_type="postgres") is None


def test_large_real_queries_are_still_parsed():
    insert = "INSERT INTO t (a, b, c, d, e) VALUES " + ", ".join(
        f"({i}, 'x{i}', 1.5, NULL, true)" for i in range(2000)
    )
    assert strict_statement_type(insert, "postgres") == "insert"
    nested = "SELECT [[1, 2], [3, 4]]" + ", [[1, 2], [3, 4]]" * 500
    assert strict_statement_type(nested, "clickhouse") == "select"
    assert strict_statement_type("SELECT m[1][2][3] FROM t", "postgres") == "select"


def test_subscript_chains_are_measured_per_depth():
    chain = sql_analysis._longest_subscript_chain
    assert chain("a[1][2] , b[3]") == 2
    assert chain("a[1] . b [2] . c[3]") == 3
    assert chain("a[b[1][2][3]][4]") == 3
    assert chain("[1], [2], [3]") == 1
    assert chain("a]]][1]") == 1  # stray closers


def test_one_parse_serves_every_check_of_a_query():
    sql = "SELECT a FROM t WHERE b = 1"
    sql_analysis._parsed.cache_clear()
    with patch.object(sql_analysis, "_parse", wraps=sql_analysis._parse) as parse:
        statement_type(sql, "postgres")
        strict_statement_type(sql, "postgres")
        table_names(sql, "postgres")
        blocked_ast_keyword(sql, "postgres")
    assert parse.call_count == 1


def test_postgres_runs_one_statement_per_query():
    """The checks for statement separators and statement keywords leave out
    the standard_conforming_strings=off reading because Postgres gets one
    statement per query: SQLAlchemy's asyncpg dialect prepares each one, and
    the extended protocol takes no more. A path that sends text another way
    (a driver-level execute, the raw driver connection, psycopg's simple
    queries) would run several, and must bring that reading back."""
    app_dir = Path(app.__file__).parent
    assert "postgresql+asyncpg://" in (app_dir / "services" / "connection_manager.py").read_text()
    for path in app_dir.rglob("*.py"):
        text = path.read_text()
        for marker in ("exec_driver_sql", "driver_connection", "raw_connection", "psycopg"):
            assert marker not in text, f"{path}: {marker}"


def test_long_text_gets_only_its_own_engines_parser():
    # The T-SQL parser rejects ~; the Postgres one would read this, as it
    # still does for short text, but not past 12,500 tokens.
    short = "SELECT x FROM t WHERE x ~ 'a'"
    long = short + " AND id IN (" + ", ".join(str(i) for i in range(7000)) + ")"
    assert strict_statement_type(short, "mssql") == "select"
    assert strict_statement_type(long, "mssql") is None
    assert statement_type(long, "mssql") == "select"  # the regex fallback
