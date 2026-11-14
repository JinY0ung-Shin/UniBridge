"""AST-backed SQL analysis helpers used by query authorization."""
from __future__ import annotations

import bisect
import functools
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Literal, NamedTuple

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.errors import ParseError, TokenError


_DIALECT_BY_DB_TYPE = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "clickhouse": "clickhouse",
}

_MUTATING_TYPES = {
    "insert",
    "update",
    "delete",
    "merge",
    "create",
    "alter",
    "drop",
    "truncate",
    "execute",
}

_LEGACY_SQL_TYPE_RE = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH|EXPLAIN|CREATE|ALTER|DROP|TRUNCATE|EXEC|EXECUTE|CALL|DO|MERGE)\b",
    re.IGNORECASE,
)
_DML_TYPES = {"insert", "update", "delete", "merge"}


# ── Keywords ────────────────────────────────────────────────────────────────
#
# A keyword starts where no name character precedes it: `#update` (a T-SQL
# temp table), `@delete` (a variable) and `col1update` are names. A number
# ends before letters, so in T-SQL `SELECT 1DELETE FROM t` is SELECT 1 and a
# DELETE; keyword_texts() puts a space after such a number first.

_NAME_CHARS = r"\w@#$"


def keyword_re(words: Iterable[str]) -> re.Pattern[str]:
    """Match any of ``words`` (regex fragments) as a whole keyword in keyword_texts()."""
    return re.compile(
        rf"(?<![{_NAME_CHARS}])(?:{'|'.join(words)})(?![{_NAME_CHARS}])", re.IGNORECASE
    )


# Hexadecimal (T-SQL binary 0x1F, Postgres 16 and ClickHouse 0x1F), binary
# (0b101: Postgres 16, ClickHouse) and octal (0o17: Postgres 16) literals end
# at their last digit too: T-SQL reads `0x1FINSERT` as 0x1F and an INSERT.
_NUMBER = r"(?<![\w@#])(?:0[xX][0-9A-Fa-f]*|0[bB][01]*|0[oO][0-7]*|\d+\.?\d*|\.\d+)"
_NUMBER_BEFORE_WORD_RE = re.compile(_NUMBER + r"(?=[^\W\d])")
_FLOAT_BEFORE_WORD_RE = re.compile(_NUMBER + r"[eE][+-]?\d*(?=[^\W\d])")


def keyword_texts(code: str) -> list[str]:
    """Code text with a space between a number and the word glued to it.

    `1exec` may be 1 and EXEC, or the float 1e and `xec`: a text holding a
    number followed by an e comes back split both ways.
    """
    texts = [_NUMBER_BEFORE_WORD_RE.sub(r"\g<0> ", code)]
    if _FLOAT_BEFORE_WORD_RE.search(code):
        texts.append(_FLOAT_BEFORE_WORD_RE.sub(r"\g<0> ", code))
    return list(dict.fromkeys(texts))


# Keywords that make text write or run code, and the statement type each one
# means. The regex fallback (_legacy_statement_type) scans the whole text for
# them: T-SQL runs a batch without semicolons ("SELECT 1 DELETE FROM t"), so
# the leading keyword alone does not say what unparsable text does.
_WRITE_KEYWORD_TYPES = {
    "INSERT": "insert",
    "UPDATE": "update",
    "DELETE": "delete",
    "MERGE": "merge",
    "CREATE": "create",
    "ALTER": "alter",
    "DROP": "drop",
    "TRUNCATE": "truncate",
    "EXEC": "execute",
    "EXECUTE": "execute",
    "CALL": "execute",
    "DO": "execute",
    "GRANT": "execute",
    "REVOKE": "execute",
    "DENY": "execute",
}
# MERGE before JOIN or UNION is a T-SQL join or union hint, not a statement.
_MERGE_WORD = r"MERGE(?!\s+(?:JOIN|UNION)(?![\w@#$]))"
_WRITE_KEYWORD_RE = keyword_re(
    _MERGE_WORD if word == "MERGE" else word for word in _WRITE_KEYWORD_TYPES
)
_INTO_RE = keyword_re(["INTO"])
# Square brackets, which the regex fallback drops from every engine's reading:
# in T-SQL they quote names ([delete]) and are already blanked; in Postgres
# (subscripts) and ClickHouse (arrays) what they hold cannot write.
_BRACKETS_RE = re.compile(r"\[[^\]]*\]")
_COMMAND_TYPE_MAP = {
    "CREATE": "create",
    "ALTER": "alter",
    "DROP": "drop",
    "TRUNCATE": "truncate",
    "EXEC": "execute",
    "EXECUTE": "execute",
    "CALL": "execute",
    "DO": "execute",
}

