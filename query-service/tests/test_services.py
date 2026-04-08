"""Comprehensive unit tests for the services layer."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.models import AuditLog


# ═══════════════════════════════════════════════════════════════════════════════
# query_executor: detect_statement_type
# ═══════════════════════════════════════════════════════════════════════════════

from app.services.query_executor import (
    check_multi_statement,
    check_permission,
    detect_statement_type,
)


class TestDetectStatementType:
    """Tests for detect_statement_type()."""

    def test_select_uppercase(self):
        assert detect_statement_type("SELECT * FROM t") == "select"

    def test_select_leading_spaces(self):
        assert detect_statement_type("  select * from t") == "select"

    def test_select_mixed_case(self):
        assert detect_statement_type("Select id FROM t") == "select"

    def test_insert(self):
        assert detect_statement_type("INSERT INTO t VALUES(1)") == "insert"

    def test_update(self):
        assert detect_statement_type("UPDATE t SET x=1") == "update"

    def test_delete(self):
        assert detect_statement_type("DELETE FROM t") == "delete"

    def test_create(self):
        assert detect_statement_type("CREATE TABLE t(id int)") == "create"

    def test_alter(self):
        assert detect_statement_type("ALTER TABLE t ADD col") == "alter"

    def test_drop(self):
        assert detect_statement_type("DROP TABLE t") == "drop"

    def test_truncate(self):
        assert detect_statement_type("TRUNCATE TABLE t") == "truncate"

    def test_exec(self):
        assert detect_statement_type("EXEC sp_test") == "execute"

    def test_execute(self):
        assert detect_statement_type("EXECUTE sp_test") == "execute"

    def test_call(self):
        assert detect_statement_type("CALL sp_test") == "execute"

    def test_with_cte_select(self):
        sql = "WITH cte AS (SELECT 1) SELECT * FROM cte"
        assert detect_statement_type(sql) == "select"

    def test_with_cte_insert(self):
        sql = "WITH cte AS (SELECT 1) INSERT INTO t SELECT * FROM cte"
        assert detect_statement_type(sql) == "insert"

    def test_with_cte_delete(self):
        sql = "WITH cte AS (SELECT 1) DELETE FROM t"
        assert detect_statement_type(sql) == "delete"

    def test_with_cte_update(self):
        sql = "WITH cte AS (SELECT 1) UPDATE t SET x = 1"
        assert detect_statement_type(sql) == "update"

    def test_explain_returns_explain(self):
        # EXPLAIN is its own statement type (not collapsed to "select")
        assert detect_statement_type("EXPLAIN SELECT * FROM t") == "explain"

    def test_garbage_text(self):
        assert detect_statement_type("garbage text") == "unknown"

    def test_empty_string(self):
        assert detect_statement_type("") == "unknown"

    def test_only_whitespace(self):
        assert detect_statement_type("   ") == "unknown"

    def test_newline_prefix(self):
        assert detect_statement_type("\nSELECT 1") == "select"

    def test_tab_prefix(self):
        assert detect_statement_type("\tDELETE FROM t") == "delete"

    def test_insert_lowercase(self):
        assert detect_statement_type("insert into t values(1)") == "insert"

    def test_with_only_select_no_dml(self):
        # WITH containing only SELECT keywords in the body
        sql = "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b"
        assert detect_statement_type(sql) == "select"


# ═══════════════════════════════════════════════════════════════════════════════
# query_executor: check_multi_statement
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckMultiStatement:
    """Tests for check_multi_statement()."""

    def test_two_statements(self):
        assert check_multi_statement("SELECT 1; SELECT 2") is True

    def test_single_statement(self):
        assert check_multi_statement("SELECT 1") is False

    def test_semicolon_inside_string(self):
        assert check_multi_statement("SELECT 'a;b'") is False

    def test_escaped_quote_then_semicolon(self):
        # 'it''s' is two single-quoted segments: 'it' and 's'
        # After parsing, the ; is outside quotes -> True
        assert check_multi_statement("SELECT 'it''s'; DROP") is True

    def test_trailing_semicolon(self):
        assert check_multi_statement("SELECT 1;") is True

    def test_empty_string(self):
        assert check_multi_statement("") is False

    def test_semicolon_in_nested_strings(self):
        # "SELECT 'a' || ';' || 'b'" - semicolon is inside quotes
        assert check_multi_statement("SELECT 'a' || ';' || 'b'") is False

    def test_no_semicolon_with_quotes(self):
        assert check_multi_statement("SELECT 'hello world'") is False

    def test_multiple_semicolons(self):
        assert check_multi_statement("SELECT 1; SELECT 2; SELECT 3") is True

    def test_semicolon_at_start(self):
        assert check_multi_statement("; SELECT 1") is True


# ═══════════════════════════════════════════════════════════════════════════════
# query_executor: check_permission
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckPermission:
    """Tests for check_permission()."""

    def test_select_allowed(self):
        assert check_permission("select", True, False, False, False) is True

    def test_select_denied(self):
        assert check_permission("select", False, True, True, True) is False

    def test_insert_allowed(self):
        assert check_permission("insert", False, True, False, False) is True

    def test_insert_denied(self):
        assert check_permission("insert", True, False, True, True) is False

    def test_update_allowed(self):
        assert check_permission("update", False, False, True, False) is True

    def test_update_denied(self):
        assert check_permission("update", True, True, False, True) is False

    def test_delete_allowed(self):
        assert check_permission("delete", False, False, False, True) is True

    def test_delete_denied(self):
        assert check_permission("delete", True, True, True, False) is False

    def test_ddl_create_all_true(self):
        assert check_permission("create", True, True, True, True) is True

    def test_ddl_create_missing_select(self):
        assert check_permission("create", False, True, True, True) is False

    def test_ddl_create_missing_insert(self):
        assert check_permission("create", True, False, True, True) is False

    def test_ddl_create_missing_update(self):
        assert check_permission("create", True, True, False, True) is False

    def test_ddl_create_missing_delete(self):
        assert check_permission("create", True, True, True, False) is False

    def test_ddl_alter_all_true(self):
        assert check_permission("alter", True, True, True, True) is True

    def test_ddl_alter_any_false(self):
        assert check_permission("alter", True, True, False, True) is False

    def test_ddl_drop_all_true(self):
        assert check_permission("drop", True, True, True, True) is True

    def test_ddl_drop_any_false(self):
        assert check_permission("drop", False, True, True, True) is False

    def test_ddl_truncate_all_true(self):
        assert check_permission("truncate", True, True, True, True) is True

    def test_ddl_truncate_any_false(self):
        assert check_permission("truncate", True, False, True, True) is False

    def test_execute_requires_select(self):
        assert check_permission("execute", True, False, False, False) is True

    def test_execute_denied_without_select(self):
        assert check_permission("execute", False, True, True, True) is False

    def test_explain_allowed_via_select(self):
        assert check_permission("explain", True, False, False, False) is True

    def test_explain_denied_without_select(self):
        assert check_permission("explain", False, True, True, True) is False

    def test_unknown_always_false(self):
        assert check_permission("unknown", True, True, True, True) is False

    def test_unknown_all_false(self):
        assert check_permission("unknown", False, False, False, False) is False

    def test_unrecognized_type_returns_false(self):
        assert check_permission("merge", True, True, True, True) is False


# ═══════════════════════════════════════════════════════════════════════════════
# connection_manager: encrypt/decrypt passwords
# ═══════════════════════════════════════════════════════════════════════════════

from app.services.connection_manager import (
    _build_url,
    decrypt_password,
    encrypt_password,
    validate_encryption_key,
)


class TestEncryptDecryptPassword:
    """Tests for encrypt_password / decrypt_password round-trip."""

    def test_round_trip_simple(self):
        encrypted = encrypt_password("mysecret")
        assert encrypted != "mysecret"
        assert decrypt_password(encrypted) == "mysecret"

    def test_round_trip_empty_string(self):
        encrypted = encrypt_password("")
        assert decrypt_password(encrypted) == ""

    def test_round_trip_special_characters(self):
        pwd = "p@$$w0rd!#%^&*()"
        encrypted = encrypt_password(pwd)
        assert decrypt_password(encrypted) == pwd

    def test_round_trip_unicode(self):
        pwd = "password"
        encrypted = encrypt_password(pwd)
        assert decrypt_password(encrypted) == pwd

    def test_round_trip_long_password(self):
        pwd = "a" * 500
        encrypted = encrypt_password(pwd)
        assert decrypt_password(encrypted) == pwd

    def test_encrypt_produces_different_ciphertext_each_call(self):
        # Fernet uses a timestamp + random IV, so ciphertexts differ
        a = encrypt_password("same")
        b = encrypt_password("same")
        assert a != b

    def test_decrypt_with_corrupted_token_raises(self):
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_password("not-a-valid-fernet-token")

    def test_decrypt_with_tampered_ciphertext_raises(self):
        encrypted = encrypt_password("secret")
        tampered = encrypted[:-5] + "XXXXX"
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_password(tampered)


class TestDecryptWithWrongKey:
    """Tests that decryption fails when the key changes."""

    def test_wrong_key_raises_value_error(self, monkeypatch):
        encrypted = encrypt_password("mysecret")
        # Switch to a different encryption key
        monkeypatch.setenv("ENCRYPTION_KEY", "a-completely-different-key-32byt!")
        # Force settings to reload
        from app.config import Settings
        new_settings = Settings()
        monkeypatch.setattr("app.services.connection_manager.settings", new_settings)

        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_password(encrypted)


class TestValidateEncryptionKey:
    """Tests for validate_encryption_key()."""

    def test_valid_key_does_not_raise(self):
        # The test env already has a valid key configured
        validate_encryption_key()


# ═══════════════════════════════════════════════════════════════════════════════
# connection_manager: _build_url
# ═══════════════════════════════════════════════════════════════════════════════

from app.models import DBConnection


class TestBuildUrl:
    """Tests for _build_url()."""

    def _make_conn(self, db_type: str, **overrides) -> DBConnection:
        defaults = {
            "alias": "test",
            "db_type": db_type,
            "host": "localhost",
            "port": 5432,
            "database": "testdb",
            "username": "user",
            "password_encrypted": encrypt_password("pass"),
        }
        defaults.update(overrides)
        return DBConnection(**defaults)

    def test_postgres_url(self):
        conn = self._make_conn("postgres")
        url = _build_url(conn, "pass")
        assert url == "postgresql+asyncpg://user:pass@localhost:5432/testdb"

    def test_postgres_url_special_chars_in_password(self):
        conn = self._make_conn("postgres")
        url = _build_url(conn, "p@ss/w ord")
        assert "p%40ss%2Fw+ord" in url or "p%40ss%2Fw%20ord" in url

    def test_postgres_url_special_chars_in_username(self):
        conn = self._make_conn("postgres", username="user@domain")
        url = _build_url(conn, "pass")
        assert "user%40domain" in url

    def test_mssql_url(self):
        conn = self._make_conn("mssql", port=1433)
        url = _build_url(conn, "pass")
        assert url.startswith("mssql+aioodbc://user:pass@localhost:1433/testdb")
        assert "driver=ODBC+Driver+18+for+SQL+Server" in url
        assert "TrustServerCertificate=yes" in url

    def test_unsupported_db_type_raises(self):
        conn = self._make_conn("mysql")
        with pytest.raises(ValueError, match="Unsupported db_type: mysql"):
            _build_url(conn, "pass")

    def test_unsupported_db_type_oracle(self):
        conn = self._make_conn("oracle")
        with pytest.raises(ValueError, match="Unsupported db_type: oracle"):
            _build_url(conn, "pass")


# ═══════════════════════════════════════════════════════════════════════════════
# apisix_client
# ═══════════════════════════════════════════════════════════════════════════════

from app.services import apisix_client


def _mock_response(status_code: int = 200, json_data: dict | None = None) -> httpx.Response:
    """Create a mock httpx.Response."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_data or {},
        request=httpx.Request("GET", "http://test"),
    )
    return resp


