"""Tests for API Keys CRUD router."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routers.api_keys import DENY_ALL_CONSUMER, sync_all_consumer_route_restrictions
from app.schemas import QueryResponse
from tests.conftest import auth_header

VALID_API_KEY = "Abcdefghijklmnopqrstuvwxyz123456"
VALID_API_KEY_2 = "Bcdefghijklmnopqrstuvwxyz1234567"

ROUTE_FIXTURES = {
    "items": [
        {
            "id": "query-api",
            "uri": "/query/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
        {
            "id": "llm-proxy",
            "uri": "/llm/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
    ]
}


@pytest.mark.asyncio
async def test_sync_all_consumer_route_restrictions_replays_stored_allowed_routes():
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(
            all=lambda: [
                SimpleNamespace(
                    consumer_name="limited-app",
                    allowed_routes=json.dumps(["llm-proxy"]),
                ),
                SimpleNamespace(
                    consumer_name="deny-app",
                    allowed_routes=None,
                ),
            ]
        )
    )

    route_state = {
        route["id"]: json.loads(json.dumps(route))
        for route in ROUTE_FIXTURES["items"]
    }

    async def list_resources(resource_type):
        assert resource_type == "routes"
        return {"items": list(route_state.values())}

    async def put_resource(resource_type, resource_id, body):
        assert resource_type == "routes"
        route_state[resource_id] = {"id": resource_id, **body}

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)

        await sync_all_consumer_route_restrictions(db)

    db.execute.assert_awaited_once()
    query = db.execute.await_args.args[0]
    assert "ORDER BY api_key_access.consumer_name ASC" in str(query)

    assert route_state["llm-proxy"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["limited-app"]
    }
    assert route_state["query-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": [DENY_ALL_CONSUMER]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_allowed_routes",
    [
        "not-json",
        json.dumps("llm-proxy"),
        json.dumps({"route": "llm-proxy"}),
        json.dumps(["llm-proxy", 1]),
    ],
)
async def test_sync_all_consumer_route_restrictions_skips_malformed_allowed_routes(
    malformed_allowed_routes,
):
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(
            all=lambda: [
                SimpleNamespace(
                    consumer_name="bad-app",
                    allowed_routes=malformed_allowed_routes,
                ),
                SimpleNamespace(
                    consumer_name="good-app",
                    allowed_routes=json.dumps(["llm-proxy"]),
                ),
            ]
        )
    )

    route_state = {
        route["id"]: json.loads(json.dumps(route))
        for route in ROUTE_FIXTURES["items"]
    }

    async def list_resources(resource_type):
        assert resource_type == "routes"
        return {"items": list(route_state.values())}

    async def put_resource(resource_type, resource_id, body):
        assert resource_type == "routes"
        route_state[resource_id] = {"id": resource_id, **body}

    with (
        patch("app.routers.api_keys.apisix_client") as mock_apisix,
        patch("app.routers.api_keys.logger.warning") as logger_warning,
    ):
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)

        await sync_all_consumer_route_restrictions(db)

    logger_warning.assert_called_once_with(
        "Skipping malformed allowed_routes for consumer '%s' during startup replay",
        "bad-app",
    )
    assert route_state["llm-proxy"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["good-app"]
    }
    assert route_state["query-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": [DENY_ALL_CONSUMER]
    }


@pytest.mark.asyncio
async def test_list_api_keys_empty(client, admin_token):
    resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_api_keys_requires_permission(client, viewer_token):
    resp = await client.get("/admin/api-keys", headers=auth_header(viewer_token))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_api_key(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "test-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "test-app",
                "description": "Test application",
                "api_key": VALID_API_KEY,
                "allowed_databases": ["mydb"],
                "allowed_routes": [],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-app"
        assert data["description"] == "Test application"
        assert data["api_key"] == VALID_API_KEY
        assert data["key_created"] is True
        assert data["allowed_databases"] == ["mydb"]

        route_calls = {
            call.args[1]: call.args[2]
            for call in mock_apisix.put_resource.await_args_list
            if call.args[0] == "routes"
        }
        assert route_calls["query-api"]["plugins"]["consumer-restriction"] == {
            "whitelist": [DENY_ALL_CONSUMER]
        }
        assert route_calls["llm-proxy"]["plugins"]["consumer-restriction"] == {
            "whitelist": [DENY_ALL_CONSUMER]
        }


@pytest.mark.asyncio
async def test_create_api_key_partial_routes_excludes_consumer_from_other_routes(client, admin_token):
    """Regression: a key with allowed_routes=["query-api"] must NOT appear
    in the llm-proxy whitelist.  Before the fix, the consumer-restriction
    plugin was removed entirely when the whitelist became empty, allowing
    any authenticated consumer to bypass route-level permissions."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "partial-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "partial-app",
                "api_key": VALID_API_KEY,
                "allowed_routes": ["query-api"],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201

        route_calls = {
            call.args[1]: call.args[2]
            for call in mock_apisix.put_resource.await_args_list
            if call.args[0] == "routes"
        }
        # query-api must whitelist this consumer
        assert "partial-app" in route_calls["query-api"]["plugins"]["consumer-restriction"]["whitelist"]
        # llm-proxy must NOT whitelist this consumer — sentinel blocks access
        llm_whitelist = route_calls["llm-proxy"]["plugins"]["consumer-restriction"]["whitelist"]
        assert "partial-app" not in llm_whitelist
        assert DENY_ALL_CONSUMER in llm_whitelist