# T-SQL words that start a statement of a batch, which SQL Server runs without
# separating semicolons. All are reserved, so outside quotes they never name
# anything. The session and batch controls write nothing themselves, but a
# SELECT carrying one is not a plain read: USE and SET stay on the pooled
# connection for the next caller, WAITFOR and BEGIN TRAN hold it.
_TSQL_STATEMENT_TYPES = {
    **{word: kind for word, kind in _WRITE_KEYWORD_TYPES.items() if word not in ("CALL", "DO")},
    "WRITETEXT": "update",
    "UPDATETEXT": "update",
    **dict.fromkeys(
        (
            "RECONFIGURE", "USE", "WAITFOR", "DECLARE", "BEGIN", "COMMIT", "ROLLBACK",
            "SAVE", "SETUSER", "REVERT", "OPEN", "CLOSE", "DEALLOCATE", "CHECKPOINT",
            "GOTO", "WHILE", "IF", "RETURN", "BREAK", "CONTINUE", "RAISERROR", "PRINT",
            "KILL", "SHUTDOWN", "BACKUP", "RESTORE", "DBCC", "DUMP", "LOAD",
        ),
        "execute",
    ),
}
_TSQL_STATEMENT_RE = keyword_re(
    [
        *(
            {"MERGE": _MERGE_WORD, "USE": r"USE(?!\s+(?:HINT|PLAN)(?![\w@#$]))"}.get(word, word)
            for word in _TSQL_STATEMENT_TYPES
        ),
        # Statements led by words T-SQL does not reserve, so only in their full form.
        r"(?:ENABLE|DISABLE)\s+TRIGGER",
        r"SEND\s+ON\s+CONVERSATION",
        r"RECEIVE(?=\s*\*|\s+(?:TOP(?![\w@#$])|[\w@#$]++\s*\(|[\w@#$.\[\]\"]++\s*(?:,|FROM(?![\w@#$]))))",
    ]
)
_TSQL_SET_RE = keyword_re(["SET"])
_TSQL_UNRESERVED_LEADS = {"ENABLE": "alter", "DISABLE": "alter", "SEND": "execute", "RECEIVE": "execute"}


def _tsql_statement_types(code: str) -> set[str]:
    """Statement types the T-SQL statement words in T-SQL code text stand for.

    SET also leads every UPDATE's assignments, so only SETs beyond the UPDATEs
    count as statements of their own.
    """
    kinds: set[str] = set()
    for words in keyword_texts(code):
        updates = 0
        for found in _TSQL_STATEMENT_RE.findall(words):
            word = found.split(None, 1)[0].upper()
            if word == "UPDATE":
                updates += 1
            kinds.add(_TSQL_UNRESERVED_LEADS.get(word) or _TSQL_STATEMENT_TYPES[word])
        if len(_TSQL_SET_RE.findall(words)) > updates:
            kinds.add("execute")
    return kinds


def _candidate_dialects(db_type: str | None = None) -> list[str]:
    preferred = _DIALECT_BY_DB_TYPE.get(db_type, db_type)
    candidates = [preferred, "postgres", "tsql", "clickhouse"]
    return list(dict.fromkeys(d for d in candidates if d))


def _parse(sql: str, db_type: str | None = None, *, fallbacks: bool = True) -> list[exp.Expression]:
    if not sql.strip():
        return []

    native = _DIALECT_BY_DB_TYPE.get(db_type, db_type) if db_type else None
    for attempt, dialect in enumerate(_candidate_dialects(db_type)):
        if attempt and not fallbacks:
            break
        # For a known engine, another engine's parser is a fallback only for
        # text both split alike: to a Postgres parser `SELECT $x$ DELETE FROM t
        # SELECT $x$` is one SELECT of a string, to SQL Server three statements.
        if native and dialect != native and not _lexes_alike(sql, native, dialect):
            continue
        try:
            parsed = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
        except (ParseError, TokenError, ValueError):
            continue
        except RecursionError:
            # Deeply nested text (`[a] [a] ...`) exhausts the parser's stack;
            # it is unreadable, and another dialect's parser would only take
            # as long again to fail the same way.
            return []
        return [statement for statement in parsed if statement is not None]
    return []


# ── Lexing ──────────────────────────────────────────────────────────────────
#
# The text checks (statement separators, blocked keywords and functions, the
# regex fallback for statement types) look only at the code between string
# literals, quoted names and comments. A check that splits the text other
# than the way the database does can be steered: a quote the database does
# not see, or a comment it ends sooner, hides the rest of the text from the
# check while the database runs it (`SELECT 1 AS a$b$c; DROP TABLE t` once
# read as a dollar quote). So each engine has its own rules here, and a check
# looks at every way the engine may read the text. Every pass is linear in
# the length of the text, whatever it holds.


@dataclass(frozen=True)
class SqlLexing:
    """How one engine splits SQL into code, literals, quoted names and comments."""

    # Backslash escapes in '...': never (T-SQL), only in E'...' (Postgres),
    # or always (ClickHouse; Postgres with standard_conforming_strings off).
    backslash_strings: Literal["never", "e_prefix", "always"]
    # Backslash escapes inside "..." and `...` names (ClickHouse).
    backslash_identifiers: bool
    # [names] (T-SQL). Elsewhere [ is a subscript or an array.
    bracket_identifiers: bool
    # $tag$...$tag$ strings: Postgres (tag starts with a letter, an unclosed
    # one is an error) or ClickHouse heredocs (tag of [A-Za-z0-9_], an unclosed
    # one is code). T-SQL has none: `$` is part of names and money literals.
    dollar_quotes: Literal["postgres", "clickhouse"] | None
    # The characters that end a -- comment.
    line_comment_ends: str
    # ClickHouse also starts a line comment with "# " or "#!".
    hash_comments: bool
    # Postgres U&"..." names hold \XXXX escapes (or another UESCAPE character).
    unicode_identifiers: bool
    # Backticks quote names in every reading: ClickHouse uses them, and in
    # Postgres (an operator character no operator uses) and T-SQL (no such
    # token) text that has one outside a literal fails to run.


