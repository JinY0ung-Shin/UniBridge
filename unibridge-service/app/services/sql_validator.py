"""SQL keyword blacklist validation.

Detects dangerous SQL keywords (GRANT, REVOKE, SHUTDOWN, etc.)
while ignoring keywords inside string literals and comments, and
calls to functions that reach outside the database.
"""
from __future__ import annotations

import re

from app.services.sql_analysis import blocked_ast_keyword, code_texts, keyword_re, keyword_texts

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

_SINGLE_RE = keyword_re(_SINGLE_KEYWORDS)
_SQL_ENGINES = ("postgres", "mssql", "clickhouse")
_MULTI_RE = keyword_re(kw.replace(" ", r"\s+") for kw in _MULTI_KEYWORDS)

# Functions that let a statement the parser accepts as a plain read reach
# outside the database: fetch URLs or object storage, connect to other servers,
# touch files on the database host, or run SQL held in a string, which neither
# the keyword checks nor the table-access check can see. They are matched only
# as calls (`name(`), so a column or table called `url`, `file` or `cluster`
# stays usable. Entries are regex fragments, matched case-insensitively.
# ClickHouse's cover the 26.3 table functions that read outside its own
# tables (system.table_functions), the table engines and dictionary sources
# that connect out, and prometheusQuery*, which reads a TimeSeries table by name.
_BLOCKED_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "clickhouse": (
        "url", "urlCluster", "s3", "s3Cluster", "gcs", r"azureBlobStorage\w*",
        "hdfs", "hdfsCluster", "file", "fileCluster",
        "remote", "remoteSecure", "cluster", "clusterAllReplicas",
        "mysql", "postgresql", "mongodb", "redis", "sqlite", "jdbc", "odbc",
        "executable", r"iceberg\w*", r"deltaLake\w*", r"hudi\w*",
        "oss", "cosn", "arrowFlight", r"paimon\w*", "hive", "ytsaurus",
        r"prometheusQuery\w*",
        # table engines and dictionary sources
        "kafka", "rabbitmq", "nats", "filelog", "s3Queue", "azureQueue",
        "executablePool", "executable_pool", "distributed", "materializedPostgreSQL",
        "http", "cassandra", "clickhouse",
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
    db_type: re.compile(r"(?<![\w@#$])(" + "|".join(names) + r")\s*\(", re.IGNORECASE)
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
    for code in code_texts(sql, db_type, unwrap_identifiers=True):
        for text in keyword_texts(code):
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
    clean. ``db_type`` picks which engine's function list and quoting rules
    apply; ``None`` (or a non-SQL engine, for the keywords) applies all of them.
    """
    keyword_patterns = [_MULTI_RE, _SINGLE_RE]
    if extra_blocked:
        keyword_patterns.append(keyword_re(re.escape(kw) for kw in extra_blocked))
    for code in code_texts(sql, db_type, statements=True):
        for words in keyword_texts(code):
            for pattern in keyword_patterns:
                match = pattern.search(words)
                if match:
                    return f"Blocked SQL keyword: {match.group(0).upper()}"

    function = blocked_function(sql, db_type)
    if function:
        return (
            f"Blocked SQL function: {function}() reaches outside the database "
            "(URLs, object storage, other servers or server files)"
        )

    ast_blocked = blocked_ast_keyword(sql, db_type if db_type in _SQL_ENGINES else None)
    if ast_blocked:
        return f"Blocked SQL keyword: {ast_blocked.upper()}"

    return None