@pytest.mark.asyncio
async def test_create_api_key_duplicate(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "dup-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        await client.post(
            "/admin/api-keys",
            json={"name": "dup-app", "api_key": VALID_API_KEY},
            headers=auth_header(admin_token),
        )
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "dup-app", "api_key": VALID_API_KEY_2},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_api_key_rejects_short_custom_key(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "weak-app", "api_key": "short-key-16"},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 422
    mock_apisix.put_resource.assert_not_called()


@pytest.mark.asyncio
async def test_create_api_key_rejects_reserved_deny_all_name(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        resp = await client.post(
            "/admin/api-keys",
            json={"name": DENY_ALL_CONSUMER, "api_key": VALID_API_KEY},
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 400
    assert resp.json()["detail"] == f"API key name '{DENY_ALL_CONSUMER}' is reserved"
    mock_apisix.put_resource.assert_not_called()


@pytest.mark.asyncio
async def test_update_api_key_access(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "update-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        await client.post(
            "/admin/api-keys",
            json={"name": "update-app", "api_key": VALID_API_KEY},
            headers=auth_header(admin_token),
        )

        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "update-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        resp = await client.put(
            "/admin/api-keys/update-app",
            json={"allowed_databases": ["db1", "db2"], "description": "Updated"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed_databases"] == ["db1", "db2"]
        assert data["description"] == "Updated"


@pytest.mark.asyncio
async def test_update_api_key_empty_allowed_routes_uses_deny_all_sentinel(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "deny-update-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={
            "items": [
                {
                    "id": "query-api",
                    "uri": "/query/*",
                    "plugins": {
                        "key-auth": {},
                        "consumer-restriction": {"whitelist": ["deny-update-app"]},
                    },
                },
                {
                    "id": "llm-proxy",
                    "uri": "/llm/*",
                    "plugins": {
                        "key-auth": {},
                        "consumer-restriction": {"whitelist": ["other-consumer", "deny-update-app"]},
                    },
                },
            ]
        })

        await client.post(
            "/admin/api-keys",
            json={"name": "deny-update-app", "api_key": VALID_API_KEY, "allowed_routes": ["query-api", "llm-proxy"]},
            headers=auth_header(admin_token),
        )

        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "deny-update-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        resp = await client.put(
            "/admin/api-keys/deny-update-app",
            json={"allowed_routes": []},
            headers=auth_header(admin_token),
        )

        assert resp.status_code == 200

        route_calls = [
            call.args for call in mock_apisix.put_resource.await_args_list if call.args[0] == "routes"
        ]
        latest_route_calls = {route_id: body for _, route_id, body in route_calls}
        assert latest_route_calls["query-api"]["plugins"]["consumer-restriction"] == {
            "whitelist": [DENY_ALL_CONSUMER]
        }
        assert latest_route_calls["llm-proxy"]["plugins"]["consumer-restriction"] == {
            "whitelist": ["other-consumer"]
        }


@pytest.mark.asyncio
async def test_update_api_key_moves_consumer_between_allowed_routes(client, admin_token):
    route_state = {
        route["id"]: json.loads(json.dumps(route))
        for route in ROUTE_FIXTURES["items"]
    }

    async def list_resources(resource_type):
        assert resource_type == "routes"
        return {"items": list(route_state.values())}

    async def put_resource(resource_type, resource_id, body):
        if resource_type == "routes":
            route_state[resource_id] = {"id": resource_id, **body}
            return route_state[resource_id]
        return {"username": resource_id, **body}

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)

        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "moving-app",
                "api_key": VALID_API_KEY,
                "allowed_routes": ["query-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "moving-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        update_resp = await client.put(
            "/admin/api-keys/moving-app",
            json={"allowed_routes": ["llm-proxy"]},
            headers=auth_header(admin_token),
        )

    assert update_resp.status_code == 200
    assert route_state["query-api"]["plugins"]["consumer-restriction"]["whitelist"] == [
        DENY_ALL_CONSUMER
    ]
    assert route_state["llm-proxy"]["plugins"]["consumer-restriction"]["whitelist"] == [
        "moving-app"
    ]


@pytest.mark.asyncio
async def test_delete_api_key(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "del-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        await client.post(
            "/admin/api-keys",
            json={"name": "del-app", "api_key": VALID_API_KEY},
            headers=auth_header(admin_token),
        )
        resp = await client.delete(
            "/admin/api-keys/del-app",
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 204

        resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))
        assert all(k["name"] != "del-app" for k in resp.json())