class TestApisixListResources:
    """Tests for apisix_client.list_resources()."""

    async def test_list_routes_returns_items(self):
        mock_data = {
            "list": [
                {"value": {"id": "1", "uri": "/foo"}},
                {"value": {"id": "2", "uri": "/bar"}},
            ],
            "total": 2,
        }
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await apisix_client.list_resources("routes")

        assert result["total"] == 2
        assert len(result["items"]) == 2
        assert result["items"][0]["id"] == "1"
        assert result["items"][1]["uri"] == "/bar"

    async def test_list_routes_url_construction(self):
        mock_resp = _mock_response(json_data={"list": [], "total": 0})

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await apisix_client.list_resources("upstreams")

        instance.get.assert_called_once()
        call_args = instance.get.call_args
        assert call_args[0][0] == "http://localhost:19180/apisix/admin/upstreams"
        assert call_args[1]["headers"]["X-API-KEY"] == "test-apisix-key"

    async def test_list_resources_empty_list(self):
        mock_resp = _mock_response(json_data={"list": None, "total": 0})

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await apisix_client.list_resources("routes")

        assert result["items"] == []
        assert result["total"] == 0

    async def test_list_resources_entries_without_value_skipped(self):
        mock_data = {
            "list": [
                {"value": {"id": "1"}},
                {"key": "no-value-here"},  # missing "value"
            ],
            "total": 2,
        }
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await apisix_client.list_resources("routes")

        assert len(result["items"]) == 1


