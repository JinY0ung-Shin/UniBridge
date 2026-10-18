"""Tests for SQL keyword blacklist validator."""
from __future__ import annotations

import pytest

from app.services.sql_validator import validate_sql


class TestValidateSql:
    """Test blocked keyword detection."""

    def test_allows_normal_select(self):
        errors = validate_sql("SELECT * FROM users")
        assert errors is None

    def test_allows_normal_insert(self):
        errors = validate_sql("INSERT INTO users (name) VALUES ('test')")
        assert errors is None

    def test_blocks_grant(self):
        errors = validate_sql("GRANT SELECT ON users TO public")
        assert errors is not None
        assert "GRANT" in errors

    def test_blocks_revoke(self):
        errors = validate_sql("REVOKE ALL ON users FROM public")
        assert errors is not None
        assert "REVOKE" in errors

    def test_blocks_create_user(self):
        errors = validate_sql("CREATE USER hacker WITH PASSWORD 'test'")
        assert errors is not None
        assert "CREATE USER" in errors

    def test_blocks_drop_user(self):
        errors = validate_sql("DROP USER admin")
        assert errors is not None
        assert "DROP USER" in errors

    def test_blocks_alter_user(self):
        errors = validate_sql("ALTER USER admin WITH SUPERUSER")
        assert errors is not None
        assert "ALTER USER" in errors

    def test_blocks_shutdown(self):
        errors = validate_sql("SHUTDOWN")
        assert errors is not None
        assert "SHUTDOWN" in errors

    def test_blocks_create_login(self):
        errors = validate_sql("CREATE LOGIN hacker WITH PASSWORD = 'test'")
        assert errors is not None
        assert "CREATE LOGIN" in errors

    def test_blocks_drop_login(self):
        errors = validate_sql("DROP LOGIN hacker")
        assert errors is not None
        assert "DROP LOGIN" in errors

    def test_blocks_backup(self):
        errors = validate_sql("BACKUP DATABASE mydb TO DISK = '/tmp/backup'")
        assert errors is not None
        assert "BACKUP" in errors

    def test_blocks_restore(self):
        errors = validate_sql("RESTORE DATABASE mydb FROM DISK = '/tmp/backup'")
        assert errors is not None
        assert "RESTORE" in errors

    def test_blocks_do_block(self):
        errors = validate_sql("DO $$ BEGIN DELETE FROM users; END $$")
        assert errors is not None
        assert "DO" in errors

    def test_blocks_call(self):
        errors = validate_sql("CALL dangerous_proc()")
        assert errors is not None
        assert "CALL" in errors

    def test_blocks_exec(self):
        errors = validate_sql("EXEC sp_who")
        assert errors is not None
        assert "EXEC" in errors

    def test_blocks_merge(self):
        errors = validate_sql(
            "MERGE INTO users USING staging ON users.id = staging.id WHEN MATCHED THEN DELETE"
        )
        assert errors is not None
        assert "MERGE" in errors

    def test_blocks_explain_analyze_dml(self):
        errors = validate_sql("EXPLAIN ANALYZE DELETE FROM users WHERE id = 1")
        assert errors is not None
        assert "EXPLAIN ANALYZE" in errors

    def test_ignores_keyword_in_string(self):
        errors = validate_sql("SELECT * FROM users WHERE name = 'GRANT me access'")
        assert errors is None

    def test_ignores_keyword_in_comment(self):
        errors = validate_sql("SELECT * FROM users -- GRANT stuff\nWHERE id = 1")
        assert errors is None

    def test_ignores_keyword_in_block_comment(self):
        errors = validate_sql("SELECT * /* SHUTDOWN */ FROM users")
        assert errors is None

    def test_blocks_kill(self):
        errors = validate_sql("KILL 1234")
        assert errors is not None
        assert "KILL" in errors

    def test_custom_blocked_keywords(self):
        errors = validate_sql("VACUUM users", extra_blocked=["VACUUM"])
        assert errors is not None
        assert "VACUUM" in errors

    def test_custom_keyword_allows_normal(self):
        errors = validate_sql("SELECT * FROM users", extra_blocked=["VACUUM"])
        assert errors is None


