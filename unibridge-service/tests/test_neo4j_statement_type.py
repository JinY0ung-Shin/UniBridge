from app.routers.query import _detect_neo4j_statement_type


def test_neo4j_statement_type_ignores_keywords_in_labels_and_string_literals():
    assert _detect_neo4j_statement_type("MATCH (n:Delete) RETURN n") == "select"
    assert _detect_neo4j_statement_type("RETURN 'DELETE' AS word") == "select"
    assert _detect_neo4j_statement_type("MATCH (n {action: 'SET'}) RETURN n") == "select"
    assert _detect_neo4j_statement_type("MATCH (n {op: 'CALL db.labels()'}) RETURN n") == "select"


def test_neo4j_statement_type_detects_read_only_queries():
    assert _detect_neo4j_statement_type("MATCH (n) RETURN n") == "select"
    assert _detect_neo4j_statement_type("OPTIONAL MATCH (n) RETURN n") == "select"
    assert _detect_neo4j_statement_type("RETURN 1 AS value") == "select"
    assert _detect_neo4j_statement_type("WITH 1 AS value RETURN value") == "select"
    assert _detect_neo4j_statement_type("UNWIND [1, 2] AS value RETURN value") == "select"


def test_neo4j_statement_type_detects_data_writes():
    assert _detect_neo4j_statement_type("MATCH (n) DELETE n") == "delete"
    assert _detect_neo4j_statement_type("MATCH (n) DETACH DELETE n") == "delete"
    assert _detect_neo4j_statement_type("MATCH (n) SET n.name = 'x' RETURN n") == "update"
    assert _detect_neo4j_statement_type("MATCH (n) REMOVE n.name RETURN n") == "update"
    assert _detect_neo4j_statement_type("CREATE (:User {name: 'x'})") == "insert"
    assert _detect_neo4j_statement_type("CREATE (a:User)-[:KNOWS]->(b:User) RETURN a") == "insert"
    assert _detect_neo4j_statement_type("MERGE (:User {name: 'x'})") == "insert"
    assert (
        _detect_neo4j_statement_type("UNWIND $rows AS row CREATE (:Item {id: row.id})")
        == "insert"
    )
    # A CREATE on a label that happens to be named like a schema keyword is
    # still a data write — the schema/admin match requires the bare keyword.
    assert _detect_neo4j_statement_type("CREATE (n:Index {name: 'x'})") == "insert"


def test_neo4j_statement_type_mixed_write_takes_most_restrictive_clause():
    # DELETE > SET/REMOVE > CREATE/MERGE: a statement combining clauses is
    # gated by the strongest flag it needs.
    assert _detect_neo4j_statement_type("MERGE (n:User) ON CREATE SET n.created = 1") == "update"
    assert _detect_neo4j_statement_type("MATCH (n) SET n.x = 1 DELETE n") == "delete"


def test_neo4j_statement_type_detects_procedures_and_imports():
    assert _detect_neo4j_statement_type("DROP INDEX user_name IF EXISTS") == "execute"
    assert (
        _detect_neo4j_statement_type(
            "LOAD CSV FROM 'file:///users.csv' AS row RETURN row"
        )
        == "execute"
    )
    assert _detect_neo4j_statement_type("CALL db.labels() YIELD label RETURN label") == "execute"
    # CALL wins over data-write keywords: a procedure smuggled into a
    # data-shaped query must not pass as a flag-gated data write.
    assert (
        _detect_neo4j_statement_type(
            "MATCH (p) CALL apoc.create.node(['X'], {}) YIELD node CREATE (q) RETURN q"
        )
        == "execute"
    )
    assert (
        _detect_neo4j_statement_type(
            "CALL { MATCH (n) DELETE n } IN TRANSACTIONS OF 100 ROWS"
        )
        == "execute"
    )
    # USE retargets another database on the same server; a write flag must not
    # let a query escape the registered database scope.
    assert _detect_neo4j_statement_type("USE other CREATE (:User {name: 'x'})") == "execute"
    assert _detect_neo4j_statement_type("USE other MATCH (n) RETURN n") == "execute"


def test_neo4j_statement_type_detects_schema_admin_commands():
    assert (
        _detect_neo4j_statement_type(
            "CREATE INDEX user_name IF NOT EXISTS FOR (n:User) ON (n.name)"
        )
        == "execute"
    )
    assert (
        _detect_neo4j_statement_type(
            "CREATE FULLTEXT INDEX names FOR (n:User) ON EACH [n.name]"
        )
        == "execute"
    )
    assert (
        _detect_neo4j_statement_type(
            "CREATE CONSTRAINT uniq_id FOR (n:User) REQUIRE n.id IS UNIQUE"
        )
        == "execute"
    )
    assert _detect_neo4j_statement_type("CREATE DATABASE inventory") == "execute"
    assert _detect_neo4j_statement_type("CREATE OR REPLACE DATABASE inventory") == "execute"
    assert _detect_neo4j_statement_type("CREATE COMPOSITE DATABASE fabric") == "execute"
    # Admin commands embedding data-write keywords must not classify as writes.
    assert _detect_neo4j_statement_type("CREATE USER alice SET PASSWORD 'x'") == "execute"
    assert _detect_neo4j_statement_type("ALTER USER alice SET PASSWORD 'x'") == "execute"
    assert _detect_neo4j_statement_type("ALTER DATABASE inventory SET ACCESS READ ONLY") == "execute"
    assert _detect_neo4j_statement_type("CREATE ROLE readers") == "execute"
    assert _detect_neo4j_statement_type("GRANT ROLE readers TO alice") == "execute"
    assert _detect_neo4j_statement_type("DENY ACCESS ON DATABASE * TO readers") == "execute"
    assert _detect_neo4j_statement_type("REVOKE ROLE readers FROM alice") == "execute"
    assert _detect_neo4j_statement_type("RENAME USER alice TO bob") == "execute"
    assert _detect_neo4j_statement_type("STOP DATABASE inventory") == "execute"
    assert _detect_neo4j_statement_type("START DATABASE inventory") == "execute"


def test_neo4j_statement_type_unknown_statements():
    assert _detect_neo4j_statement_type("SHOW INDEXES") == "unknown"
    assert _detect_neo4j_statement_type("EXPLAIN MATCH (n) RETURN n") == "unknown"