class TestApisixGetResource:
    """Tests for apisix_client.get_resource()."""

    async def test_get_resource_returns_value(self):
        mock_data = {"value": {"id": "123", "uri": "/test"}}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await apisix_client.get_resource("routes", "123")

        assert result == {"id": "123", "uri": "/test"}

    async def test_get_resource_url_construction(self):
        mock_resp = _mock_response(json_data={"value": {}})

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await apisix_client.get_resource("consumers", "my-consumer")

        call_args = instance.get.call_args
        assert call_args[0][0] == "http://localhost:19180/apisix/admin/consumers/my-consumer"

    async def test_get_resource_fallback_to_full_data(self):
        # When "value" key is missing, return the full data dict
        mock_data = {"id": "123", "uri": "/test"}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await apisix_client.get_resource("routes", "123")

        assert result == {"id": "123", "uri": "/test"}


class TestApisixPutResource:
    """Tests for apisix_client.put_resource()."""

    async def test_put_resource_sends_body(self):
        body = {"uri": "/new", "upstream_id": "1"}
        mock_resp = _mock_response(json_data={"value": body})

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.put.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await apisix_client.put_resource("routes", "r1", body)

        instance.put.assert_called_once()
        call_args = instance.put.call_args
        assert call_args[0][0] == "http://localhost:19180/apisix/admin/routes/r1"
        assert call_args[1]["json"] == body
        assert call_args[1]["headers"]["X-API-KEY"] == "test-apisix-key"
        assert result == body

    async def test_put_resource_fallback_no_value_key(self):
        body = {"uri": "/new"}
        mock_data = {"action": "set", "uri": "/new"}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.put.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await apisix_client.put_resource("routes", "r1", body)

        assert result == mock_data


