"""SPARQL statement-type detection.

This module is the security single-source-of-truth for read/write
classification of SPARQL queries directed at GraphDB. It is called from
``routers/query.py::_detect_statement_type`` and nowhere else (see spec
``docs/superpowers/specs/2026-05-26-graphdb-integration-design.md`` §6.2).

The implementation works in five passes over the input:
  1. Strip BOM; reject inputs that contain non-ASCII whitespace.
  2. Strip ``#`` line comments and ``/* ... */`` block comments.
  3. Strip string literals (``"..."``, ``'...'``, triple-quoted).
  4. Reject if the post-strip text contains a depth-0 ``;`` (multi-statement
     or trailing semicolon).
  5. Skip ``BASE``/``PREFIX`` prologue tokens, then match the first remaining
     keyword against ``SELECT|ASK|CONSTRUCT|DESCRIBE``. Anything else rejects.
"""
from __future__ import annotations

import re
from typing import Literal

__all__ = [
    "detect_sparql_statement_type",
    "strip_sparql_strings_and_comments",
    "StatementType",
]

StatementType = Literal["select", "ask", "construct", "describe", "reject"]

# Unicode whitespace that SPARQL spec does not list as legal — we reject these
# conservatively rather than silently normalize, to avoid bypass vectors.
# Also includes ASCII control whitespace (VT/FF) and NEL that Python's ``\s``
# would otherwise match, so the regex token-separator class and this disallow
# list agree on exactly which characters count as whitespace.
_DISALLOWED_WHITESPACE = re.compile(
    "[\x0b\x0c\x85\xa0  -     　﻿​‌‍]"
)

# ``\r`` is a legal SPARQL line terminator (Windows CRLF / classic Mac CR), so
# a ``#`` line comment must end at either ``\n`` or ``\r``. Without ``\r`` in
# the terminator class the comment would swallow the next line on CR-only or
# CRLF inputs and falsely reject legitimate SELECT queries that follow.
_LINE_COMMENT = re.compile(r"#[^\n\r]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# Triple-quoted strings must be matched before single-quoted to avoid
# misreading """foo""" as "" + foo + "".
_TRIPLE_DQ = re.compile(r'"""(?:[^"\\]|\\.|"(?!""))*"""', re.DOTALL)
_TRIPLE_SQ = re.compile(r"'''(?:[^'\\]|\\.|'(?!''))*'''", re.DOTALL)
_DQ = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_SQ = re.compile(r"'(?:[^'\\\n]|\\.)*'")

# Use an explicit ASCII whitespace class ``[ \t\r\n]`` instead of ``\s`` so the
# token separator matches exactly what ``_DISALLOWED_WHITESPACE`` lets through.
# The PREFIX label uses ``[\w.\-]*`` because SPARQL ``PN_PREFIX`` legitimately
# permits ``-`` and ``.`` (e.g. ``dcat-ap``, ``foaf.v0.1``).
_PROLOGUE = re.compile(
    r"""
    ^[ \t\r\n]*
    (?:
        BASE [ \t\r\n]+ < [^>]* >
        |
        PREFIX [ \t\r\n]+ [\w.\-]* : [ \t\r\n]* < [^>]* >
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_FIRST_KEYWORD = re.compile(r"^[ \t\r\n]*([A-Za-z_][A-Za-z0-9_]*)")

_READ_KEYWORDS = {
    "SELECT": "select",
    "ASK": "ask",
    "CONSTRUCT": "construct",
    "DESCRIBE": "describe",
}


def _strip_strings_and_comments(text: str) -> str:
    """Remove string literals and comments.

    Strings are stripped before comments so a ``#`` inside a literal isn't
    treated as a comment start. Block comments before line comments so that
    ``/* # */`` is one block, not a block followed by a line comment.
    """
    text = _TRIPLE_DQ.sub('""', text)
    text = _TRIPLE_SQ.sub("''", text)
    text = _DQ.sub('""', text)
    text = _SQ.sub("''", text)
    text = _BLOCK_COMMENT.sub(" ", text)
    text = _LINE_COMMENT.sub("", text)
    return text


def strip_sparql_strings_and_comments(text: str) -> str:
    """Public wrapper for non-structural SPARQL text checks."""
    return _strip_strings_and_comments(text)


def _contains_top_level_semicolon(text: str) -> bool:
    """Return True iff a ``;`` appears at brace depth 0 (statement separator).

    ``;`` at depth >= 1 is the SPARQL property list separator and is legal.
    Strings/comments are assumed already stripped.
    """
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            return True
    return False


def _strip_prologue(text: str) -> str:
    """Repeatedly strip ``BASE`` and ``PREFIX`` declarations from the start."""
    while True:
        match = _PROLOGUE.match(text)
        if not match:
            return text.lstrip()
        text = text[match.end():]


def detect_sparql_statement_type(sparql: str) -> StatementType:
    """Classify a SPARQL query as a read form or reject it.

    Returns one of ``"select"``, ``"ask"``, ``"construct"``, ``"describe"``
    for legal read queries, otherwise ``"reject"``. The caller is responsible
    for converting ``"reject"`` into an HTTP error.
    """
    # 1. BOM strip + Unicode whitespace check.
    if sparql.startswith("﻿"):
        sparql = sparql[1:]
    if _DISALLOWED_WHITESPACE.search(sparql):
        return "reject"

    # 2-3. Strip literals and comments.
    stripped = _strip_strings_and_comments(sparql)

    # 4. Reject multi-statement / trailing semicolon.
    if _contains_top_level_semicolon(stripped):
        return "reject"

    # 5. Skip prologue and inspect the first keyword.
    body = _strip_prologue(stripped)
    match = _FIRST_KEYWORD.match(body)
    if not match:
        return "reject"
    keyword = match.group(1).upper()
    return _READ_KEYWORDS.get(keyword, "reject")  # type: ignore[return-value]
