from app.routers.query import _detect_neo4j_statement_type


def test_neo4j_statement_type_ignores_keywords_in_labels_and_string_literals():
    assert _detect_neo4j_statement_type("MATCH (n:Delete) RETURN n") == "select"
    assert _detect_neo4j_statement_type("RETURN 'DELETE' AS word") == "select"
    assert _detect_neo4j_statement_type("MATCH (n {action: 'SET'}) RETURN n") == "select"


def test_neo4j_statement_type_detects_mutating_clauses():
    assert _detect_neo4j_statement_type("MATCH (n) DELETE n") == "delete"
    assert _detect_neo4j_statement_type("MATCH (n) SET n.name = 'x' RETURN n") == "update"
    assert _detect_neo4j_statement_type("CREATE (:User {name: 'x'})") == "insert"
