"""Tests for API Keys CRUD router."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routers.api_keys import DENY_ALL_CONSUMER, MASTER_ACCESS, sync_all_consumer_route_restrictions
from app.schemas import QueryResponse
from tests.conftest import auth_header

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
async def test_query_template_write_route_is_granted_independently(client, admin_token):
    route_state = {
        "query-api": {
            "id": "query-api", "uri": "/api/query/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
        "query-template-write-api": {
            "id": "query-template-write-api", "uri": "/api/query/templates/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
    }

    async def list_resources(resource_type):
        assert resource_type == "routes"
        return {"items": list(route_state.values())}

    async def put_resource(resource_type, resource_id, body):
        if resource_type == "routes":
            route_state[resource_id] = {"id": resource_id, **body}
        return body

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)
        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "template-editor",
                "api_key": "editor-key",
                "allowed_databases": ["maindb"],
                "allowed_routes": ["query-template-write-api"],
            },
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 201
    assert resp.json()["allowed_routes"] == ["query-template-write-api"]
    assert route_state["query-template-write-api"]["plugins"][
        "consumer-restriction"
    ] == {"whitelist": ["template-editor"]}
    assert route_state["query-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": [DENY_ALL_CONSUMER]
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
async def test_sync_consumer_restriction_grants_llm_messages_alongside_llm_proxy():
    """Granting ``llm-proxy`` implicitly grants the converter route ``llm-messages``.

    Existing stored keys list ``llm-proxy`` (the only LLM route that existed when
    they were created) — without the alias they would be denied on the new
    ``/api/llm/v1/messages`` route after the converter rolls out.
    """
    from app.routers.api_keys import _sync_consumer_restriction

    route_state = {
        "query-api": {
            "id": "query-api",
            "uri": "/query/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
        "llm-proxy": {
            "id": "llm-proxy",
            "uri": "/llm/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
        "llm-messages": {
            "id": "llm-messages",
            "uri": "/api/llm/v1/messages",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
        "llm-responses": {
            "id": "llm-responses",
            "uri": "/api/llm/v1/responses",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
    }

    async def list_resources(resource_type):
        return {"items": list(route_state.values())}

    async def put_resource(resource_type, resource_id, body):
        route_state[resource_id] = {"id": resource_id, **body}

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)

        await _sync_consumer_restriction(["llm-proxy"], "llm-user")

    # Granting llm-proxy whitelists the consumer on the proxy AND both converter
    # routes (llm-messages, llm-responses).
    assert route_state["llm-proxy"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["llm-user"]
    }
    assert route_state["llm-messages"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["llm-user"]
    }
    assert route_state["llm-responses"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["llm-user"]
    }
    # ...but a route the key was not granted stays deny-all.
    assert route_state["query-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": [DENY_ALL_CONSUMER]
    }


@pytest.mark.asyncio
async def test_sync_consumer_restriction_wildcard_grants_all_keyauth_routes():
    from app.routers.api_keys import _sync_consumer_restriction

    route_state = {
        "query-api": {
            "id": "query-api",
            "uri": "/query/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
        },
        "nas-api": {
            "id": "nas-api",
            "uri": "/api/nas/*",
            "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": [DENY_ALL_CONSUMER]}},
        },
        "public": {
            "id": "public",
            "uri": "/public/*",
            "plugins": {},
        },
    }

    async def list_resources(resource_type):
        return {"items": list(route_state.values())}

    async def put_resource(resource_type, resource_id, body):
        route_state[resource_id] = {"id": resource_id, **body}

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)

        await _sync_consumer_restriction([MASTER_ACCESS], "master-app")

    assert route_state["query-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["master-app"]
    }
    assert route_state["nas-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["master-app"]
    }
    assert "consumer-restriction" not in route_state["public"]["plugins"]


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
async def test_list_api_keys_requires_permission(client, user_token):
    resp = await client.get("/admin/api-keys", headers=auth_header(user_token))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_api_key(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "test-app",
            "plugins": {"key-auth": {"key": "key-abc123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "test-app",
                "description": "Test application",
                "api_key": "key-abc123",
                "allowed_databases": ["mydb"],
                "allowed_routes": [],
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-app"
        assert data["description"] == "Test application"
        assert data["api_key"] == "key-abc123"
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
async def test_create_master_api_key_stores_wildcards_and_grants_all_routes(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "master-app",
            "plugins": {"key-auth": {"key": "master-key"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "master-app",
                "api_key": "master-key",
                "is_master": True,
                "allowed_databases": ["ignored-db"],
                "allowed_routes": ["query-api"],
            },
            headers=auth_header(admin_token),
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["is_master"] is True
    assert data["allowed_databases"] == [MASTER_ACCESS]
    assert data["allowed_routes"] == [MASTER_ACCESS]

    route_calls = {
        call.args[1]: call.args[2]
        for call in mock_apisix.put_resource.await_args_list
        if call.args[0] == "routes"
    }
    assert route_calls["query-api"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["master-app"]
    }
    assert route_calls["llm-proxy"]["plugins"]["consumer-restriction"] == {
        "whitelist": ["master-app"]
    }


@pytest.mark.asyncio
async def test_create_api_key_rejects_reserved_dunder_name(client, admin_token):
    """Names wrapped in '__' are reserved for internal sentinels (e.g. the
    gateway-monitoring no-key sentinel '__no_self_api_key__'); a real key with
    such a name would leak its traffic to keyless self-scoped users."""
    resp = await client.post(
        "/admin/api-keys",
        json={"name": "__no_self_api_key__", "api_key": "key-x"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_api_key_partial_routes_excludes_consumer_from_other_routes(client, admin_token):
    """Regression: a key with allowed_routes=["query-api"] must NOT appear
    in the llm-proxy whitelist.  Before the fix, the consumer-restriction
    plugin was removed entirely when the whitelist became empty, allowing
    any authenticated consumer to bypass route-level permissions."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "partial-app",
            "plugins": {"key-auth": {"key": "pk-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "partial-app",
                "api_key": "pk-123",
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
            "plugins": {"key-auth": {"key": "key-1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        await client.post(
            "/admin/api-keys",
            json={"name": "dup-app", "api_key": "key-1"},
            headers=auth_header(admin_token),
        )
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "dup-app", "api_key": "key-2"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_api_key_rejects_reserved_deny_all_name(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        resp = await client.post(
            "/admin/api-keys",
            json={"name": DENY_ALL_CONSUMER, "api_key": "key-reserved"},
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
            "plugins": {"key-auth": {"key": "key-u1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        await client.post(
            "/admin/api-keys",
            json={"name": "update-app", "api_key": "key-u1"},
            headers=auth_header(admin_token),
        )

        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "update-app",
            "plugins": {"key-auth": {"key": "key-u1"}},
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
            "plugins": {"key-auth": {"key": "key-du1"}},
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
            json={"name": "deny-update-app", "api_key": "key-du1", "allowed_routes": ["query-api", "llm-proxy"]},
            headers=auth_header(admin_token),
        )

        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "deny-update-app",
            "plugins": {"key-auth": {"key": "key-du1"}},
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
                "api_key": "mv-123",
                "allowed_routes": ["query-api"],
            },
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "moving-app",
            "plugins": {"key-auth": {"key": "mv-123"}},
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
            "plugins": {"key-auth": {"key": "key-d1"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        await client.post(
            "/admin/api-keys",
            json={"name": "del-app", "api_key": "key-d1"},
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
            "plugins": {"key-auth": {"key": "qk-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        await client.post(
            "/admin/api-keys",
            json={"name": "query-app", "api_key": "qk-123",
                  "allowed_databases": ["testdb"], "allowed_routes": ["query-api"]},
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
            "plugins": {"key-auth": {"key": "ak-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "allowed-app",
                "api_key": "ak-123",
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
            "plugins": {"key-auth": {"key": "ro-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        create_resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "readonly-app",
                "api_key": "ro-123",
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
    assert resp.json()["detail"] == (
        "API key 'readonly-app' is not allowed to execute INSERT queries"
    )


@pytest.mark.asyncio
async def test_query_execute_apikey_db_not_allowed(client, admin_token):
    """API key user cannot query databases not in their allowed list."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "restricted-app",
            "plugins": {"key-auth": {"key": "rk-123"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        await client.post(
            "/admin/api-keys",
            json={"name": "restricted-app", "api_key": "rk-123",
                  "allowed_databases": ["allowed-db"], "allowed_routes": ["query-api"]},
            headers=auth_header(admin_token),
        )

    resp = await client.post(
        "/query/execute",
        json={"database": "forbidden-db", "sql": "SELECT 1"},
        headers={"X-Consumer-Username": "restricted-app"},
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()


# ── Per-key write permissions + table ACL ────────────────────────────────────

async def _create_key(client, admin_token, mock_apisix, body: dict):
    body = {"allowed_routes": ["query-api"], **body}
    mock_apisix.put_resource = AsyncMock(return_value={
        "username": body["name"],
        "plugins": {"key-auth": {"key": body.get("api_key", "k")}},
    })
    mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
    mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
    resp = await client.post("/admin/api-keys", json=body, headers=auth_header(admin_token))
    assert resp.status_code == 201
    return resp


@pytest.mark.asyncio
async def test_create_api_key_defaults_write_flags_off_and_no_expiry(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        resp = await _create_key(client, admin_token, mock_apisix, {
            "name": "plain-app",
            "api_key": "pk-1",
            "allowed_databases": ["mydb"],
        })
    data = resp.json()
    assert data["allow_insert"] is False
    assert data["allow_update"] is False
    assert data["allow_delete"] is False
    assert data["allowed_tables"] is None
    # Admin-created keys never expire.
    assert data["expires_at"] is None


@pytest.mark.asyncio
async def test_create_api_key_with_write_flags_and_allowed_tables(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        resp = await _create_key(client, admin_token, mock_apisix, {
            "name": "writer-app",
            "api_key": "wk-1",
            "allowed_databases": ["mydb"],
            "allow_insert": True,
            "allow_delete": True,
            "allowed_tables": ["orders", "users"],
        })
    data = resp.json()
    assert data["allow_insert"] is True
    assert data["allow_update"] is False
    assert data["allow_delete"] is True
    assert data["allowed_tables"] == ["orders", "users"]


@pytest.mark.asyncio
async def test_update_api_key_write_flags_and_clear_allowed_tables(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        await _create_key(client, admin_token, mock_apisix, {
            "name": "flagged-app",
            "api_key": "fk-1",
            "allowed_tables": ["orders"],
        })

        resp = await client.put(
            "/admin/api-keys/flagged-app",
            json={"allow_update": True},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        # Omitted fields stay untouched.
        assert data["allow_update"] is True
        assert data["allow_insert"] is False
        assert data["allowed_tables"] == ["orders"]

        # Explicit null clears the table restriction (all tables allowed).
        resp = await client.put(
            "/admin/api-keys/flagged-app",
            json={"allowed_tables": None},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["allowed_tables"] is None
        assert resp.json()["allow_update"] is True


@pytest.mark.asyncio
async def test_query_execute_apikey_insert_allowed_with_flag(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        await _create_key(client, admin_token, mock_apisix, {
            "name": "insert-app",
            "api_key": "ik-1",
            "allowed_databases": ["allowed-db"],
            "allowed_routes": ["query-api"],
            "allow_insert": True,
        })

    query_response = QueryResponse(
        columns=[], rows=[], row_count=1, truncated=False, elapsed_ms=3,
    )
    with patch(
        "app.routers.query.connection_manager.get_engine",
        return_value=MagicMock(),
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
            json={"database": "allowed-db", "sql": "INSERT INTO orders (id) VALUES (1)"},
            headers={"X-Consumer-Username": "insert-app"},
        )

    assert resp.status_code == 200
    mock_execute_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_execute_apikey_update_delete_rejected_without_flags(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        await _create_key(client, admin_token, mock_apisix, {
            "name": "insert-only-app",
            "api_key": "io-1",
            "allowed_databases": ["allowed-db"],
            "allow_insert": True,
        })

    with patch(
        "app.routers.query.connection_manager.get_engine",
        return_value=MagicMock(),
    ), patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="postgres",
    ):
        update_resp = await client.post(
            "/query/execute",
            json={"database": "allowed-db", "sql": "UPDATE orders SET id = 2"},
            headers={"X-Consumer-Username": "insert-only-app"},
        )
        delete_resp = await client.post(
            "/query/execute",
            json={"database": "allowed-db", "sql": "DELETE FROM orders WHERE id = 1"},
            headers={"X-Consumer-Username": "insert-only-app"},
        )
    assert update_resp.status_code == 403
    assert "UPDATE" in update_resp.json()["detail"]
    assert delete_resp.status_code == 403
    assert "DELETE" in delete_resp.json()["detail"]


@pytest.mark.asyncio
async def test_query_execute_apikey_ddl_rejected_even_with_all_flags(client, admin_token):
    """DDL is never allowed for API keys, even when every write flag is set."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        await _create_key(client, admin_token, mock_apisix, {
            "name": "full-write-app",
            "api_key": "fw-1",
            "allowed_databases": ["allowed-db"],
            "allow_insert": True,
            "allow_update": True,
            "allow_delete": True,
        })

    with patch(
        "app.routers.query.connection_manager.get_engine",
        return_value=MagicMock(),
    ), patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="postgres",
    ):
        resp = await client.post(
            "/query/execute",
            json={"database": "allowed-db", "sql": "DROP TABLE orders"},
            headers={"X-Consumer-Username": "full-write-app"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_query_execute_apikey_table_acl_blocks_unlisted_table(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        await _create_key(client, admin_token, mock_apisix, {
            "name": "tabled-app",
            "api_key": "tb-1",
            "allowed_databases": ["allowed-db"],
            "allowed_tables": ["orders"],
        })

    with patch(
        "app.routers.query.connection_manager.get_engine",
        return_value=MagicMock(),
    ), patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="postgres",
    ), patch(
        "app.routers.query.log_query",
        new_callable=AsyncMock,
    ):
        resp = await client.post(
            "/query/execute",
            json={"database": "allowed-db", "sql": "SELECT * FROM secrets"},
            headers={"X-Consumer-Username": "tabled-app"},
        )
    assert resp.status_code == 403
    assert "secrets" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_query_execute_apikey_table_acl_applies_to_writes(client, admin_token):
    """The table whitelist gates INSERT/UPDATE/DELETE too, not only SELECT."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        await _create_key(client, admin_token, mock_apisix, {
            "name": "tabled-writer-app",
            "api_key": "tw-1",
            "allowed_databases": ["allowed-db"],
            "allow_insert": True,
            "allowed_tables": ["orders"],
        })

    query_response = QueryResponse(
        columns=[], rows=[], row_count=1, truncated=False, elapsed_ms=3,
    )
    with patch(
        "app.routers.query.connection_manager.get_engine",
        return_value=MagicMock(),
    ), patch(
        "app.routers.query.connection_manager.get_db_type",
        return_value="postgres",
    ), patch(
        "app.routers.query.execute_query",
        new_callable=AsyncMock,
        return_value=query_response,
    ), patch(
        "app.routers.query.log_query",
        new_callable=AsyncMock,
    ):
        blocked = await client.post(
            "/query/execute",
            json={"database": "allowed-db", "sql": "INSERT INTO secrets (id) VALUES (1)"},
            headers={"X-Consumer-Username": "tabled-writer-app"},
        )
        allowed = await client.post(
            "/query/execute",
            json={"database": "allowed-db", "sql": "INSERT INTO orders (id) VALUES (1)"},
            headers={"X-Consumer-Username": "tabled-writer-app"},
        )
    assert blocked.status_code == 403
    assert "secrets" in blocked.json()["detail"]
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_query_execute_apikey_expired_key_returns_401(client, admin_token, seeded_db):
    """A key past its expires_at must be rejected with 401 at the service."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models import ApiKeyAccess

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        await _create_key(client, admin_token, mock_apisix, {
            "name": "expired-app",
            "api_key": "ex-1",
            "allowed_databases": ["allowed-db"],
        })

    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await db.execute(
            update(ApiKeyAccess)
            .where(ApiKeyAccess.consumer_name == "expired-app")
            .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        )
        await db.commit()

    resp = await client.post(
        "/query/execute",
        json={"database": "allowed-db", "sql": "SELECT 1"},
        headers={"X-Consumer-Username": "expired-app"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "API key expired"


@pytest.mark.asyncio
async def test_list_api_keys_includes_expires_at(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        await _create_key(client, admin_token, mock_apisix, {
            "name": "listed-app",
            "api_key": "ls-1",
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))

    assert resp.status_code == 200
    entry = next(k for k in resp.json() if k["name"] == "listed-app")
    assert "expires_at" in entry
    assert entry["expires_at"] is None
