"""Tests for SPARQL statement-type detection.

This module is the security gate for the GraphDB integration.
False negatives (treating a write as read) are a critical security bug.
False positives (treating a legitimate read as a write) are user-facing breakage.
"""
import pytest

from app.services.sparql_analysis import detect_sparql_statement_type as detect


# ---------------------------------------------------------------------------
# Positive: read forms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sparql,expected", [
    ("SELECT ?s WHERE { ?s ?p ?o }", "select"),
    ("select * where { ?s ?p ?o }", "select"),
    ("ASK { ?s ?p ?o }", "ask"),
    ("ASK { FILTER(false) }", "ask"),
    ("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", "construct"),
    ("DESCRIBE <http://example.org/x>", "describe"),
])
def test_read_forms(sparql, expected):
    assert detect(sparql) == expected


def test_prefix_then_select():
    sparql = """
    PREFIX ex: <http://example.org/>
    SELECT ?s WHERE { ?s ex:p ?o }
    """
    assert detect(sparql) == "select"


def test_base_then_select():
    sparql = "BASE <http://example.org/> SELECT ?s WHERE { ?s ?p ?o }"
    assert detect(sparql) == "select"


def test_prefix_iri_contains_select_keyword():
    """`SELECT` inside a PREFIX IRI must not break prologue parsing."""
    sparql = "PREFIX ex: <http://example.com/SELECT/> SELECT ?s WHERE { ?s ex:p ?o }"
    assert detect(sparql) == "select"


# ---------------------------------------------------------------------------
# Comment/literal stripping
# ---------------------------------------------------------------------------

def test_keyword_in_hash_comment_ignored():
    sparql = "# INSERT DATA { ex:a ex:b ex:c }\nSELECT ?s WHERE { ?s ?p ?o }"
    assert detect(sparql) == "select"


def test_keyword_in_block_comment_ignored():
    sparql = "/* DELETE WHERE { ?s ?p ?o } */ SELECT ?s WHERE { ?s ?p ?o }"
    assert detect(sparql) == "select"


def test_keyword_in_string_literal_ignored():
    sparql = 'SELECT ?s WHERE { ?s ex:label "DELETE WHERE { ?x ?y ?z }" }'
    assert detect(sparql) == "select"


def test_keyword_in_triple_quoted_literal_ignored():
    sparql = 'SELECT ?s WHERE { ?s ex:label """INSERT DATA {}""" }'
    assert detect(sparql) == "select"


# ---------------------------------------------------------------------------
# Negative: write forms must all reject
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sparql", [
    "INSERT DATA { ex:a ex:b ex:c }",
    "DELETE DATA { ex:a ex:b ex:c }",
    "LOAD <http://example.org/data.ttl>",
    "CLEAR ALL",
    "CLEAR GRAPH <http://example.org/g>",
    "DROP GRAPH <http://example.org/g>",
    "CREATE GRAPH <http://example.org/g>",
    "COPY <http://example.org/g1> TO <http://example.org/g2>",
    "MOVE <http://example.org/g1> TO <http://example.org/g2>",
    "ADD <http://example.org/g1> TO <http://example.org/g2>",
    "WITH <http://example.org/g> DELETE { ?s ?p ?o } WHERE { ?s ?p ?o }",
    "MODIFY <http://example.org/g> DELETE { ?s ?p ?o } INSERT { ?s ?p ?o } WHERE {}",
    "EXPLAIN SELECT ?s WHERE { ?s ?p ?o }",
    "DEFINE input:inference 'none' SELECT ?s WHERE { ?s ?p ?o }",
    "SPARQL SELECT ?s WHERE { ?s ?p ?o }",
])
def test_write_or_extension_forms_reject(sparql):
    assert detect(sparql) == "reject"


# ---------------------------------------------------------------------------
# Multi-statement and trailing semicolon
# ---------------------------------------------------------------------------

def test_multi_statement_select_then_insert_reject():
    sparql = "SELECT ?s WHERE { ?s ?p ?o } ; INSERT DATA { ex:a ex:b ex:c }"
    assert detect(sparql) == "reject"


def test_trailing_semicolon_reject():
    """Spec §4: trailing `;` is rejected with a clear error rather than stripped."""
    sparql = "SELECT ?s WHERE { ?s ?p ?o } ;"
    assert detect(sparql) == "reject"


def test_property_list_semicolon_inside_braces_allowed():
    """`;` at depth >= 1 is the property list separator and must pass."""
    sparql = "SELECT ?s WHERE { ?s ex:p ?o ; ex:q ?r }"
    assert detect(sparql) == "select"


def test_property_list_inside_construct_allowed():
    sparql = """
    CONSTRUCT { ?s ex:p ?o ; ex:q ?r } WHERE { ?s ex:p ?o ; ex:q ?r }
    """
    assert detect(sparql) == "construct"


# ---------------------------------------------------------------------------
# Whitespace / BOM normalization
# ---------------------------------------------------------------------------

def test_bom_prefix_stripped():
    sparql = "﻿SELECT ?s WHERE { ?s ?p ?o }"
    assert detect(sparql) == "select"


def test_unusual_unicode_whitespace_rejected():
    """NBSP and ideographic space are not legal SPARQL whitespace; reject conservatively."""
    sparql = " SELECT ?s WHERE { ?s ?p ?o }"
    assert detect(sparql) == "reject"
