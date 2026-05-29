"""Tests for self-service API key endpoints (/admin/api-keys/me)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.auth import CurrentUser, get_current_user
from app.routers.api_keys import _self_consumer_name


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with a response carrying the given code."""
    request = httpx.Request("GET", "http://apisix/consumers/x")
    response = httpx.Response(status_code, request=request, text="not found")
    return httpx.HTTPStatusError("error", request=request, response=response)


def _override_user(app, sub: str, role: str = "user", username: str = "alice") -> None:
    """Override the JWT auth dependency so require_permission resolves against
    the seeded role while supplying a known sub."""
    async def _fake_current_user() -> CurrentUser:
        return CurrentUser(username=username, role=role, sub=sub)

    app.dependency_overrides[get_current_user] = _fake_current_user


def _patch_apisix():
    """Stub APISIX client: put/get/delete are no-op async; list_resources
    returns no routes so _sync_consumer_restriction is a no-op."""
    mock = patch("app.routers.api_keys.apisix_client")
    return mock


def test_self_consumer_name_strips_dashes():
    assert _self_consumer_name("abc-123-def") == "self_abc123def"


@pytest.mark.asyncio
async def test_get_me_returns_null_when_no_key(app, client):
    _override_user(app, sub="alice-sub-1")
    resp = await client.get("/admin/api-keys/me")
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_post_me_creates_key(app, client):
    _override_user(app, sub="alice-sub-1", username="alice")
    with _patch_apisix() as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        resp = await client.post("/admin/api-keys/me")

    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key"]  # truthy full key
    assert data["key_created"] is True
    assert data["allowed_databases"] == ["*"]
    assert sorted(data["allowed_routes"]) == ["query-api", "s3-api"]
    assert data["rate_limit_per_minute"] == 30
    assert data["owner"] == "alice-sub-1"
    assert data["name"] == _self_consumer_name("alice-sub-1")


@pytest.mark.asyncio
async def test_post_me_second_time_409(app, client):
    _override_user(app, sub="alice-sub-1")
    with _patch_apisix() as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        first = await client.post("/admin/api-keys/me")
        assert first.status_code == 201
        second = await client.post("/admin/api-keys/me")

    assert second.status_code == 409


@pytest.mark.asyncio
async def test_regenerate_changes_key(app, client):
    _override_user(app, sub="alice-sub-1")
    with _patch_apisix() as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        # get_resource 404s → regenerate uses minimal limit-count fallback
        mock_apisix.get_resource = AsyncMock(side_effect=_http_status_error(404))
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        create = await client.post("/admin/api-keys/me")
        assert create.status_code == 201
        original_key = create.json()["api_key"]

        regen = await client.post("/admin/api-keys/me/regenerate")
        assert regen.status_code == 200
        new_key = regen.json()["api_key"]

    assert new_key
    assert new_key != original_key

    # DB row still single (GET returns the same consumer, masked)
    with _patch_apisix() as mock_apisix:
        mock_apisix.get_resource = AsyncMock(side_effect=_http_status_error(404))
        get = await client.get("/admin/api-keys/me")
    assert get.status_code == 200
    assert get.json()["name"] == _self_consumer_name("alice-sub-1")


@pytest.mark.asyncio
async def test_regenerate_non_404_apisix_error_returns_502(app, client):
    """A transient (non-404) APISIX error while fetching the consumer must
    surface as 502 rather than silently resetting the consumer plugins."""
    _override_user(app, sub="alice-sub-1")
    with _patch_apisix() as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not called on create"))

        create = await client.post("/admin/api-keys/me")
        assert create.status_code == 201

        mock_apisix.get_resource = AsyncMock(side_effect=_http_status_error(500))
        regen = await client.post("/admin/api-keys/me/regenerate")

    assert regen.status_code == 502
    # The consumer plugins must NOT have been overwritten on the failed fetch.
    consumer_puts = [
        call.args for call in mock_apisix.put_resource.await_args_list
        if call.args[0] == "consumers"
    ]
    # Only the create call put the consumer; regenerate aborted before PUT.
    assert len(consumer_puts) == 1


@pytest.mark.asyncio
async def test_delete_me(app, client):
    _override_user(app, sub="alice-sub-1")
    with _patch_apisix() as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        create = await client.post("/admin/api-keys/me")
        assert create.status_code == 201

        delete = await client.delete("/admin/api-keys/me")
        assert delete.status_code == 204

        get = await client.get("/admin/api-keys/me")

    assert get.status_code == 200
    assert get.json() is None


