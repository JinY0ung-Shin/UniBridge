"""Table-level access control.

Extracts table names from SQL and validates them against a whitelist.
"""
from __future__ import annotations

import re

from app.services.query_executor import _strip_strings_and_comments

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


def _clean_name(name: str) -> str:
    """Remove brackets and lowercase a table name."""
    return name.strip("[]").lower()


def extract_tables(sql: str) -> set[str]:
    """Extract table names referenced in a SQL statement.

    Strips string literals and comments first to avoid false positives.
    Returns lowercase table names.
    """
    cleaned = _strip_strings_and_comments(sql)
    tables: set[str] = set()

    for match in _TABLE_RE.finditer(cleaned):
        tables.add(_clean_name(match.group(1)))

    for match in _COMMA_TABLE_RE.finditer(cleaned):
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