class TestApisixDeleteResource:
    """Tests for apisix_client.delete_resource()."""

    async def test_delete_resource_calls_correct_url(self):
        mock_resp = _mock_response(status_code=200, json_data={})

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.delete.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await apisix_client.delete_resource("routes", "r1")

        call_args = instance.delete.call_args
        assert call_args[0][0] == "http://localhost:19180/apisix/admin/routes/r1"
        assert call_args[1]["headers"]["X-API-KEY"] == "test-apisix-key"

    async def test_delete_resource_returns_none(self):
        mock_resp = _mock_response(status_code=200, json_data={})

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.delete.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await apisix_client.delete_resource("routes", "r1")

        assert result is None


class TestApisixErrorHandling:
    """Tests for APISIX client error propagation."""

    async def test_list_resources_http_error_propagates(self):
        error_resp = httpx.Response(
            status_code=500,
            text="Internal Server Error",
            request=httpx.Request("GET", "http://test"),
        )

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = error_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            with pytest.raises(httpx.HTTPStatusError):
                await apisix_client.list_resources("routes")

    async def test_get_resource_404_propagates(self):
        error_resp = httpx.Response(
            status_code=404,
            text="Not Found",
            request=httpx.Request("GET", "http://test"),
        )

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = error_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            with pytest.raises(httpx.HTTPStatusError):
                await apisix_client.get_resource("routes", "nonexistent")

    async def test_put_resource_http_error_propagates(self):
        error_resp = httpx.Response(
            status_code=400,
            text="Bad Request",
            request=httpx.Request("PUT", "http://test"),
        )

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.put.return_value = error_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            with pytest.raises(httpx.HTTPStatusError):
                await apisix_client.put_resource("routes", "r1", {"uri": "/x"})

    async def test_delete_resource_http_error_propagates(self):
        error_resp = httpx.Response(
            status_code=503,
            text="Service Unavailable",
            request=httpx.Request("DELETE", "http://test"),
        )

        with patch("app.services.apisix_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.delete.return_value = error_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            with pytest.raises(httpx.HTTPStatusError):
                await apisix_client.delete_resource("routes", "r1")


# ═══════════════════════════════════════════════════════════════════════════════
# prometheus_client
# ═══════════════════════════════════════════════════════════════════════════════

from app.services.prometheus_client import _parse_duration, instant_query, range_query


class TestParseDuration:
    """Tests for _parse_duration()."""

    def test_15_minutes(self):
        assert _parse_duration("15m") == 900

    def test_1_hour(self):
        assert _parse_duration("1h") == 3600

    def test_6_hours(self):
        assert _parse_duration("6h") == 21600

    def test_24_hours(self):
        assert _parse_duration("24h") == 86400

    def test_7_days(self):
        assert _parse_duration("7d") == 604800

    def test_1_day(self):
        assert _parse_duration("1d") == 86400

    def test_30_minutes(self):
        assert _parse_duration("30m") == 1800

    def test_plain_seconds(self):
        assert _parse_duration("120") == 120

    def test_whitespace_stripped(self):
        assert _parse_duration("  15m  ") == 900

    def test_2_hours(self):
        assert _parse_duration("2h") == 7200


class TestInstantQuery:
    """Tests for prometheus_client.instant_query()."""

    async def test_instant_query_success(self):
        mock_data = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"__name__": "up"}, "value": [1234567890, "1"]},
                ],
            },
        }
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await instant_query("up")

        assert len(result) == 1
        assert result[0]["metric"]["__name__"] == "up"

    async def test_instant_query_url_and_params(self):
        mock_data = {"status": "success", "data": {"result": []}}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await instant_query("rate(http_requests_total[5m])")

        call_args = instance.get.call_args
        assert call_args[0][0] == "http://localhost:19090/api/v1/query"
        assert call_args[1]["params"]["query"] == "rate(http_requests_total[5m])"

    async def test_instant_query_non_success_returns_empty(self):
        mock_data = {"status": "error", "errorType": "bad_data", "error": "parse error"}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await instant_query("bad{query")

        assert result == []

    async def test_instant_query_missing_data_key(self):
        mock_data = {"status": "success"}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await instant_query("up")

        assert result == []

    async def test_instant_query_http_error_propagates(self):
        error_resp = httpx.Response(
            status_code=500,
            text="Internal Server Error",
            request=httpx.Request("GET", "http://test"),
        )

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = error_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            with pytest.raises(httpx.HTTPStatusError):
                await instant_query("up")