_POSTGRES_LEXING = SqlLexing(
    backslash_strings="e_prefix",
    backslash_identifiers=False,
    bracket_identifiers=False,
    dollar_quotes="postgres",
    line_comment_ends="\n\r",
    hash_comments=False,
    unicode_identifiers=True,
)
# Any session can turn standard_conforming_strings off with set_config(), and a
# pooled connection keeps it once that transaction commits.
_POSTGRES_BACKSLASH_LEXING = replace(_POSTGRES_LEXING, backslash_strings="always")
_TSQL_LEXING = SqlLexing(
    backslash_strings="never",
    backslash_identifiers=False,
    bracket_identifiers=True,
    dollar_quotes=None,
    line_comment_ends="\n\r",
    hash_comments=False,
    unicode_identifiers=False,
)
_CLICKHOUSE_LEXING = SqlLexing(
    backslash_strings="always",
    backslash_identifiers=True,
    bracket_identifiers=False,
    dollar_quotes="clickhouse",
    line_comment_ends="\n",
    hash_comments=True,
    unicode_identifiers=False,
)
_LEXINGS_BY_DIALECT: dict[str, tuple[SqlLexing, ...]] = {
    "postgres": (_POSTGRES_LEXING, _POSTGRES_BACKSLASH_LEXING),
    # Whether a lone CR ends a -- comment is not documented: read it both ways.
    "tsql": (_TSQL_LEXING, replace(_TSQL_LEXING, line_comment_ends="\n")),
    "clickhouse": (_CLICKHOUSE_LEXING,),
}
ALL_SQL_LEXINGS: tuple[SqlLexing, ...] = tuple(
    lexing for lexings in _LEXINGS_BY_DIALECT.values() for lexing in lexings
)


def sql_lexings(db_type: str | None) -> tuple[SqlLexing, ...]:
    """The ways ``db_type`` may read SQL text; every engine's for any other type."""
    dialect = _DIALECT_BY_DB_TYPE.get(db_type or "")
    return _LEXINGS_BY_DIALECT.get(dialect or "", ALL_SQL_LEXINGS)


def _lexings_for(sql: str, lexings: Iterable[SqlLexing], *, statements: bool = False) -> list[SqlLexing]:
    """``lexings`` with rules ``sql`` gives no chance to apply dropped, deduplicated.

    ``statements`` drops the reading with standard_conforming_strings off: this
    service sends Postgres one statement per query (SQLAlchemy's asyncpg
    dialect prepares each one, and the extended protocol takes no more), so
    text that reading would split, or a statement keyword only it sees, never
    runs as a statement of its own. A path that sends Postgres text another
    way must bring the reading back; test_postgres_runs_one_statement_per_query
    fails when one appears.
    """
    changes: dict[str, object] = {}
    if "\\" not in sql:
        changes.update(backslash_strings="never", backslash_identifiers=False)
    if "\r" not in sql:
        changes["line_comment_ends"] = "\n"
    if "#" not in sql:
        changes["hash_comments"] = False
    if "[" not in sql:
        changes["bracket_identifiers"] = False
    if "$" not in sql:
        changes["dollar_quotes"] = None
    if "&" not in sql:
        changes["unicode_identifiers"] = False
    chosen = [
        lexing for lexing in lexings
        if not (statements and lexing == _POSTGRES_BACKSLASH_LEXING)
    ]
    return list(dict.fromkeys(replace(lexing, **changes) if changes else lexing for lexing in chosen))


def code_texts(
    sql: str, db_type: str | None, *, unwrap_identifiers: bool = False, statements: bool = False
) -> list[str]:
    """The code text of ``sql`` under each distinct way ``db_type`` may read it."""
    return [
        _code(sql, lexing, unwrap_identifiers)[0]
        for lexing in _lexings_for(sql, sql_lexings(db_type), statements=statements)
    ]


_POSTGRES_DOLLAR_TAG_RE = re.compile(
    r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$"
)
_CLICKHOUSE_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z0-9_]*\$")
# Every `$tag$` in a text, overlapping ones included: where each tag occurs.
_POSTGRES_DOLLAR_INDEX_RE = re.compile(r"\$(?=([A-Za-z0-9_\u0080-\U0010ffff]*)\$)")
_CLICKHOUSE_DOLLAR_INDEX_RE = re.compile(r"\$(?=([A-Za-z0-9_]*)\$)")
_BLOCK_COMMENT_MARK_RE = re.compile(r"/\*|\*/")
_SPACES_RE = re.compile(r"\s*")
_LINE_END_RE = re.compile(r"[\n\r]")
# ClickHouse reads these as spaces too, Python's \s does not.
_ZERO_WIDTH_SPACES = "᠎​‌‍⁠﻿"
_ZERO_WIDTH_RE = re.compile(f"[{_ZERO_WIDTH_SPACES}]")
_ZERO_WIDTH_TO_SPACE = str.maketrans(dict.fromkeys(_ZERO_WIDTH_SPACES, " "))


