"""Tests for the gateway OpenAPI spec publishing (app/services/openapi_export.py)."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import QueryTemplate
from app.services.openapi_export import (
    SECURITY_SCHEME_NAME,
    build_openapi_spec,
    extract_template_params,
)
from tests.conftest import auth_header


def _template(**overrides) -> SimpleNamespace:
    base = {
        "path": "reports/users",
        "name": "Users report",
        "description": "List users",
        "db_alias": "maindb",
        "sql": "SELECT id, name FROM users WHERE id = :id",
        "default_limit": 50,
        "timeout": 30,
        "enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(routes=None, templates=None):
    return build_openapi_spec(
        routes or [],
        templates or [],
        server_url="https://gw.example:3000",
        version="1.0.0",
    )


# ---------------------------------------------------------------------------
# Document shape
# ---------------------------------------------------------------------------


class TestDocumentShape:
    def test_basic_openapi_structure(self):
        spec = _build()
        assert spec["openapi"] == "3.0.3"
        assert spec["info"]["title"] == "UniBridge Gateway API"
        assert spec["info"]["version"] == "1.0.0"
        assert isinstance(spec["paths"], dict)
        assert SECURITY_SCHEME_NAME in spec["components"]["securitySchemes"]

    def test_security_scheme_uses_apikey_header(self):
        scheme = _build()["components"]["securitySchemes"][SECURITY_SCHEME_NAME]
        assert scheme["type"] == "apiKey"
        assert scheme["in"] == "header"
        assert scheme["name"] == "apikey"

    def test_servers_from_argument(self):
        spec = _build()
        assert spec["servers"][0]["url"] == "https://gw.example:3000"


# ---------------------------------------------------------------------------
# Route → path mapping
# ---------------------------------------------------------------------------


class TestRouteMapping:
    def test_exact_uri_and_methods(self):
        route = {
            "id": "r1",
            "name": "my route",
            "desc": "does things",
            "uri": "/api/v1/test",
            "methods": ["GET", "POST"],
            "plugins": {},
        }
        paths = _build(routes=[route])["paths"]
        assert set(paths["/api/v1/test"].keys()) == {"get", "post"}
        op = paths["/api/v1/test"]["get"]
        assert op["summary"] == "my route"
        assert op["description"] == "does things"

    def test_wildcard_uri_becomes_path_parameter(self):
        route = {"id": "r1", "uri": "/api/foo/*", "methods": ["GET"], "plugins": {}}
        paths = _build(routes=[route])["paths"]
        assert "/api/foo/{path}" in paths
        params = paths["/api/foo/{path}"]["get"]["parameters"]
        assert params[0]["name"] == "path"
        assert params[0]["in"] == "path"
        assert params[0]["required"] is True

    def test_missing_methods_defaults_to_get(self):
        route = {"id": "r1", "uri": "/api/bare", "plugins": {}}
        paths = _build(routes=[route])["paths"]
        assert list(paths["/api/bare"].keys()) == ["get"]

    def test_unsupported_methods_are_dropped(self):
        route = {"id": "r1", "uri": "/api/x", "methods": ["GET", "PURGE"], "plugins": {}}
        paths = _build(routes=[route])["paths"]
        assert list(paths["/api/x"].keys()) == ["get"]

    def test_builtin_vs_custom_tags(self):
        routes = [
            {"id": "query-api", "uri": "/api/query/*", "methods": ["POST"], "plugins": {}},
            {"id": "my-route", "uri": "/api/custom", "methods": ["GET"], "plugins": {}},
        ]
        paths = _build(routes=routes)["paths"]
        assert "built-in" in paths["/api/query/{path}"]["post"]["tags"]
        assert "custom" in paths["/api/custom"]["get"]["tags"]

    def test_labels_become_tags(self):
        route = {
            "id": "r1",
            "uri": "/api/x",
            "methods": ["GET"],
            "labels": {"env": "prod"},
            "plugins": {},
        }
        paths = _build(routes=[route])["paths"]
        assert "env:prod" in paths["/api/x"]["get"]["tags"]

    def test_route_without_uri_is_skipped(self):
        assert _build(routes=[{"id": "r1", "plugins": {}}])["paths"] == {}

    def test_uris_list_is_supported(self):
        route = {"id": "r1", "uris": ["/api/a", "/api/b/*"], "methods": ["GET"], "plugins": {}}
        paths = _build(routes=[route])["paths"]
        assert "/api/a" in paths
        assert "/api/b/{path}" in paths


# ---------------------------------------------------------------------------
# key-auth security application
# ---------------------------------------------------------------------------


class TestKeyAuthSecurity:
    def test_key_auth_route_gets_security(self):
        route = {"id": "r1", "uri": "/api/x", "methods": ["GET"], "plugins": {"key-auth": {}}}
        op = _build(routes=[route])["paths"]["/api/x"]["get"]
        assert op["security"] == [{SECURITY_SCHEME_NAME: []}]

    def test_open_route_has_no_security(self):
        route = {"id": "r1", "uri": "/api/x", "methods": ["GET"], "plugins": {}}
        op = _build(routes=[route])["paths"]["/api/x"]["get"]
        assert "security" not in op


# ---------------------------------------------------------------------------
# Secret non-leakage
# ---------------------------------------------------------------------------


class TestSecretNonLeakage:
    def test_proxy_rewrite_secret_headers_never_appear(self):
        secret = "super-secret-upstream-key-12345"
        route = {
            "id": "llm-proxy",
            "uri": "/api/llm/*",
            "methods": ["POST"],
            "plugins": {
                "key-auth": {},
                "proxy-rewrite": {
                    "regex_uri": ["^/api/llm(.*)", "$1"],
                    "headers": {"set": {"Authorization": f"Bearer {secret}"}},
                },
            },
        }
        spec = _build(routes=[route])
        dumped = json.dumps(spec)
        assert secret not in dumped
        assert "proxy-rewrite" not in dumped

    def test_consumer_key_never_appears(self):
        route = {
            "id": "r1",
            "uri": "/api/x",
            "methods": ["GET"],
            "plugins": {"key-auth": {"key": "leaked-consumer-key"}},
        }
        assert "leaked-consumer-key" not in json.dumps(_build(routes=[route]))


# ---------------------------------------------------------------------------
# Query template → operation mapping
# ---------------------------------------------------------------------------


class TestTemplateMapping:
    def test_enabled_template_becomes_post_operation(self):
        spec = _build(templates=[_template()])
        op = spec["paths"]["/api/query/templates/reports/users"]["post"]
        assert op["summary"] == "Users report"
        assert "query-template" in op["tags"]
        assert op["security"] == [{SECURITY_SCHEME_NAME: []}]
        assert "maindb" in op["description"]

    def test_disabled_template_is_excluded(self):
        spec = _build(templates=[_template(enabled=False)])
        assert spec["paths"] == {}

    def test_bind_params_mapped_to_request_body(self):
        template = _template(sql="SELECT * FROM t WHERE a = :a AND b = :b")
        op = _build(templates=[template])["paths"]["/api/query/templates/reports/users"]["post"]
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        params_schema = schema["properties"]["params"]
        assert set(params_schema["properties"].keys()) == {"a", "b"}
        assert params_schema["required"] == ["a", "b"]
        assert op["requestBody"]["required"] is True
        assert "limit" in schema["properties"]
        assert "timeout" in schema["properties"]

    def test_template_without_params_has_optional_body(self):
        template = _template(sql="SELECT 1")
        op = _build(templates=[template])["paths"]["/api/query/templates/reports/users"]["post"]
        assert op["requestBody"]["required"] is False


class TestExtractTemplateParams:
    def test_named_binds(self):
        assert extract_template_params("SELECT :a, :b WHERE x = :a") == ["a", "b"]

    def test_postgres_cast_is_not_a_param(self):
        assert extract_template_params("SELECT id::text FROM t WHERE id = :id") == ["id"]

    def test_no_params(self):
        assert extract_template_params("SELECT 1") == []


# ---------------------------------------------------------------------------
# Endpoint tests (mock apisix_client, real DB session for templates)
# ---------------------------------------------------------------------------

MOCK_ROUTES = {
    "items": [
        {
            "id": "query-api",
            "name": "query-api",
            "uri": "/api/query/*",
            "methods": ["POST", "GET"],
            "plugins": {
                "key-auth": {},
                "proxy-rewrite": {
                    "regex_uri": ["^/api/query(.*)", "/query$1"],
                    "headers": {"set": {"X-Internal-Secret": "do-not-leak-9999"}},
                },
            },
        },
        {"id": "open-route", "uri": "/api/open", "methods": ["GET"], "plugins": {}},
    ],
    "total": 2,
}


class TestGatewayOpenapiEndpoint:
    async def test_returns_spec_with_routes_and_templates(self, client, admin_token, seeded_db):
        session_factory = async_sessionmaker(
            seeded_db, class_=AsyncSession, expire_on_commit=False
        )
        async with session_factory() as db:
            db.add(
                QueryTemplate(
                    path="reports/users",
                    name="Users report",
                    description="List users",
                    db_alias="maindb",
                    sql="SELECT id FROM users WHERE id = :id",
                )
            )
            db.add(
                QueryTemplate(
                    path="reports/disabled",
                    name="Disabled report",
                    db_alias="maindb",
                    sql="SELECT 1",
                    enabled=False,
                )
            )
            await db.commit()

        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value=deepcopy(MOCK_ROUTES),
        ):
            resp = await client.get(
                "/admin/gateway/openapi.json", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["openapi"] == "3.0.3"
        assert spec["info"]["title"] == "UniBridge Gateway API"
        assert SECURITY_SCHEME_NAME in spec["components"]["securitySchemes"]
        assert "/api/query/{path}" in spec["paths"]
        assert "/api/open" in spec["paths"]
        assert "/api/query/templates/reports/users" in spec["paths"]
        assert "/api/query/templates/reports/disabled" not in spec["paths"]
        assert "do-not-leak-9999" not in resp.text

    async def test_unsupported_format_returns_400(self, client, admin_token):
        resp = await client.get(
            "/admin/gateway/openapi.json?format=yaml", headers=auth_header(admin_token)
        )
        assert resp.status_code == 400

    async def test_forbidden_without_permission(self, client, user_token):
        resp = await client.get(
            "/admin/gateway/openapi.json", headers=auth_header(user_token)
        )
        assert resp.status_code == 403

    async def test_unauthenticated_returns_401(self, client):
        resp = await client.get("/admin/gateway/openapi.json")
        assert resp.status_code == 401

    async def test_apisix_error_returns_502(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            side_effect=RuntimeError("down"),
        ):
            resp = await client.get(
                "/admin/gateway/openapi.json", headers=auth_header(admin_token)
            )
        assert resp.status_code == 502