@pytest.mark.asyncio
async def test_get_me_requires_apikeys_self(app, client):
    """A role lacking apikeys.self must be rejected with 403.

    A role with no apikeys.self permission resolves to an empty permission
    set in require_permission, so the self-service endpoints return 403.
    """
    _override_user(app, sub="bob-sub-1", role="noselfrole", username="bob")
    resp = await client.get("/admin/api-keys/me")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_regenerate_preserves_other_plugins(app, client):
    """When the consumer already exists in APISIX, regenerate must PRESERVE
    unrelated plugins (and limit-count) while swapping only key-auth.key.

    create_my_api_key does not call get_resource, so a static get_resource
    return value only affects the regenerate path."""
    _override_user(app, sub="alice-sub-1")
    with _patch_apisix() as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})
        # create path: does not call get_resource
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not called on create"))

        create = await client.post("/admin/api-keys/me")
        assert create.status_code == 201
        original_key = create.json()["api_key"]

        # regenerate path: consumer exists with extra plugins to preserve
        existing_limit = {"count": 30, "time_window": 60, "rejected_code": 429,
                          "key_type": "var", "key": "consumer_name"}
        mock_apisix.get_resource = AsyncMock(return_value={
            "plugins": {
                "key-auth": {"key": "old"},
                "limit-count": existing_limit,
                "other": {"x": 1},
            }
        })

        regen = await client.post("/admin/api-keys/me/regenerate")
        assert regen.status_code == 200
        new_key = regen.json()["api_key"]
        assert new_key and new_key != original_key

        # Find the put_resource call for the consumer (resource_type == "consumers")
        consumer_puts = [
            call.args for call in mock_apisix.put_resource.await_args_list
            if call.args[0] == "consumers"
        ]
        # The last consumer put is the regenerate one
        _, _, body = consumer_puts[-1]
        put_plugins = body["plugins"]

    assert put_plugins["other"] == {"x": 1}
    assert put_plugins["limit-count"] == existing_limit
    assert put_plugins["key-auth"]["key"] == new_key
    assert put_plugins["key-auth"]["key"] != "old"


@pytest.mark.asyncio
async def test_post_me_missing_sub_returns_400(app, client):
    _override_user(app, sub="", username="x")
    resp = await client.post("/admin/api-keys/me")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_me_cleans_up_consumer_on_sync_failure(app, client):
    """If _sync_consumer_restriction raises, the created APISIX consumer must
    be deleted and no DB row should persist."""
    _override_user(app, sub="alice-sub-1")
    with _patch_apisix() as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()
        # list_resources fails → _sync_consumer_restriction raises HTTPException(502)
        mock_apisix.list_resources = AsyncMock(side_effect=Exception("APISIX down"))

        resp = await client.post("/admin/api-keys/me")
        assert resp.status_code == 502
        mock_apisix.delete_resource.assert_awaited_once()
        assert mock_apisix.delete_resource.await_args.args[0] == "consumers"
        assert mock_apisix.delete_resource.await_args.args[1] == _self_consumer_name("alice-sub-1")

    # No DB row persisted
    with _patch_apisix() as mock_apisix:
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        get = await client.get("/admin/api-keys/me")
    assert get.status_code == 200
    assert get.json() is None


@pytest.mark.asyncio
async def test_self_wildcard_allows_arbitrary_s3_alias(app, client):
    """Carry-over Task 11 coverage: a self key has allowed_databases=["*"],
    so an APISIX-forwarded S3 browse for an ARBITRARY (unlisted) alias must
    NOT be rejected by the allowed_databases guard."""
    sub = "alice-sub-1"
    _override_user(app, sub=sub)
    with _patch_apisix() as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.delete_resource = AsyncMock()
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        create = await client.post("/admin/api-keys/me")
        assert create.status_code == 201
        consumer_name = create.json()["name"]

    # Simulate an APISIX-forwarded request (header only, no Bearer) for an
    # alias that is NOT in any explicit list — wildcard must let it through.
    with patch("app.routers.s3.s3_manager") as mock_s3_manager:
        mock_s3_manager.has_connection.return_value = True
        mock_s3_manager.list_buckets = AsyncMock(return_value=[{"name": "anything"}])

        # Remove the JWT override so the header-based ApiKeyUser path is used.
        from app.auth import get_current_user
        app.dependency_overrides.pop(get_current_user, None)

        resp = await client.get(
            "/s3/some-arbitrary-unlisted-alias/buckets",
            headers={"X-Consumer-Username": consumer_name},
        )

    assert resp.status_code == 200
    assert resp.json() == [{"name": "anything"}]
    mock_s3_manager.list_buckets.assert_awaited_once_with("some-arbitrary-unlisted-alias")