@functools.cache
def _span_re(lexing: SqlLexing) -> re.Pattern[str]:
    """One pattern for every literal, quoted name and comment ``lexing`` knows.

    Each alternative runs to the end of its span without backtracking; only a
    nested block comment and a dollar quote are finished in Python.
    """
    line_ends = re.escape(lexing.line_comment_ends)
    line = rf"--[^{line_ends}]*"
    if lexing.hash_comments:
        line += r"|#[ !][^\n]*"
    escaped = r"(?:[^'\\]|\\[\s\S]?|'')*+'?"
    plain = escaped if lexing.backslash_strings == "always" else r"(?:[^']|'')*+'?"
    parts = [
        rf"(?P<line>{line})",
        r"(?P<block>/\*(?:[^/*]|/(?!\*)|\*(?!/))*+\*/)",
        r"(?P<nested>/\*)",
    ]
    if lexing.backslash_strings == "e_prefix":
        parts.append(rf"(?P<estring>(?<![A-Za-z0-9_$\u0080-\U0010ffff])[eE]'{escaped})")
    parts.append(rf"(?P<string>'{plain})")
    for name, open_, close in (("dq", '"', '"'), ("bt", "`", "`")):
        if lexing.backslash_identifiers:
            body = rf"(?:[^{close}\\]|\\[\s\S]?|{close}{close})*+"
        else:
            body = rf"(?:[^{close}]|{close}{close})*+"
        parts.append(rf"(?P<{name}>{open_}{body}(?P<{name}_end>{close})?)")
    if lexing.bracket_identifiers:
        parts.append(r"(?P<br>\[(?:[^\]]|\]\])*+(?P<br_end>\])?)")
    # A `$` after a name character is inside the name; after a digit it may
    # end a number (a token starts there) or sit inside a name: _DollarQuotes.
    if lexing.dollar_quotes == "postgres":
        parts.append(r"(?P<dollar>(?<![A-Za-z_$\u0080-\U0010ffff])\$)")
    elif lexing.dollar_quotes == "clickhouse":
        parts.append(r"(?P<dollar>(?<![A-Za-z_$])\$)")
    return re.compile("|".join(parts))


def _block_comment_end(sql: str, start: int) -> int:
    """Block comments nest in Postgres, T-SQL and ClickHouse alike."""
    depth = 0
    for mark in _BLOCK_COMMENT_MARK_RE.finditer(sql, start):
        depth += 1 if mark.group() == "/*" else -1
        if depth == 0:
            return mark.end()
    return len(sql)


def _name_char(char: str, postgres: bool) -> bool:
    """Postgres names take every non-ASCII character, ClickHouse's none."""
    if char.isascii():
        return char.isalnum() or char in "_$"
    return postgres


def _digits_end_a_number(sql: str, at: int, postgres: bool) -> bool:
    """Whether the ASCII digits before ``at`` end a number (or a Postgres $n
    parameter), after which a `$` starts a token, rather than sit in a name."""
    i = at - 1
    while i >= 0 and "0" <= sql[i] <= "9":
        i -= 1
    if i < 0:
        return True
    if sql[i] == "$":
        return postgres and (i == 0 or not _name_char(sql[i - 1], postgres))
    return not _name_char(sql[i], postgres)


class _DollarQuotes:
    """Finds where a dollar quote closes, in time linear in the whole text.

    A plain search for the closing tag is fastest while quotes close soon, but
    every unclosed tag searches to the end of the text (`$a1$ $a2$ ...` would
    take quadratic time). Once the searches have read four times the text, an
    index of where every tag occurs answers instead.
    """

    def __init__(self, sql: str, lexing: SqlLexing) -> None:
        self.sql = sql
        self.postgres = lexing.dollar_quotes == "postgres"
        self.searched = 0
        self.index: dict[str, list[int]] | None = None

    def _close(self, tag: str, start: int) -> int:
        sql = self.sql
        if self.index is None and self.searched <= 4 * len(sql):
            close = sql.find(tag, start)
            self.searched += (close if close != -1 else len(sql)) - start
            return close
        if self.index is None:
            index_re = _POSTGRES_DOLLAR_INDEX_RE if self.postgres else _CLICKHOUSE_DOLLAR_INDEX_RE
            self.index = {}
            for found in index_re.finditer(sql):
                self.index.setdefault(found.group(1), []).append(found.start())
        positions = self.index.get(tag[1:-1], [])
        k = bisect.bisect_left(positions, start)
        return positions[k] if k < len(positions) else -1

    def end(self, at: int) -> int | None:
        """End of the dollar quote opening at ``at``; None when it is code."""
        sql = self.sql
        if at and "0" <= sql[at - 1] <= "9" and not _digits_end_a_number(sql, at, self.postgres):
            return None
        tag = (_POSTGRES_DOLLAR_TAG_RE if self.postgres else _CLICKHOUSE_DOLLAR_TAG_RE).match(sql, at)
        if tag is None:
            return None
        close = self._close(tag.group(), tag.end())
        if close != -1:
            return close + len(tag.group())
        # Postgres rejects an unclosed dollar quote; ClickHouse reads it as a name.
        return len(sql) if self.postgres else None


SpanKind = Literal["comment", "string", "identifier"]