class TestRangeQuery:
    """Tests for prometheus_client.range_query()."""

    async def test_range_query_success(self):
        mock_data = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"__name__": "up"},
                        "values": [[1234567890, "1"], [1234567950, "1"]],
                    },
                ],
            },
        }
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await range_query("up", duration="1h", step="60s")

        assert len(result) == 1
        assert len(result[0]["values"]) == 2

    async def test_range_query_url_and_params(self):
        mock_data = {"status": "success", "data": {"result": []}}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await range_query("up", duration="6h", step="120s")

        call_args = instance.get.call_args
        assert call_args[0][0] == "http://localhost:19090/api/v1/query_range"
        params = call_args[1]["params"]
        assert params["query"] == "up"
        assert params["step"] == "120s"
        # start should be approximately end - 6h (21600s)
        start = float(params["start"])
        end = float(params["end"])
        assert abs((end - start) - 21600) < 1

    async def test_range_query_non_success_returns_empty(self):
        mock_data = {"status": "error", "error": "some error"}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = await range_query("bad_query")

        assert result == []

    async def test_range_query_default_duration_and_step(self):
        mock_data = {"status": "success", "data": {"result": []}}
        mock_resp = _mock_response(json_data=mock_data)

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = mock_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            await range_query("up")

        call_args = instance.get.call_args
        params = call_args[1]["params"]
        assert params["step"] == "60s"
        start = float(params["start"])
        end = float(params["end"])
        # Default duration is 1h = 3600s
        assert abs((end - start) - 3600) < 1

    async def test_range_query_http_error_propagates(self):
        error_resp = httpx.Response(
            status_code=502,
            text="Bad Gateway",
            request=httpx.Request("GET", "http://test"),
        )

        with patch("app.services.prometheus_client.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get.return_value = error_resp
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            with pytest.raises(httpx.HTTPStatusError):
                await range_query("up")


# ═══════════════════════════════════════════════════════════════════════════════
# audit: log_query
# ═══════════════════════════════════════════════════════════════════════════════

from app.services.audit import log_query


class TestLogQuery:
    """Tests for audit.log_query(). Uses engine/db_session fixtures from conftest."""

    async def test_log_query_creates_entry(self, db_session):
        entry = await log_query(
            db_session,
            user="alice",
            database_alias="prod-db",
            sql="SELECT * FROM users",
            row_count=42,
            elapsed_ms=150,
            status="success",
        )

        assert entry.id is not None
        assert entry.user == "alice"
        assert entry.database_alias == "prod-db"
        assert entry.sql == "SELECT * FROM users"
        assert entry.row_count == 42
        assert entry.elapsed_ms == 150
        assert entry.status == "success"
        assert entry.error_message is None
        assert entry.params is None

    async def test_log_query_error_status(self, db_session):
        entry = await log_query(
            db_session,
            user="bob",
            database_alias="staging-db",
            sql="SELECT * FROM nonexistent",
            status="error",
            error_message="relation 'nonexistent' does not exist",
        )

        assert entry.id is not None
        assert entry.user == "bob"
        assert entry.status == "error"
        assert entry.error_message == "relation 'nonexistent' does not exist"
        assert entry.row_count is None
        assert entry.elapsed_ms is None

    async def test_log_query_with_params(self, db_session):
        params = {"id": 123, "name": "test"}
        entry = await log_query(
            db_session,
            user="charlie",
            database_alias="dev-db",
            sql="SELECT * FROM users WHERE id = :id AND name = :name",
            params=params,
            row_count=1,
            elapsed_ms=5,
            status="success",
        )

        assert entry.params is not None
        parsed = json.loads(entry.params)
        assert parsed == {"id": 123, "name": "test"}

    async def test_log_query_none_params_stored_as_none(self, db_session):
        entry = await log_query(
            db_session,
            user="dave",
            database_alias="dev-db",
            sql="SELECT 1",
            params=None,
            row_count=1,
            elapsed_ms=1,
            status="success",
        )

        assert entry.params is None

    async def test_log_query_persisted_in_database(self, db_session):
        await log_query(
            db_session,
            user="eve",
            database_alias="analytics-db",
            sql="SELECT count(*) FROM events",
            row_count=1,
            elapsed_ms=300,
            status="success",
        )

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.user == "eve")
        )
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].database_alias == "analytics-db"
        assert rows[0].sql == "SELECT count(*) FROM events"

    async def test_log_query_multiple_entries(self, db_session):
        for i in range(3):
            await log_query(
                db_session,
                user="frank",
                database_alias="db",
                sql=f"SELECT {i}",
                status="success",
            )

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.user == "frank")
        )
        rows = result.scalars().all()
        assert len(rows) == 3

    async def test_log_query_empty_params_dict(self, db_session):
        entry = await log_query(
            db_session,
            user="grace",
            database_alias="db",
            sql="SELECT 1",
            params={},
            status="success",
        )

        # Empty dict is falsy in Python, so `json.dumps(params) if params`
        # evaluates to None
        assert entry.params is None

    async def test_log_query_complex_params(self, db_session):
        params = {"ids": [1, 2, 3], "nested": {"key": "value"}}
        entry = await log_query(
            db_session,
            user="heidi",
            database_alias="db",
            sql="SELECT 1",
            params=params,
            status="success",
        )

        parsed = json.loads(entry.params)
        assert parsed["ids"] == [1, 2, 3]
        assert parsed["nested"]["key"] == "value"

    async def test_log_query_long_sql(self, db_session):
        long_sql = "SELECT " + ", ".join([f"col_{i}" for i in range(200)]) + " FROM big_table"
        entry = await log_query(
            db_session,
            user="ivan",
            database_alias="db",
            sql=long_sql,
            status="success",
        )

        assert entry.sql == long_sql

    async def test_log_query_zero_row_count_and_elapsed(self, db_session):
        entry = await log_query(
            db_session,
            user="judy",
            database_alias="db",
            sql="DELETE FROM empty_table WHERE 1=0",
            row_count=0,
            elapsed_ms=0,
            status="success",
        )

        assert entry.row_count == 0
        assert entry.elapsed_ms == 0
