from app.routers.query import _detect_neo4j_statement_type


def test_neo4j_statement_type_ignores_keywords_in_labels_and_string_literals():
    assert _detect_neo4j_statement_type("MATCH (n:Delete) RETURN n") == "select"
    assert _detect_neo4j_statement_type("RETURN 'DELETE' AS word") == "select"
    assert _detect_neo4j_statement_type("MATCH (n {action: 'SET'}) RETURN n") == "select"


def test_neo4j_statement_type_detects_read_only_queries():
    assert _detect_neo4j_statement_type("MATCH (n) RETURN n") == "select"
    assert _detect_neo4j_statement_type("OPTIONAL MATCH (n) RETURN n") == "select"
    assert _detect_neo4j_statement_type("RETURN 1 AS value") == "select"
    assert _detect_neo4j_statement_type("WITH 1 AS value RETURN value") == "select"
    assert _detect_neo4j_statement_type("UNWIND [1, 2] AS value RETURN value") == "select"


def test_neo4j_statement_type_detects_mutating_clauses():
    assert _detect_neo4j_statement_type("MATCH (n) DELETE n") == "delete"
    assert _detect_neo4j_statement_type("MATCH (n) SET n.name = 'x' RETURN n") == "update"
    assert _detect_neo4j_statement_type("MATCH (n) REMOVE n.name RETURN n") == "update"
    assert _detect_neo4j_statement_type("CREATE (:User {name: 'x'})") == "insert"
    assert _detect_neo4j_statement_type("MERGE (:User {name: 'x'})") == "insert"
    assert _detect_neo4j_statement_type("DROP INDEX user_name IF EXISTS") == "execute"
    assert (
        _detect_neo4j_statement_type(
            "LOAD CSV FROM 'file:///users.csv' AS row RETURN row"
        )
        == "execute"
    )
    assert _detect_neo4j_statement_type("CALL db.labels() YIELD label RETURN label") == "execute"