def _spans(sql: str, lexing: SqlLexing, start: int = 0) -> Iterator[tuple[SpanKind, int, int, int]]:
    """Each literal, quoted name and comment from ``start``: (kind, start, end,
    body end). An unclosed one runs to the end of the text."""
    pattern = _span_re(lexing)
    dollars = _DollarQuotes(sql, lexing)
    position = start
    while (found := pattern.search(sql, position)) is not None:
        at = found.start()
        group = found.lastgroup
        if group == "line" or group == "block":
            end = found.end()
            yield "comment", at, end, end
        elif group == "nested":
            end = _block_comment_end(sql, at)
            yield "comment", at, end, end
        elif group == "dollar":
            end = dollars.end(at)
            if end is None:
                position = at + 1
                continue
            yield "string", at, end, end
        elif group == "string" or group == "estring":
            end = found.end()
            yield "string", at, end, end
        else:
            end = found.end()
            yield "identifier", at, end, end - 1 if found.group(f"{group}_end") else end
        position = end


_IDENTIFIER_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b",
    "f": "\f", "a": "\a", "v": "\v", "e": "\x1b",
}
_HEX = frozenset("0123456789abcdefABCDEF")


def _decode_identifier(body: str, quote: str, backslash_escapes: bool, unicode_escape: str | None) -> str:
    """Unwrap a quoted identifier's body the way the database would read it.

    ``unicode_escape`` is the escape character of a Postgres U&"..." name. An
    escape past U+10FFFF stays as written: Postgres rejects the text anyway.
    """
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == quote and body[i + 1:i + 2] == quote:
            out.append(quote)
            i += 2
            continue
        if unicode_escape and c == unicode_escape and i + 1 < len(body):
            if body[i + 1] == unicode_escape:
                out.append(c)
                i += 2
                continue
            plus = body[i + 1] == "+"
            width = 6 if plus else 4
            digits = body[i + 2:i + 8] if plus else body[i + 1:i + 5]
            if len(digits) == width and _HEX.issuperset(digits) and int(digits, 16) <= 0x10FFFF:
                out.append(chr(int(digits, 16)))
                i += width + (2 if plus else 1)
                continue
        if backslash_escapes and c == "\\" and i + 1 < len(body):  # ClickHouse: \xHH, \n, \<char>
            nxt = body[i + 1]
            hex_digits = body[i + 2:i + 4]
            if nxt in "xX" and len(hex_digits) == 2 and _HEX.issuperset(hex_digits):
                out.append(chr(int(hex_digits, 16)))
                i += 4
                continue
            out.append(_IDENTIFIER_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _skip_blank(sql: str, position: int) -> int:
    """Past the spaces and comments at ``position``."""
    while True:
        position = _SPACES_RE.match(sql, position).end()
        if sql.startswith("--", position):
            line_end = _LINE_END_RE.search(sql, position)
            position = line_end.start() if line_end else len(sql)
        elif sql.startswith("/*", position):
            position = _block_comment_end(sql, position)
        else:
            return position


_UESCAPE_RE = re.compile(r"UESCAPE(?![\w$])", re.IGNORECASE)
_ONE_CHAR_STRING_RE = re.compile(r"'([^'])'")


def _uescape(sql: str, position: int) -> tuple[str, int]:
    """(escape character, end of the UESCAPE clause) of the U& name ending at
    ``position``: Postgres reads `U&"..." UESCAPE '!'` as one name."""
    keyword = _UESCAPE_RE.match(sql, _skip_blank(sql, position))
    if keyword is not None:
        character = _ONE_CHAR_STRING_RE.match(sql, _skip_blank(sql, keyword.end()))
        if character is not None:
            return character.group(1), character.end()
    return "\\", position


@functools.lru_cache(maxsize=16)
def _code(sql: str, lexing: SqlLexing, unwrap_identifiers: bool) -> tuple[str, int]:
    """(code text, index of the first semicolon outside literals, quoted names
    and comments, or -1). See :func:`sql_code_text`."""
    out: list[str] = []
    code_start = 0
    semicolon = -1
    for kind, start, end, body_end in _spans(sql, lexing):
        if start < code_start:  # inside a UESCAPE clause already read
            continue
        chunk_end = start
        if kind == "comment":
            text = " "
        elif kind == "string":
            text = "''"
        elif not unwrap_identifiers:
            text = '""'
        else:
            quote = sql[start]
            unicode_escape = None
            if (
                lexing.unicode_identifiers
                and quote == '"'
                and start - 2 >= code_start
                and sql[start - 2:start].upper() == "U&"
            ):
                unicode_escape, clause_end = _uescape(sql, end)
                end = max(end, clause_end)
                chunk_end = start - 2
            text = _decode_identifier(
                sql[start + 1:body_end],
                "]" if quote == "[" else quote,
                lexing.backslash_identifiers and quote != "[",
                unicode_escape,
            )
        if code_start < start:
            if semicolon < 0:
                found = sql.find(";", code_start, start)
                semicolon = found
            out.append(sql[code_start:chunk_end])
        out.append(text)
        code_start = end
    if code_start < len(sql):
        if semicolon < 0:
            semicolon = sql.find(";", code_start)
        out.append(sql[code_start:])
    code = "".join(out)
    if _ZERO_WIDTH_RE.search(code):
        code = code.translate(_ZERO_WIDTH_TO_SPACE)
    return code, semicolon


def sql_code_text(sql: str, lexing: SqlLexing, *, unwrap_identifiers: bool = False) -> str:
    """The SQL as ``lexing`` reads it, with literals and comments blanked.

    Strings become ``''`` and comments a space. Quoted names become ``""``,
    which keeps a keyword-like name (``[delete]``) out of keyword checks, or
    with ``unwrap_identifiers`` the name as the database reads it, so
    `"pg_read_file"(` still reads as a call. Zero-width spaces, which
    ClickHouse reads as spaces, become spaces.
    """
    return _code(sql, lexing, unwrap_identifiers)[0]


def _lexes_alike(sql: str, native: str, dialect: str) -> bool:
    """Whether the engines of two parser dialects read the same code in ``sql``."""
    lexings = _LEXINGS_BY_DIALECT.get(native, ALL_SQL_LEXINGS) + _LEXINGS_BY_DIALECT.get(
        dialect, ALL_SQL_LEXINGS
    )
    return len({_code(sql, lexing, False)[0] for lexing in _lexings_for(sql, lexings)}) == 1


def has_statement_after_semicolon(sql: str, db_type: str | None = "postgres") -> bool:
    """Whether a semicolon outside literals, quoted names and comments has
    anything after it, in any way ``db_type`` may split statements (see
    _lexings_for). One trailing semicolon (with only spaces after it) is allowed."""
    if ";" not in sql:
        return False
    for lexing in _lexings_for(sql, sql_lexings(db_type), statements=True):
        semicolon = _code(sql, lexing, False)[1]
        if semicolon >= 0 and sql[semicolon + 1:].strip():
            return True
    return False


def _closing_paren(sql: str, start: int) -> int:
    """Index of the parenthesis closing the one at ``start``, outside literals,
    names and comments (Postgres reading), or the end of the text."""
    depth = 0
    position = start
    spans = _spans(sql, _POSTGRES_LEXING, start)
    while True:
        span = next(spans, None)
        stop = span[1] if span else len(sql)
        for paren in re.finditer(r"[()]", sql[position:stop]):
            depth += 1 if paren.group() == "(" else -1
            if depth == 0:
                return position + paren.start()
        if span is None:
            return len(sql)
        position = span[2]


_EXPLAIN_RE = re.compile(r"^\s*EXPLAIN(?![\w$])", re.IGNORECASE)
_ANALYZE_WORD_RE = re.compile(r"ANALY[SZ]E(?![\w$])", re.IGNORECASE)
_VERBOSE_WORD_RE = re.compile(r"VERBOSE(?![\w$])", re.IGNORECASE)
_ANALYZE_OPTION_RE = keyword_re([r"ANALY[SZ]E"])


def _split_explain(sql: str) -> tuple[bool, str] | None:
    """(whether the plan runs the statement, the statement) for an EXPLAIN.

    Postgres runs it under ANALYZE (or ANALYSE), bare or among the
    parenthesized options, where a quoted name ("analyze") also counts.
    """
    match = _EXPLAIN_RE.match(sql)
    if not match:
        return None
    position = _skip_blank(sql, match.end())
    analyze = False
    if sql.startswith("(", position):
        close = _closing_paren(sql, position)
        options = sql_code_text(sql[position + 1:close], _POSTGRES_LEXING, unwrap_identifiers=True)
        analyze = _ANALYZE_OPTION_RE.search(options) is not None
        position = _skip_blank(sql, close + 1)
    else:
        word = _ANALYZE_WORD_RE.match(sql, position)
        if word is not None:
            analyze = True
            position = _skip_blank(sql, word.end())
        word = _VERBOSE_WORD_RE.match(sql, position)
        if word is not None:
            position = _skip_blank(sql, word.end())
    return analyze, sql[position:]


def _legacy_statement_type(sql: str, db_type: str | None) -> str:
    """Classify text the parser could not read, erring towards the stricter type.

    Text that reads but also carries a write keyword, or that mixes write
    types, is ``execute`` — the type no select-only or single-flag grant
    allows. Only a CTE leading into one kind of DML keeps that DML's type, and
    a SELECT ... INTO creates a table. Keywords count under every way
    ``db_type`` may read the text (every engine's, for a caller that does not
    say which engine it is for).
    """
    match = _LEGACY_SQL_TYPE_RE.match(sql)
    if not match:
        return "unknown"
    keyword = match.group(1).upper()
    writes: set[str] = set()
    into = False
    for code in code_texts(sql, db_type):
        for words in keyword_texts(_BRACKETS_RE.sub("", code)):
            writes.update(
                _WRITE_KEYWORD_TYPES[found.upper()] for found in _WRITE_KEYWORD_RE.findall(words)
            )
            into = into or _INTO_RE.search(words) is not None
    if _DIALECT_BY_DB_TYPE.get(db_type or "", "tsql") == "tsql":
        for code in code_texts(sql, "mssql"):
            writes |= _tsql_statement_types(code)
    if into and keyword in ("SELECT", "EXPLAIN", "WITH") and not writes & {"insert", "merge"}:
        writes.add("create")
    if keyword in ("SELECT", "EXPLAIN"):
        return "execute" if writes else keyword.lower()
    if keyword == "WITH":
        if not writes:
            return "select"
        if len(writes) == 1 and writes <= _DML_TYPES:
            return writes.pop()
        return "execute"
    lead = _WRITE_KEYWORD_TYPES[keyword]
    return lead if writes <= {lead} else "execute"


def _reads_only(node: exp.Expression) -> bool:
    """Whether a query is SELECTs joined by UNION/INTERSECT/EXCEPT."""
    pending = [node]
    while pending:
        node = pending.pop()
        if isinstance(node, exp.SetOperation):
            pending += (node.this, node.expression)
        elif isinstance(node, exp.Subquery):
            pending.append(node.this)
        elif not isinstance(node, exp.Select):
            return False
    return True


def _statement_type_from_expression(statement: exp.Expression) -> str:
    if isinstance(statement, (exp.Select, exp.SetOperation)):
        for child_type, name in (
            (exp.Insert, "insert"),
            (exp.Update, "update"),
            (exp.Delete, "delete"),
            (exp.Merge, "merge"),
        ):
            if statement.find(child_type) is not None:
                return name
        # SELECT ... INTO creates the target table (a #temp one too).
        if statement.find(exp.Into) is not None:
            return "create"
        return "select" if _reads_only(statement) else "unknown"
    if isinstance(statement, exp.Insert):
        return "insert"
    if isinstance(statement, exp.Update):
        return "update"
    if isinstance(statement, exp.Delete):
        return "delete"
    if isinstance(statement, exp.Merge):
        return "merge"
    if isinstance(statement, exp.Create):
        return "create"
    if isinstance(statement, exp.Alter):
        return "alter"
    if isinstance(statement, exp.Drop):
        return "drop"
    if isinstance(statement, exp.TruncateTable):
        return "truncate"
    if isinstance(statement, exp.Execute):
        return "execute"
    if isinstance(statement, exp.Command):
        command = str(statement.this).upper()
        return _COMMAND_TYPE_MAP.get(command, "unknown")
    return "unknown"


def statement_type(sql: str, db_type: str | None = None) -> str:
    explain = _split_explain(sql)
    if explain is not None:
        analyze, inner_sql = explain
        inner_type = statement_type(inner_sql, db_type)
        if analyze and inner_type in _MUTATING_TYPES:
            return inner_type
        return "explain" if inner_type != "unknown" else "unknown"

    parsed = _parsed(sql, db_type)
    if (
        parsed.statements != 1
        or parsed.kind == "unknown"
        or _tsql_batch_hides_writes(sql, db_type, parsed.kind)
    ):
        return _legacy_statement_type(sql, db_type)
    return parsed.kind


def strict_statement_type(sql: str, db_type: str | None = None) -> str | None:
    """Type of ``sql`` when it parses as exactly one statement, else None.

    Unlike :func:`statement_type` there is no regex fallback: SQL the parser
    cannot read, or text too complex to give it, is never classified, not
    even by a strict guess.
    """
    parsed = _parsed(sql, db_type)
    if (
        parsed.statements != 1
        or parsed.kind == "unknown"
        or _tsql_batch_hides_writes(sql, db_type, parsed.kind)
    ):
        return None
    return parsed.kind


def _tsql_batch_hides_writes(sql: str, db_type: str | None, parsed_type: str) -> bool:
    """Whether SQL Server would run a statement the parsed one does not show.

    T-SQL runs a batch without semicolons, and the parser takes a reserved
    word for an alias: `SELECT 1 DELETE FROM t` parses as one SELECT (of 1 AS
    DELETE), while SQL Server runs the SELECT and then the DELETE.
    """
    if _DIALECT_BY_DB_TYPE.get(db_type or "") != "tsql":
        return False
    kinds: set[str] = set()
    for code in code_texts(sql, db_type):
        kinds |= _tsql_statement_types(code)
    shown = {parsed_type} | ({"insert", "update", "delete"} if parsed_type == "merge" else set())
    return bool(kinds - shown)


def _cte_aliases(statement: exp.Expression) -> set[str]:
    aliases: set[str] = set()
    for cte in statement.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            aliases.add(alias.lower())
    return aliases


# ClickHouse table functions that read tables the caller names in arguments,
# or picks by a regex (merge). They count as the pseudo-table "name()", which
# no allow-list holds, so a caller limited to some tables cannot use them.
_TABLE_READING_FUNCTIONS = frozenset(
    name.lower()
    for name in (
        "merge", "loop", "mergeTreeIndex", "mergeTreeProjection", "mergeTreeAnalyzeIndexes",
        "mergeTreeAnalyzeIndexesUUID", "mergeTreeTextIndex", "timeSeriesData",
        "timeSeriesMetrics", "timeSeriesTags", "timeSeriesSelector", "prometheusQuery",
        "prometheusQueryRange",
    )
)
_JOIN_GET_FUNCTIONS = frozenset({"joinget", "joingetornull"})


def _tables_read(statement: exp.Expression) -> set[str]:
    tables: set[str] = set()
    cte_aliases = _cte_aliases(statement)
    for table in statement.find_all(exp.Table):
        name = table.name
        if not name:
            function = table.this
            if isinstance(function, exp.Anonymous) and function.name.lower() in _TABLE_READING_FUNCTIONS:
                tables.add(f"{function.name.lower()}()")
            continue
        normalized = name.lower()
        if normalized in cte_aliases:
            continue
        tables.add(normalized)
    for function in statement.find_all(exp.Anonymous):
        if function.name.lower() not in _JOIN_GET_FUNCTIONS:
            continue
        first = function.expressions[0] if function.expressions else None
        if isinstance(first, exp.Literal) and first.is_string:
            tables.add(first.this.rsplit(".", 1)[-1].strip("`\"").lower())
        else:
            tables.add(f"{function.name.lower()}()")
    for membership in statement.find_all(exp.In):
        field = membership.args.get("field")
        if isinstance(field, exp.Column) and field.name.lower() not in cte_aliases:
            tables.add(field.name.lower())
    return tables


def _blocked_statement(statement: exp.Expression) -> str | None:
    if isinstance(statement, exp.Merge):
        return "MERGE"
    if isinstance(statement, exp.Execute):
        return "EXEC"
    if isinstance(statement, exp.Command):
        command = str(statement.this).upper()
        if command in {"EXEC", "EXECUTE", "CALL", "DO"}:
            return command
    if isinstance(statement, exp.Grant):
        return "GRANT"
    if isinstance(statement, exp.Revoke):
        return "REVOKE"
    return None


# Text the parser is not given. sqlglot reads about 3µs a token, so past this
# many tokens of code one parse outlasts the whole query check; a chain of
# subscripts (`a[1][1]...`, `[a] [a] ...` in Postgres and ClickHouse) takes it
# quadratic time. Such text is read like text the parser rejects: the regex
# fallback types it, the strict type is None, tables come from the regex and
# blocked statements from their keywords. Real queries stay far below both:
# a 2,000-row INSERT ... VALUES is 32,000 tokens.
_MAX_PARSE_TOKENS = 50_000
# Past this many, only the first parser is tried, so failing parsers cannot
# add up: three of them over 50,000 tokens would take about a second.
_MAX_FALLBACK_PARSE_TOKENS = 12_500
_MAX_SUBSCRIPT_CHAIN = 32
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_BRACKET_RE = re.compile(r"[\[\]]")
_CHAIN_GAP_RE = re.compile(r"\s*(?:\.\s*[\w$\"]+\s*)*")


def _longest_subscript_chain(code: str) -> int:
    """The most bracket groups that follow one another (`][`, `].b[`) at one depth."""
    longest = 0
    depth = 0
    runs: dict[int, tuple[int, int]] = {}  # depth -> (chain length, end of its last group)
    for bracket in _BRACKET_RE.finditer(code):
        if bracket.group() == "[":
            run, last_end = runs.get(depth, (0, -1))
            chained = last_end >= 0 and _CHAIN_GAP_RE.fullmatch(code, last_end, bracket.start())
            run = run + 1 if chained else 1
            runs[depth] = (run, -1)
            longest = max(longest, run)
            depth += 1
        elif depth:
            runs.pop(depth, None)
            depth -= 1
            run, _ = runs.get(depth, (1, -1))
            runs[depth] = (run, bracket.end())
    return longest


def _parse_tokens(sql: str, db_type: str | None) -> int | None:
    """Tokens of code the parser would read, or None when ``sql`` is too
    complex to give it."""
    most = 0
    for code in code_texts(sql, db_type):
        tokens = _TOKEN_RE.subn("", code)[1]
        if tokens > _MAX_PARSE_TOKENS:
            return None
        if "[" in code and _longest_subscript_chain(code) > _MAX_SUBSCRIPT_CHAIN:
            return None
        most = max(most, tokens)
    return most


class _Parsed(NamedTuple):
    statements: int  # how many statements the parser read (0: none)
    kind: str  # the type of the one statement, "unknown" otherwise
    tables: frozenset[str]
    blocked: str | None  # the first statement every caller is refused
    skipped: bool  # too complex to give to the parser


@functools.lru_cache(maxsize=16)
def _parsed(sql: str, db_type: str | None) -> _Parsed:
    """What the parser makes of ``sql``, read once for all the checks of a query."""
    tokens = _parse_tokens(sql, db_type)
    skipped = tokens is None
    statements = (
        [] if tokens is None
        else _parse(sql, db_type, fallbacks=tokens <= _MAX_FALLBACK_PARSE_TOKENS)
    )
    tables: set[str] = set()
    blocked = None
    for statement in statements:
        tables |= _tables_read(statement)
        blocked = blocked or _blocked_statement(statement)
    kind = _statement_type_from_expression(statements[0]) if len(statements) == 1 else "unknown"
    return _Parsed(len(statements), kind, frozenset(tables), blocked, skipped)


def table_names(sql: str, db_type: str | None = None) -> set[str]:
    """Tables the parsed statement reads, lower-cased bare names.

    Besides FROM/JOIN tables: ClickHouse `x IN table`, joinGet('table', ...)
    and the table functions above (as "name()").
    """
    explain = _split_explain(sql)
    if explain is not None:
        sql = explain[1]
    return set(_parsed(sql, db_type).tables)


_BLOCKED_STATEMENT_RE = keyword_re(
    ["EXEC", "EXECUTE", "CALL", "DO", _MERGE_WORD, "GRANT", "REVOKE"]
)


def blocked_ast_keyword(sql: str, db_type: str | None = None) -> str | None:
    explain = _split_explain(sql)
    if explain is not None:
        analyze, inner_sql = explain
        if analyze and statement_type(inner_sql, db_type) in _MUTATING_TYPES:
            return "EXPLAIN ANALYZE"
        return None

    parsed = _parsed(sql, db_type)
    if not parsed.skipped:
        return parsed.blocked
    # Too complex to parse: the statements' keywords stand in for them.
    for code in code_texts(sql, db_type):
        for words in keyword_texts(code):
            found = _BLOCKED_STATEMENT_RE.search(words)
            if found:
                return found.group(0).upper()
    return None
