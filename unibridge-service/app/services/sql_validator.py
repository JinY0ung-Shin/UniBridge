"""SQL keyword blacklist validation.

Detects dangerous SQL keywords (GRANT, REVOKE, SHUTDOWN, etc.)
while ignoring keywords inside string literals and comments.
"""
from __future__ import annotations

import re

from app.services.query_executor import _strip_strings_and_comments

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


def validate_sql(
    sql: str,
    extra_blocked: list[str] | None = None,
) -> str | None:
    """Check SQL for blocked keywords.

    Returns an error message string if a blocked keyword is found,
    or None if the SQL is clean.
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

    return None
