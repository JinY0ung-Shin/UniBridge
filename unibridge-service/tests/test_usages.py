"""Tests for the API-key-facing /usages endpoint (app.routers.usages).

Covers the dual auth paths (APISIX ``X-Consumer-Username`` header vs JWT),
the forced self-scoping for API-key callers, and that date/consumer/LLM
handling matches the admin metrics endpoint it shares its implementation with.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import auth_header


async def _create_apikey(client, admin_token, *, name, key, allowed_routes):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": name,
            "plugins": {"key-auth": {"key": key}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": name,
                "api_key": key,
                "allowed_databases": [],
                "allowed_routes": allowed_routes,
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text


@pytest.fixture(autouse=True)
def _patch_routes_listing():
    """Default empty routes listing so tests don't hit a real APISIX."""
    with patch(
        "app.routers.gateway.apisix_client.list_resources",
        new=AsyncMock(return_value={"items": [], "total": 0}),
    ):
        yield


@pytest.mark.asyncio
async def test_apikey_caller_scoped_to_own_consumer(client, admin_token):
    await _create_apikey(
        client, admin_token, name="usage-app", key="usage-key", allowed_routes=["usages-api"]
    )
    results = [{"metric": {"route": "query-api"}, "value": [0, "42"]}]
    mock = AsyncMock(return_value=results)
    with patch("app.routers.gateway.prometheus_client.instant_query", mock):
        resp = await client.get(
            # A ?consumer= naming someone else is ignored (no cross-tenant leak).
            "/usages?date=2026-06-15&consumer=other-key",
            headers={"X-Consumer-Username": "usage-app"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["consumer"] == "usage-app"
    assert data["date"] == "2026-06-15"
    assert data["total_requests"] == 42
    assert data["routes"] == [{"route": "query-api", "name": None, "requests": 42}]

    query = mock.call_args.args[0]
    assert 'consumer="usage-app"' in query
    assert "other-key" not in query
    assert "[86400s]" in query


@pytest.mark.asyncio
async def test_apikey_caller_llm_routes_hidden(client, admin_token):
    await _create_apikey(
        client, admin_token, name="usage-app2", key="usage-key2", allowed_routes=["usages-api"]
    )
    mock = AsyncMock(return_value=[])
    with patch("app.routers.gateway.prometheus_client.instant_query", mock):
        resp = await client.get(
            "/usages?date=2026-06-15",
            headers={"X-Consumer-Username": "usage-app2"},
        )
    assert resp.status_code == 200
    assert 'route!="llm-proxy"' in mock.call_args.args[0]

    # include_llm stays admin-only, like the rest of LLM monitoring.
    resp = await client.get(
        "/usages?date=2026-06-15&include_llm=true",
        headers={"X-Consumer-Username": "usage-app2"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unknown_consumer_rejected(client):
    resp = await client.get(
        "/usages?date=2026-06-15",
        headers={"X-Consumer-Username": "ghost-key"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_missing_auth_rejected(client):
    resp = await client.get("/usages?date=2026-06-15")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_jwt_admin_can_filter_any_consumer(client, admin_token):
    mock = AsyncMock(return_value=[])
    with patch("app.routers.gateway.prometheus_client.instant_query", mock):
        resp = await client.get(
            "/usages?date=2026-06-15&consumer=k1",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["consumer"] == "k1"
    assert 'consumer="k1"' in mock.call_args.args[0]


@pytest.mark.asyncio
async def test_jwt_self_user_forced_to_sentinel_without_key(client, user_token):
    mock = AsyncMock(return_value=[])
    with patch("app.routers.gateway.prometheus_client.instant_query", mock):
        resp = await client.get(
            "/usages?date=2026-06-15&consumer=attacker-key",
            headers=auth_header(user_token),
        )
    assert resp.status_code == 200
    query = mock.call_args.args[0]
    assert 'consumer="__no_self_api_key__"' in query
    assert "attacker-key" not in query
    assert resp.json()["consumer"] is None


@pytest.mark.asyncio
async def test_invalid_and_future_dates_rejected(client, admin_token):
    resp = await client.get(
        "/usages?date=not-a-date", headers=auth_header(admin_token)
    )
    assert resp.status_code == 400

    resp = await client.get(
        "/usages?date=2999-01-01", headers=auth_header(admin_token)
    )
    assert resp.status_code == 400
