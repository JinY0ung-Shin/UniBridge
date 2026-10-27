"""SQL keyword blacklist validation.

Detects dangerous SQL keywords (GRANT, REVOKE, SHUTDOWN, etc.)
while ignoring keywords inside string literals and comments, and
calls to functions that reach outside the database.
"""
from __future__ import annotations

import re

from app.services.sql_analysis import _strip_strings_and_comments, blocked_ast_keyword

# Default blocked keywords
_SINGLE_KEYWORDS = [
    "GRANT", "REVOKE", "SHUTDOWN", "KILL", "BACKUP", "RESTORE",
    "TRUNCATE", "DBCC",
]

_MULTI_KEYWORDS = [
    "CREATE USER", "DROP USER", "ALTER USER",
    "CREATE LOGIN", "DROP LOGIN", "ALTER LOGIN",
    "DROP TABLE", "DROP DATABASE",
    "BULK INSERT", "OPENROWSET",
    "xp_cmdshell", "sp_configure", "sp_addrolemember", "sp_droprolemember",
]

_SINGLE_RE = re.compile(
    r"\b(" + "|".join(_SINGLE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

_MULTI_RE = re.compile(
    r"\b(" + "|".join(kw.replace(" ", r"\s+") for kw in _MULTI_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Functions that let a statement the parser accepts as a plain read reach
# outside the database: fetch URLs or object storage, connect to other servers,
# touch files on the database host, or run SQL held in a string, which neither
# the keyword checks nor the table-access check can see. They are matched only
# as calls (`name(`), so a column or table called `url`, `file` or `cluster`
# stays usable. Entries are regex fragments, matched case-insensitively.
_BLOCKED_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "clickhouse": (
        "url", "urlCluster", "s3", "s3Cluster", "gcs", r"azureBlobStorage\w*",
        "hdfs", "hdfsCluster", "file", "fileCluster",
        "remote", "remoteSecure", "cluster", "clusterAllReplicas",
        "mysql", "postgresql", "mongodb", "redis", "sqlite", "jdbc", "odbc",
        "executable", r"iceberg\w*", r"deltaLake\w*", r"hudi\w*",
    ),
    "postgres": (
        "pg_read_file", "pg_read_binary_file", "pg_stat_file", r"pg_ls_\w+",
        "lo_import", "lo_export", r"dblink\w*",
        r"(?:query|table|schema|database|cursor)_to_xml\w*",
    ),
    "mssql": (
        "OPENQUERY", "OPENDATASOURCE", "OPENROWSET",
        "fn_xe_file_target_read_file", "fn_get_audit_file", "fn_trace_gettable",
    ),
}
_BLOCKED_FUNCTION_RES = {
    db_type: re.compile(r"\b(" + "|".join(names) + r")\s*\(", re.IGNORECASE)
    for db_type, names in _BLOCKED_FUNCTIONS.items()
}
# ClickHouse's names are ordinary words, so `INSERT INTO url (id)` or
# `CREATE TABLE file (...)` name a table rather than call a function; writing
# to a table function needs `INSERT INTO FUNCTION url(...)`, which this does
# not match.
_TABLE_NAME_BEFORE_RE = re.compile(
    r"\b(?:INTO|TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+(?:[\w$]+\s*\.\s*)*$",
    re.IGNORECASE,
)
_IDENTIFIER_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b",
    "f": "\f", "a": "\a", "v": "\v", "e": "\x1b",
}


def _decode_identifier(body: str, quote: str, backslash_escapes: bool, unicode_escapes: bool) -> str:
    """Unwrap a quoted identifier's body the way the database would read it."""
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == quote and body[i + 1:i + 2] == quote:
            out.append(quote)
            i += 2
            continue
        if c == "\\" and i + 1 < len(body):
            if unicode_escapes:  # Postgres U&"..." : \XXXX or \+XXXXXX
                plus = body[i + 1] == "+"
                digits = body[i + 2:i + 8] if plus else body[i + 1:i + 5]
                width = 6 if plus else 4
                if len(digits) == width and all(ch in "0123456789abcdefABCDEF" for ch in digits):
                    out.append(chr(int(digits, 16)))
                    i += width + (2 if plus else 1)
                    continue
            if backslash_escapes:  # ClickHouse: C-style escapes, \xHH, \<char>
                nxt = body[i + 1]
                hex_digits = body[i + 2:i + 4]
                if nxt in "xX" and len(hex_digits) == 2 and all(
                    ch in "0123456789abcdefABCDEF" for ch in hex_digits
                ):
                    out.append(chr(int(hex_digits, 16)))
                    i += 4
                    continue
                out.append(_IDENTIFIER_ESCAPES.get(nxt, nxt))
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _code_text(sql: str, *, backslash_escapes: bool, bracket_identifiers: bool) -> str:
    """Return the SQL with literals and comments removed and quoted identifiers
    unwrapped, so `"pg_read_file"(` or `` `url`( `` still reads as a call.

    Whether a backslash escapes the next character inside a string depends on
    the engine (ClickHouse and Postgres E'' strings: yes; standard SQL: no), and
    getting it wrong can shift where a string ends and hide a call inside a
    fake literal, so callers check both readings. Square brackets quote
    identifiers only in T-SQL.
    """
    out: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            if end == -1:
                break
            out.append("\n")
            i = end + 1
            continue
        if c == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                break
            out.append(" ")
            i = end + 2
            continue
        if c == "'":
            i += 1
            while i < length:
                if backslash_escapes and sql[i] == "\\":
                    i += 2
                elif sql[i] == "'":
                    if sql.startswith("''", i):
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            out.append("''")
            continue
        if c in "\"`" or (c == "[" and bracket_identifiers):
            close = "]" if c == "[" else c
            unicode_escapes = c == '"' and "".join(out[-2:]).upper() == "U&"
            start = i + 1
            i = start
            while i < length:
                if backslash_escapes and c != "[" and sql[i] == "\\":
                    i += 2
                elif sql[i] == close:
                    if sql.startswith(close * 2, i):
                        i += 2
                    else:
                        break
                else:
                    i += 1
            body = sql[start:i]
            i += 1  # past the closing quote (or the end of an unterminated one)
            if unicode_escapes:
                del out[-2:]
            out.append(
                _decode_identifier(
                    body, close, backslash_escapes and c != "[", unicode_escapes
                )
            )
            continue
        # A dollar quote ($$ or $tag$) cannot start inside an identifier: T-SQL
        # and Postgres allow `$` in names, so `a$b$c` is one identifier, not
        # an opening $b$ that would swallow the rest of the statement. A tag
        # starting with a digit is a positional parameter ($1).
        if c == "$" and not (i and (sql[i - 1].isalnum() or sql[i - 1] in "_$")):
            tag_end = sql.find("$", i + 1)
            if tag_end != -1:
                tag = sql[i:tag_end + 1]
                tag_body = tag[1:-1]
                if all(ch.isalnum() or ch == "_" for ch in tag_body) and not tag_body[:1].isdigit():
                    close_at = sql.find(tag, tag_end + 1)
                    if close_at == -1:
                        break
                    i = close_at + len(tag)
                    out.append("''")
                    continue
        out.append(c)
        i += 1
    return "".join(out)


def blocked_function(sql: str, db_type: str | None = None) -> str | None:
    """Name the first call to an outside-reaching function for this engine.

    ``db_type=None`` (an engine the caller did not name) checks every engine's
    list.
    """
    if db_type is None:
        engines = list(_BLOCKED_FUNCTION_RES)
    elif db_type in _BLOCKED_FUNCTION_RES:
        engines = [db_type]
    else:
        return None
    bracket_modes = (False, True) if db_type is None else (db_type == "mssql",)
    for backslash_escapes in (False, True):
        for bracket_identifiers in bracket_modes:
            text = _code_text(
                sql,
                backslash_escapes=backslash_escapes,
                bracket_identifiers=bracket_identifiers,
            )
            for engine in engines:
                for match in _BLOCKED_FUNCTION_RES[engine].finditer(text):
                    if engine == "clickhouse" and _TABLE_NAME_BEFORE_RE.search(
                        text[max(0, match.start() - 200):match.start()]
                    ):
                        continue
                    return match.group(1)
    return None


def validate_sql(
    sql: str,
    extra_blocked: list[str] | None = None,
    db_type: str | None = None,
) -> str | None:
    """Check SQL for blocked keywords and outside-reaching function calls.

    Returns an error message string if one is found, or None if the SQL is
    clean. ``db_type`` picks which engine's function list applies; ``None``
    applies all of them.
    """
    cleaned = _strip_strings_and_comments(sql)

    match = _MULTI_RE.search(cleaned)
    if match:
        return f"Blocked SQL keyword: {match.group(0).upper()}"

    match = _SINGLE_RE.search(cleaned)
    if match:
        return f"Blocked SQL keyword: {match.group(0).upper()}"

    if extra_blocked:
        extra_pattern = re.compile(
            r"\b(" + "|".join(re.escape(kw) for kw in extra_blocked) + r")\b",
            re.IGNORECASE,
        )
        match = extra_pattern.search(cleaned)
        if match:
            return f"Blocked SQL keyword: {match.group(0).upper()}"

    function = blocked_function(sql, db_type)
    if function:
        return (
            f"Blocked SQL function: {function}() reaches outside the database "
            "(URLs, object storage, other servers or server files)"
        )

    ast_blocked = blocked_ast_keyword(sql)
    if ast_blocked:
        return f"Blocked SQL keyword: {ast_blocked.upper()}"

    return None
