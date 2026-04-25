"""AST-backed SQL analysis helpers used by query authorization."""
from __future__ import annotations

import re
from collections.abc import Iterable

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.errors import ParseError

try:
    from sqlglot.errors import TokenError
except ImportError:  # pragma: no cover - compatibility with older sqlglot
    TokenError = ParseError


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

_EXPLAIN_RE = re.compile(r"^\s*EXPLAIN\b", re.IGNORECASE)
_LEGACY_SQL_TYPE_RE = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH|EXPLAIN|CREATE|ALTER|DROP|TRUNCATE|EXEC|EXECUTE|CALL|DO|MERGE)\b",
    re.IGNORECASE,
)
_DML_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)
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


def _candidate_dialects(db_type: str = "postgres") -> list[str]:
    preferred = _DIALECT_BY_DB_TYPE.get(db_type, db_type)
    candidates = [preferred, "postgres", "tsql", "clickhouse"]
    return list(dict.fromkeys(d for d in candidates if d))


def _parse(sql: str, db_type: str = "postgres") -> list[exp.Expression]:
    if not sql.strip():
        return []

    for dialect in _candidate_dialects(db_type):
        try:
            parsed = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
        except (ParseError, TokenError, ValueError):
            continue
        return [statement for statement in parsed if statement is not None]
    return []


def _split_explain(sql: str) -> tuple[bool, str] | None:
    match = _EXPLAIN_RE.match(sql)
    if not match:
        return None

    rest = sql[match.end():].lstrip()
    analyze = False

    if rest.startswith("("):
        depth = 0
        for index, char in enumerate(rest):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    rest = rest[index + 1:].lstrip()
                    break

    if rest.upper().startswith("ANALYZE "):
        analyze = True
        rest = rest[len("ANALYZE "):].lstrip()

    return analyze, rest


def _strip_strings_and_comments(sql: str) -> str:
    result: list[str] = []
    i = 0
    length = len(sql)
    while i < length:
        c = sql[i]
        if c == '-' and i + 1 < length and sql[i + 1] == '-':
            i = sql.find('\n', i)
            if i == -1:
                break
            i += 1
            continue
        if c == '/' and i + 1 < length and sql[i + 1] == '*':
            end = sql.find('*/', i + 2)
            if end == -1:
                break
            i = end + 2
            continue
        if c == "'":
            i += 1
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            result.append("''")
            continue
        if c == '"':
            i += 1
            while i < length:
                if sql[i] == '"':
                    if i + 1 < length and sql[i + 1] == '"':
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            result.append('""')
            continue
        if c == '$':
            tag_end = sql.find('$', i + 1)
            if tag_end != -1:
                tag = sql[i:tag_end + 1]
                tag_body = tag[1:-1]
                if all(ch.isalnum() or ch == '_' for ch in tag_body):
                    close = sql.find(tag, tag_end + 1)
                    if close == -1:
                        break
                    i = close + len(tag)
                    result.append("''")
                    continue
            result.append(c)
            i += 1
            continue
        result.append(c)
        i += 1
    return "".join(result)


def _legacy_statement_type(sql: str) -> str:
    match = _LEGACY_SQL_TYPE_RE.match(sql)
    if not match:
        return "unknown"
    keyword = match.group(1).upper()
    if keyword == "WITH":
        cleaned = _strip_strings_and_comments(sql)
        dml_match = _DML_RE.search(cleaned)
        if dml_match:
            return dml_match.group(1).lower()
        return "select"
    if keyword in ("EXEC", "EXECUTE", "CALL", "DO"):
        return "execute"
    return keyword.lower()


def _statement_type_from_expression(statement: exp.Expression) -> str:
    if isinstance(statement, exp.Select):
        for child_type, name in (
            (exp.Insert, "insert"),
            (exp.Update, "update"),
            (exp.Delete, "delete"),
            (exp.Merge, "merge"),
        ):
            if statement.find(child_type) is not None:
                return name
        return "select"
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


def statement_type(sql: str, db_type: str = "postgres") -> str:
    explain = _split_explain(sql)
    if explain is not None:
        analyze, inner_sql = explain
        inner_type = statement_type(inner_sql, db_type)
        if analyze and inner_type in _MUTATING_TYPES:
            return inner_type
        return "explain" if inner_type != "unknown" else "unknown"

    statements = _parse(sql, db_type)
    if len(statements) != 1:
        return _legacy_statement_type(sql)
    parsed_type = _statement_type_from_expression(statements[0])
    return parsed_type if parsed_type != "unknown" else _legacy_statement_type(sql)


def _expressions_for_table_scan(sql: str, db_type: str = "postgres") -> Iterable[exp.Expression]:
    explain = _split_explain(sql)
    if explain is not None:
        _analyze, inner_sql = explain
        return _parse(inner_sql, db_type)
    return _parse(sql, db_type)


def _cte_aliases(statement: exp.Expression) -> set[str]:
    aliases: set[str] = set()
    for cte in statement.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            aliases.add(alias.lower())
    return aliases


def table_names(sql: str, db_type: str = "postgres") -> set[str]:
    tables: set[str] = set()
    for statement in _expressions_for_table_scan(sql, db_type):
        cte_aliases = _cte_aliases(statement)
        for table in statement.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            normalized = name.lower()
            if normalized in cte_aliases:
                continue
            tables.add(normalized)
    return tables


def blocked_ast_keyword(sql: str, db_type: str = "postgres") -> str | None:
    explain = _split_explain(sql)
    if explain is not None:
        analyze, inner_sql = explain
        if analyze and statement_type(inner_sql, db_type) in _MUTATING_TYPES:
            return "EXPLAIN ANALYZE"
        return None

    statements = _parse(sql, db_type)
    for statement in statements:
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