class TestBlockedFunctions:
    """Calls that reach outside the database are blocked per engine, but the
    same words as column or table names are not."""

    @pytest.mark.parametrize(
        ("db_type", "sql"),
        [
            ("clickhouse", "SELECT * FROM url('http://169.254.169.254/latest/meta-data', 'CSV')"),
            ("clickhouse", "SELECT count() FROM s3('http://minio:9000/b/*.parquet', 'Parquet')"),
            ("clickhouse", "SELECT * FROM remote('localhost:9000', system.users)"),
            ("clickhouse", "SELECT * FROM remoteSecure('h:9440', db.t, 'u', 'p')"),
            ("clickhouse", "SELECT * FROM cluster ('c', db.t)"),
            ("clickhouse", "SELECT * FROM clusterAllReplicas('default', system.query_log)"),
            ("clickhouse", "SELECT * FROM t JOIN url('http://x', 'CSV') AS u ON 1"),
            ("clickhouse", "INSERT INTO FUNCTION url('http://x/', 'CSV') SELECT 1"),
            ("clickhouse", "CREATE TABLE t (x UInt8) ENGINE = MySQL('h:3306', 'db', 't', 'u', 'p')"),
            ("clickhouse", "SELECT * FROM icebergS3('http://x')"),
            ("postgres", "SELECT pg_read_file('/etc/passwd')"),
            ("postgres", "SELECT pg_catalog.pg_read_file('/etc/passwd')"),
            ("postgres", "SELECT * FROM pg_ls_dir('.')"),
            ("postgres", "SELECT lo_import('/etc/passwd')"),
            ("postgres", "SELECT * FROM dblink('host=10.0.0.1', 'select 1') AS t(x int)"),
            ("postgres", "SELECT dblink_exec('host=x', 'drop table t')"),
            # SQL inside a string escapes the table-access check
            ("postgres", "SELECT query_to_xml('select * from secret', true, false, '')"),
            ("postgres", "SELECT table_to_xml('secret', true, false, '')"),
            ("mssql", "SELECT * FROM OPENQUERY(srv, 'select 1')"),
            ("mssql", "INSERT INTO OPENQUERY(srv, 'select a from t') VALUES (1)"),
            ("mssql", "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Data Source=x').db.dbo.t"),
            ("mssql", "SELECT * FROM sys.fn_xe_file_target_read_file('\\\\h\\s\\*.xel', NULL, NULL, NULL)"),
        ],
    )
    def test_blocks_outside_reaching_calls(self, db_type, sql):
        errors = validate_sql(sql, db_type=db_type)
        assert errors is not None
        assert "Blocked SQL function" in errors

    @pytest.mark.parametrize(
        ("db_type", "sql"),
        [
            # quoted names
            ("clickhouse", "SELECT * FROM `url`('http://x', 'CSV')"),
            ("clickhouse", "SELECT * FROM \"s3\"('http://x')"),
            ("clickhouse", "SELECT * FROM `\\x75rl`('http://x', 'CSV')"),
            ("postgres", "SELECT \"pg_read_file\"('/etc/passwd')"),
            ("postgres", "SELECT U&\"pg\\005fread_file\"('/etc/passwd')"),
            # a backslash that may or may not end the string
            ("clickhouse", "SELECT 'a\\'', url('http://x') -- '"),
            ("postgres", "SELECT E'\\'', pg_read_file('x') -- '"),
            ("postgres", "SELECT 'a\\', pg_read_file('x') -- '"),
            # a comment between the name and the parenthesis
            ("clickhouse", "SELECT * FROM url/* x */('http://x', 'CSV')"),
            # a T-SQL bracket identifier holding a quote
            ("mssql", "SELECT [it's], * FROM OPENQUERY(srv, 'q')"),
            # `$` inside a name is not the start of a dollar quote
            ("mssql", "SELECT a$b$c FROM OPENQUERY(srv, 'select 1')"),
            ("postgres", "SELECT x$y$ FROM t, pg_read_file('x')"),
            # doubled quotes inside quoted names
            ("postgres", "SELECT \"a\"\"b\", pg_read_file('x')"),
            ("mssql", "SELECT [a]]b], * FROM OPENQUERY(srv, 'q')"),
        ],
    )
    def test_blocks_disguised_calls(self, db_type, sql):
        assert validate_sql(sql, db_type=db_type) is not None

    @pytest.mark.parametrize(
        ("db_type", "sql"),
        [
            ("clickhouse", "SELECT url, file, cluster, remote, s3 FROM pages"),
            ("clickhouse", "INSERT INTO url (id) VALUES (1)"),
            ("clickhouse", "INSERT INTO db.url (id) VALUES (1)"),
            ("clickhouse", "CREATE TABLE IF NOT EXISTS file (id UInt64) ENGINE = Memory"),
            ("clickhouse", "SELECT * FROM db.url WHERE path LIKE '%url(%'"),
            ("clickhouse", "SELECT URLHierarchy(url), filesystemAvailable() FROM pages"),
            ("clickhouse", "SELECT 1 -- url('http://x')"),
            ("clickhouse", "SELECT /* s3( */ 1"),
            ("clickhouse", "SELECT count() FROM numbers(10)"),
            ("postgres", "SELECT * FROM remote_hosts JOIN url_map USING (id)"),
            ("postgres", "SELECT xmlelement(name foo, 'bar')"),
            ("postgres", "SELECT $$ pg_read_file('x') $$"),
            ("postgres", "SELECT $q$ pg_read_file('x') $q$"),
            ("postgres", "SELECT * FROM t WHERE a = $1 AND b = $2"),
            ("postgres", "SELECT U&\"a\\zzzz\" FROM t"),
            ("mssql", "SELECT [url], [file] FROM pages"),
            # unterminated text the database itself would reject
            ("postgres", "SELECT 1 /* pg_read_file('x')"),
            ("postgres", "SELECT 'pg_read_file(x)"),
            ("postgres", "SELECT \"abc"),
            ("postgres", "SELECT $q$ pg_read_file('x')"),
        ],
    )
    def test_allows_same_words_that_are_not_calls(self, db_type, sql):
        assert validate_sql(sql, db_type=db_type) is None

    def test_lists_are_per_engine(self):
        # ClickHouse's names are ordinary words elsewhere: a Postgres function
        # called remote() is the user's own.
        assert validate_sql("SELECT remote(1)", db_type="postgres") is None
        assert validate_sql("SELECT remote(1)", db_type="clickhouse") is not None
        assert validate_sql("MATCH (n) RETURN file(n)", db_type="neo4j") is None

    def test_unknown_engine_checks_every_list(self):
        assert validate_sql("SELECT pg_read_file('x')") is not None
        assert validate_sql("SELECT * FROM s3('http://x')") is not None
