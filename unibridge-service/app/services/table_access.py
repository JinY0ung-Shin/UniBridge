"""Table-level access control.

Extracts table names from SQL and validates them against a whitelist.
"""
from __future__ import annotations

import re

from app.services.sql_analysis import code_texts, table_names

# Matches table references after FROM, JOIN, INTO, UPDATE keywords.
# Handles optional schema prefix: schema.table, [schema].[table]
_TABLE_RE = re.compile(
    r"""
    \b(?:FROM|JOIN|INTO|UPDATE)\s+      # keyword
    (?:\[?\w+\]?\.)?\s*                  # optional schema prefix
    (\[?\w+\]?)                          # table name (capture group 1)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Matches comma-separated tables in FROM clause
_COMMA_TABLE_RE = re.compile(
    r"""
    \bFROM\s+
    (
        (?:\[?\w+\]?\.)?\[?\w+\]?
        (?:\s*,\s*(?:\[?\w+\]?\.)?\[?\w+\]?)*
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_INDIVIDUAL_TABLE_RE = re.compile(
    r"(?:\[?\w+\]?\.)?\s*(\[?\w+\]?)",
    re.IGNORECASE,
)


# `TABLE name` (Postgres' short form of SELECT * FROM name, which the parser
# does not read inside UNION or IN) and DDL. Quoted names are blanked in the
# code text, so one counts as the pseudo-table "table()".
_TABLE_COMMAND_RE = re.compile(
    r"""(?<![\w@#$])TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?:ONLY\s+)?"""
    r"""(?:(?:[\w$]+|"")\s*\.\s*)*([\w$]+|"")""",
    re.IGNORECASE,
)


def _clean_name(name: str) -> str:
    """Remove brackets and lowercase a table name."""
    return name.strip("[]").lower()


def extract_tables(sql: str, db_type: str = "postgres") -> set[str]:
    """Extract table names referenced in a SQL statement, lower-cased.

    Reads them from the parsed statement (FROM/JOIN tables, ClickHouse
    `x IN table` and joinGet(), and table functions such as merge() as the
    pseudo-table "merge()", which no allow-list holds). When the parser finds
    none, a regex over the raw text takes over, which also counts names inside
    literals and comments. Names after TABLE count either way. A table read
    another way (a view, a dictionary, a function that queries it) is not seen.
    """
    tables = table_names(sql, db_type=db_type) or _extract_tables_with_regex(sql)
    for code in code_texts(sql, db_type):
        for match in _TABLE_COMMAND_RE.finditer(code):
            name = match.group(1)
            tables.add("table()" if name == '""' else name.lower())
    return tables


def _extract_tables_with_regex(sql: str) -> set[str]:
    tables: set[str] = set()
    for match in _TABLE_RE.finditer(sql):
        tables.add(_clean_name(match.group(1)))

    for match in _COMMA_TABLE_RE.finditer(sql):
        table_list = match.group(1)
        for part in table_list.split(","):
            part = part.strip()
            if part:
                inner = _INDIVIDUAL_TABLE_RE.match(part)
                if inner:
                    tables.add(_clean_name(inner.group(1)))
    return tables


def check_table_access(
    referenced_tables: set[str],
    allowed_tables: list[str] | None,
) -> str | None:
    """Check if all referenced tables are in the whitelist.

    Returns error message if access denied, or None if allowed.
    """
    if allowed_tables is None:
        return None

    allowed_set = {t.lower() for t in allowed_tables}
    denied = referenced_tables - allowed_set
    if denied:
        denied_list = ", ".join(sorted(denied))
        return f"Access denied to table(s): {denied_list}"
    return None


def table_name_allowed(table: str, allowed_tables: list[str] | None) -> bool:
    """Whether a catalog table name (``schema.table`` or bare) is in scope.

    Query checks compare bare lower-case names (:func:`extract_tables`), so a
    qualified name passes when either it or its bare name is listed.
    """
    if allowed_tables is None:
        return True
    allowed = {name.lower() for name in allowed_tables}
    lowered = table.lower()
    return lowered in allowed or lowered.rpartition(".")[2] in allowed
