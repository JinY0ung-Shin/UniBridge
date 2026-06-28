"""Tests for administrative change auditing (AdminAuditLog).

Covers four things:
  * ``redact_snapshot`` — secret masking in before/after snapshots.
  * Wiring: gateway route/upstream and API-key mutations write audit rows.
  * The ``GET /admin/audit-logs`` read endpoint (filters + permission gate).
  * The best-effort guarantee: a failed audit write must not break the mutation.

Each test starts with a fresh in-memory DB (the ``engine`` fixture is
function-scoped), so audit-row counts are asserted absolutely.
"""
from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import httpx

from app.services.audit import redact_snapshot
from tests.conftest import auth_header


def _http_status(code: int, body: str = "not found") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://apisix")
    res = httpx.Response(code, request=req, text=body)
    return httpx.HTTPStatusError("err", request=req, response=res)


# Route inventory the API-key consumer-restriction sync iterates over.
ROUTE_FIXTURES = {
    "items": [
        {
            "id": "query-api",
            "uri": "/query/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
    ]
}

GW = "app.routers.gateway.apisix_client"


async def _audit_logs(client, token, **params):
    resp = await client.get(
        "/admin/audit-logs", params=params, headers=auth_header(token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── redact_snapshot ──────────────────────────────────────────────────────────


class TestRedactSnapshot:
    def test_masks_key_auth_key(self):
        out = redact_snapshot({"plugins": {"key-auth": {"key": "supersecretkey1234"}}})
        assert out["plugins"]["key-auth"]["key"] == "***1234"

    def test_masks_proxy_rewrite_header_set_values(self):
        snap = {
            "plugins": {"proxy-rewrite": {"headers": {"set": {"X-Custom": "abcdef123456"}}}}
        }
        out = redact_snapshot(snap)
        # Arbitrary header names under headers.set hold upstream service secrets.
        assert out["plugins"]["proxy-rewrite"]["headers"]["set"]["X-Custom"] == "***3456"

    def test_masks_service_keys_header_value_but_not_name(self):
        snap = {"service_keys": [{"header_name": "X-Key", "header_value": "longsecret9999"}]}
        out = redact_snapshot(snap)
        assert out["service_keys"][0]["header_value"] == "***9999"
        assert out["service_keys"][0]["header_name"] == "X-Key"

    def test_masks_api_key_and_secret_fields(self):
        out = redact_snapshot({"api_key": "key-abcdef1234", "secret": "topsecret9090"})
        assert out["api_key"] == "***1234"
        assert out["secret"] == "***9090"

    def test_short_secret_fully_masked(self):
        assert redact_snapshot({"key": "ab"})["key"] == "***"

    def test_preserves_non_secret_fields(self):
        snap = {"uri": "/api/x/*", "upstream_id": "u1", "name": "myroute", "enabled": True}
        assert redact_snapshot(snap) == snap

    def test_does_not_mutate_input(self):
        snap = {"plugins": {"key-auth": {"key": "supersecretkey1234"}}}
        redact_snapshot(snap)
        assert snap["plugins"]["key-auth"]["key"] == "supersecretkey1234"

    def test_non_dict_passthrough(self):
        assert redact_snapshot(None) is None
        assert redact_snapshot("plain") == "plain"


# ── Gateway route auditing ─────────────────────────────────────────────────────


class TestRouteAuditing:
    async def test_create_route_writes_masked_audit(self, client, admin_token):
        saved = {
            "id": "r1",
            "uri": "/api/test/*",
            "upstream_id": "u1",
            "plugins": {
                "proxy-rewrite": {"headers": {"set": {"X-Key": "supersecret123456"}}}
            },
        }
        with (
            patch(f"{GW}.get_resource", new_callable=AsyncMock, side_effect=_http_status(404)),
            patch(f"{GW}.put_resource", new_callable=AsyncMock, return_value=deepcopy(saved)),
        ):
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={
                    "uri": "/api/test/*",
                    "upstream_id": "u1",
                    "service_keys": [
                        {"header_name": "X-Key", "header_value": "supersecret123456"}
                    ],
                },
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200

        logs = await _audit_logs(client, admin_token, resource_type="route")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["actor"] == "testadmin"
        assert entry["action"] == "create"
        assert entry["resource_id"] == "r1"
        assert entry["summary"] == "/api/test/*"
        assert entry["before"] is None
        after = json.loads(entry["after"])
        assert after["plugins"]["proxy-rewrite"]["headers"]["set"]["X-Key"] == "***3456"
        assert "supersecret123456" not in entry["after"]

    async def test_update_route_records_before(self, client, admin_token):
        existing = {"id": "r2", "uri": "/api/old/*", "upstream_id": "u1", "plugins": {}}
        saved = {"id": "r2", "uri": "/api/new/*", "upstream_id": "u2", "plugins": {}}
        with (
            patch(f"{GW}.get_resource", new_callable=AsyncMock, return_value=deepcopy(existing)),
            patch(f"{GW}.put_resource", new_callable=AsyncMock, return_value=deepcopy(saved)),
        ):
            resp = await client.put(
                "/admin/gateway/routes/r2",
                json={"uri": "/api/new/*", "upstream_id": "u2"},
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200

        logs = await _audit_logs(client, admin_token, resource_type="route", action="update")
        assert len(logs) == 1
        assert logs[0]["action"] == "update"
        assert json.loads(logs[0]["before"])["uri"] == "/api/old/*"
        assert json.loads(logs[0]["after"])["uri"] == "/api/new/*"

    async def test_delete_route_writes_audit(self, client, admin_token):
        existing = {"id": "r3", "uri": "/api/del/*", "upstream_id": "u1", "plugins": {}}
        with (
            patch(f"{GW}.get_resource", new_callable=AsyncMock, return_value=deepcopy(existing)),
            patch(f"{GW}.delete_resource", new_callable=AsyncMock),
        ):
            resp = await client.delete(
                "/admin/gateway/routes/r3", headers=auth_header(admin_token)
            )
            assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="route", action="delete")
        assert len(logs) == 1
        assert logs[0]["resource_id"] == "r3"
        assert logs[0]["summary"] == "/api/del/*"
        assert logs[0]["after"] is None
        assert json.loads(logs[0]["before"])["uri"] == "/api/del/*"


# ── Gateway upstream auditing ───────────────────────────────────────────────────


class TestUpstreamAuditing:
    async def test_create_upstream_writes_audit(self, client, admin_token):
        saved = {"id": "u9", "name": "my-up"}
        with (
            patch(f"{GW}.get_resource", new_callable=AsyncMock, side_effect=_http_status(404)),
            patch(f"{GW}.put_resource", new_callable=AsyncMock, return_value=deepcopy(saved)),
        ):
            resp = await client.put(
                "/admin/gateway/upstreams/u9",
                json={"name": "my-up", "type": "roundrobin", "nodes": {"h:80": 1}},
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200

        logs = await _audit_logs(client, admin_token, resource_type="upstream", action="create")
        assert len(logs) == 1
        assert logs[0]["resource_id"] == "u9"
        assert logs[0]["summary"] == "my-up"
        assert logs[0]["before"] is None

    async def test_delete_upstream_writes_audit(self, client, admin_token):
        existing = {"id": "u8", "name": "old-up"}
        with (
            patch(f"{GW}.get_resource", new_callable=AsyncMock, return_value=deepcopy(existing)),
            patch(f"{GW}.delete_resource", new_callable=AsyncMock),
        ):
            resp = await client.delete(
                "/admin/gateway/upstreams/u8", headers=auth_header(admin_token)
            )
            assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="upstream", action="delete")
        assert len(logs) == 1
        assert logs[0]["resource_id"] == "u8"
        assert logs[0]["summary"] == "old-up"
        assert logs[0]["after"] is None


# ── API-key auditing ───────────────────────────────────────────────────────────


class TestApiKeyAuditing:
    async def test_create_api_key_writes_masked_audit(self, client, admin_token):
        with patch("app.routers.api_keys.apisix_client") as mock_apisix:
            mock_apisix.put_resource = AsyncMock(
                return_value={"username": "audit-app", "plugins": {"key-auth": {"key": "key-secret9999"}}}
            )
            mock_apisix.patch_resource = mock_apisix.put_resource
            mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
            mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
            resp = await client.post(
                "/admin/api-keys",
                json={
                    "name": "audit-app",
                    "description": "billing service",
                    "api_key": "key-secret9999",
                    "allowed_databases": ["mydb"],
                    "allowed_routes": [],
                },
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 201

        logs = await _audit_logs(client, admin_token, resource_type="api_key", action="create")
        assert len(logs) == 1
        entry = logs[0]
        assert entry["resource_id"] == "audit-app"
        assert entry["summary"] == "billing service"
        assert entry["before"] is None
        after = json.loads(entry["after"])
        assert after["api_key"] == "***9999"
        assert "key-secret9999" not in entry["after"]

    async def test_delete_api_key_writes_audit(self, client, admin_token):
        with patch("app.routers.api_keys.apisix_client") as mock_apisix:
            mock_apisix.put_resource = AsyncMock(
                return_value={"username": "del-audit", "plugins": {"key-auth": {"key": "key-d2"}}}
            )
            mock_apisix.patch_resource = mock_apisix.put_resource
            mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
            mock_apisix.delete_resource = AsyncMock()
            mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
            await client.post(
                "/admin/api-keys",
                json={"name": "del-audit", "description": "temp", "api_key": "key-d2"},
                headers=auth_header(admin_token),
            )
            resp = await client.delete(
                "/admin/api-keys/del-audit", headers=auth_header(admin_token)
            )
            assert resp.status_code == 204

        logs = await _audit_logs(client, admin_token, resource_type="api_key", action="delete")
        assert len(logs) == 1
        assert logs[0]["resource_id"] == "del-audit"
        assert logs[0]["after"] is None
        assert json.loads(logs[0]["before"])["name"] == "del-audit"


# ── Read endpoint: filters + permission gate ────────────────────────────────────


class TestAdminAuditEndpoint:
    async def test_requires_admin_audit_read_permission(self, client, user_token):
        resp = await client.get("/admin/audit-logs", headers=auth_header(user_token))
        assert resp.status_code == 403

    async def test_filters_by_actor_and_resource_type(self, client, admin_token):
        route = {"id": "r1", "uri": "/api/r/*", "upstream_id": "u1", "plugins": {}}
        upstream = {"id": "u1", "name": "up"}
        with (
            patch(f"{GW}.get_resource", new_callable=AsyncMock, side_effect=_http_status(404)),
            patch(f"{GW}.put_resource", new_callable=AsyncMock, return_value=deepcopy(route)),
        ):
            await client.put(
                "/admin/gateway/routes/r1",
                json={"uri": "/api/r/*", "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
        with (
            patch(f"{GW}.get_resource", new_callable=AsyncMock, side_effect=_http_status(404)),
            patch(f"{GW}.put_resource", new_callable=AsyncMock, return_value=deepcopy(upstream)),
        ):
            await client.put(
                "/admin/gateway/upstreams/u1",
                json={"name": "up", "type": "roundrobin", "nodes": {"h:80": 1}},
                headers=auth_header(admin_token),
            )

        assert len(await _audit_logs(client, admin_token)) == 2
        route_logs = await _audit_logs(client, admin_token, resource_type="route")
        assert len(route_logs) == 1 and route_logs[0]["resource_type"] == "route"
        up_logs = await _audit_logs(client, admin_token, resource_type="upstream")
        assert len(up_logs) == 1 and up_logs[0]["resource_type"] == "upstream"
        assert len(await _audit_logs(client, admin_token, actor="testadmin")) == 2
        assert await _audit_logs(client, admin_token, actor="nobody") == []

    async def test_audit_write_failure_does_not_break_mutation(self, client, admin_token):
        """A failure inside log_admin_action is swallowed — the route still saves."""
        saved = {"id": "rok", "uri": "/api/ok/*", "upstream_id": "u1", "plugins": {}}
        with (
            patch(f"{GW}.get_resource", new_callable=AsyncMock, side_effect=_http_status(404)),
            patch(f"{GW}.put_resource", new_callable=AsyncMock, return_value=deepcopy(saved)),
            patch("app.services.audit.async_sessionmaker", side_effect=RuntimeError("audit db down")),
        ):
            resp = await client.put(
                "/admin/gateway/routes/rok",
                json={"uri": "/api/ok/*", "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
            assert resp.status_code == 200

        # The audit write failed and was swallowed, so no row was persisted.
        assert await _audit_logs(client, admin_token, resource_type="route") == []