@pytest.mark.asyncio
async def test_query_execute_via_apikey_header(client, admin_token):
    """Simulate APISIX-forwarded request with X-Consumer-Username."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "query-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        await client.post(
            "/admin/api-keys",
            json={"name": "query-app", "api_key": VALID_API_KEY, "allowed_databases": ["testdb"]},
            headers=auth_header(admin_token),
        )

    # APISIX-forwarded request (no Bearer token, just header)
    resp = await client.post(
        "/query/execute",
        json={"database": "testdb", "sql": "SELECT 1"},
        headers={"X-Consumer-Username": "query-app"},
    )
    # 404 because "testdb" engine doesn't exist in connection_manager, but auth passes
    assert resp.status_code == 404
    assert "not registered" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_query_execute_apikey_allowed_db_select_returns_200(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "allowed-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "allowed-app",
                "api_key": VALID_API_KEY,
                "allowed_databases": ["allowed-db"],
                "allowed_routes": ["query-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    mock_engine = MagicMock()
    query_response = QueryResponse(
        columns=["ok"],
        rows=[[1]],
        row_count=1,
        truncated=False,
        elapsed_ms=7,
    )
    with patch(
        "app.routers.query.connection_manager.get_engine",
        return_value=mock_engine,
    ), patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="postgres",
    ), patch(
        "app.routers.query.execute_query",
        new_callable=AsyncMock,
        return_value=query_response,
    ) as mock_execute_query, patch(
        "app.routers.query.log_query",
        new_callable=AsyncMock,
    ):
        resp = await client.post(
            "/query/execute",
            json={"database": "allowed-db", "sql": "SELECT 1"},
            headers={"X-Consumer-Username": "allowed-app"},
        )

    assert resp.status_code == 200
    assert resp.json()["row_count"] == 1
    mock_execute_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_execute_apikey_allowed_db_rejects_insert(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "readonly-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "readonly-app",
                "api_key": VALID_API_KEY,
                "allowed_databases": ["allowed-db"],
                "allowed_routes": ["query-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    with patch(
        "app.routers.query.connection_manager.get_engine",
        return_value=MagicMock(),
    ), patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="postgres",
    ):
        resp = await client.post(
            "/query/execute",
            json={"database": "allowed-db", "sql": "INSERT INTO audit_logs (id) VALUES (1)"},
            headers={"X-Consumer-Username": "readonly-app"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "API key users can only execute SELECT queries"


@pytest.mark.asyncio
async def test_query_execute_apikey_db_not_allowed(client, admin_token):
    """API key user cannot query databases not in their allowed list."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "restricted-app",
            "plugins": {"key-auth": {"key": VALID_API_KEY}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        await client.post(
            "/admin/api-keys",
            json={"name": "restricted-app", "api_key": VALID_API_KEY, "allowed_databases": ["allowed-db"]},
            headers=auth_header(admin_token),
        )

    resp = await client.post(
        "/query/execute",
        json={"database": "forbidden-db", "sql": "SELECT 1"},
        headers={"X-Consumer-Username": "restricted-app"},
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()
