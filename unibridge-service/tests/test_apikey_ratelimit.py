from unittest.mock import AsyncMock, patch

import pytest

from app.routers.api_keys import _build_limit_count_plugin
from tests.conftest import auth_header

ROUTE_FIXTURES = {"items": []}

EXPECTED_LIMIT_COUNT = {
    "count": 30,
    "time_window": 60,
    "rejected_code": 429,
    "key_type": "var",
    "key": "consumer_name",
}


def test_limit_count_none_when_unlimited():
    assert _build_limit_count_plugin(None) is None


def test_limit_count_config_when_set():
    cfg = _build_limit_count_plugin(30)
    assert cfg == EXPECTED_LIMIT_COUNT


def _consumer_put_bodies(mock_apisix):
    """Extract the bodies of put_resource('consumers', ...) calls."""
    return [
        call.args[2]
        for call in mock_apisix.put_resource.await_args_list
        if call.args[0] == "consumers"
    ]


@pytest.mark.asyncio
async def test_create_api_key_provisions_limit_count(client, admin_token):
    """create_api_key with rate_limit_per_minute must provision the
    limit-count plugin on the APISIX consumer alongside key-auth."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={"username": "rl-create"})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": "rl-create",
                "api_key": "rl-key-1",
                "rate_limit_per_minute": 30,
            },
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 201
        assert resp.json()["rate_limit_per_minute"] == 30

        bodies = _consumer_put_bodies(mock_apisix)
        assert len(bodies) == 1
        plugins = bodies[0]["plugins"]
        assert plugins["limit-count"] == EXPECTED_LIMIT_COUNT
        # key-auth must also be provisioned when api_key was provided
        assert plugins["key-auth"] == {"key": "rl-key-1"}


@pytest.mark.asyncio
async def test_update_api_key_rate_limit_only_preserves_key_auth(client, admin_token):
    """A rate-limit-only update (no api_key) must add limit-count while
    PRESERVING the consumer's existing key-auth — regression guard against
    dropping key-auth on the consumer PUT."""
    # Create the key first (no rate limit yet).
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={"username": "rl-update"})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        create_resp = await client.post(
            "/admin/api-keys",
            json={"name": "rl-update", "api_key": "rl-key-2"},
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    # Update with ONLY rate_limit_per_minute; existing consumer has key-auth.
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "rl-update",
            "plugins": {"key-auth": {"key": "rl-key-2"}},
        })
        mock_apisix.put_resource = AsyncMock(return_value={"username": "rl-update"})
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.put(
            "/admin/api-keys/rl-update",
            json={"rate_limit_per_minute": 30},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["rate_limit_per_minute"] == 30

        bodies = _consumer_put_bodies(mock_apisix)
        assert len(bodies) == 1
        plugins = bodies[0]["plugins"]
        # key-auth preserved AND limit-count added
        assert plugins["key-auth"] == {"key": "rl-key-2"}
        assert plugins["limit-count"] == EXPECTED_LIMIT_COUNT


@pytest.mark.asyncio
async def test_update_clears_rate_limit_when_explicit_null(client, admin_token):
    """An admin PUT with an EXPLICIT null rate_limit_per_minute must clear the
    limit: the consumer PUT body must drop limit-count, and the DB row's
    rate_limit_per_minute must become None."""
    # Create the key WITH a rate limit so APISIX consumer has limit-count and
    # the DB row has rate_limit_per_minute=30.
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={"username": "rl-clear"})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        create_resp = await client.post(
            "/admin/api-keys",
            json={"name": "rl-clear", "api_key": "rl-key-3", "rate_limit_per_minute": 30},
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201
        assert create_resp.json()["rate_limit_per_minute"] == 30

    # PUT explicit null → clear the limit.
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "rl-clear",
            "plugins": {
                "key-auth": {"key": "rl-key-3"},
                "limit-count": EXPECTED_LIMIT_COUNT,
            },
        })
        mock_apisix.put_resource = AsyncMock(return_value={"username": "rl-clear"})
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.put(
            "/admin/api-keys/rl-clear",
            json={"rate_limit_per_minute": None},  # explicit JSON null
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        # DB row now unlimited
        assert resp.json()["rate_limit_per_minute"] is None

        bodies = _consumer_put_bodies(mock_apisix)
        assert len(bodies) == 1
        plugins = bodies[0]["plugins"]
        # limit-count was popped; key-auth preserved
        assert "limit-count" not in plugins
        assert plugins["key-auth"] == {"key": "rl-key-3"}


@pytest.mark.asyncio
async def test_update_omitted_rate_limit_preserves_existing(client, admin_token):
    """An admin PUT that OMITS rate_limit_per_minute must leave the existing
    limit untouched: limit-count stays on the consumer body and the DB row's
    rate_limit_per_minute is unchanged."""
    # Create the key WITH a rate limit.
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={"username": "rl-keep"})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        create_resp = await client.post(
            "/admin/api-keys",
            json={"name": "rl-keep", "api_key": "rl-key-4", "rate_limit_per_minute": 30},
            headers=auth_header(admin_token),
        )
        assert create_resp.status_code == 201

    # PUT a body WITHOUT rate_limit_per_minute (description only).
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "rl-keep",
            "plugins": {
                "key-auth": {"key": "rl-key-4"},
                "limit-count": EXPECTED_LIMIT_COUNT,
            },
        })
        mock_apisix.put_resource = AsyncMock(return_value={"username": "rl-keep"})
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)

        resp = await client.put(
            "/admin/api-keys/rl-keep",
            json={"description": "x"},  # rate_limit_per_minute omitted
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        # DB row's rate limit unchanged
        assert resp.json()["rate_limit_per_minute"] == 30

        # rate_limit was not provided → no consumer PUT at all (api_key absent
        # and rate_limit_provided False), so the existing limit-count is left
        # intact in APISIX. Assert we did not touch the consumer.
        bodies = _consumer_put_bodies(mock_apisix)
        assert bodies == []
