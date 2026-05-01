"""Extra coverage for api_keys router edge cases and error paths."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.routers.api_keys import (
    DENY_ALL_CONSUMER,
    _extract_api_key,
    _mask_key,
    _sync_consumer_restriction,
)
from tests.conftest import auth_header


def _http_status_error(status_code: int, body: str = "boom") -> httpx.HTTPStatusError:
    request = httpx.Request("PUT", "http://apisix")
    response = httpx.Response(status_code, request=request, text=body)
    return httpx.HTTPStatusError("err", request=request, response=response)


# ── Pure helper functions ───────────────────────────────────────────────────

def test_mask_key_short_value():
    assert _mask_key("abc") == "***"


def test_mask_key_long_value():
    assert _mask_key("supersecretkey") == "***tkey"


def test_extract_api_key_returns_none_when_missing():
    assert _extract_api_key({"plugins": {}}) is None
    assert _extract_api_key({}) is None
    assert _extract_api_key({"plugins": {"key-auth": {}}}) is None


def test_extract_api_key_masked():
    assert _extract_api_key({"plugins": {"key-auth": {"key": "abcdef1234"}}}) == "***1234"


def test_extract_api_key_unmasked():
    out = _extract_api_key(
        {"plugins": {"key-auth": {"key": "abc"}}}, mask=False,
    )
    assert out == "abc"


# ── _sync_consumer_restriction error paths ──────────────────────────────────

@pytest.mark.asyncio
async def test_sync_consumer_restriction_list_routes_failure():
    """list_resources failure should raise 502."""
    from fastapi import HTTPException

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=RuntimeError("network"))
        with pytest.raises(HTTPException) as exc_info:
            await _sync_consumer_restriction(["r1"], "consumer-x")
    assert exc_info.value.status_code == 502
    assert "Failed to list APISIX routes" in exc_info.value.detail


@pytest.mark.asyncio
async def test_sync_consumer_restriction_skips_route_without_id_or_keyauth():
    """Routes without id or without key-auth plugin must be skipped."""
    routes = {
        "items": [
            {"uri": "/no-id"},  # no id
            {"id": "no-keyauth", "plugins": {"basic-auth": {}}},  # no key-auth
            {"id": "ok", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}}},
        ]
    }
    captured = []

    async def list_resources(rt):
        return routes

    async def put_resource(rt, rid, body):
        captured.append((rt, rid, body))
        return {}

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)
        await _sync_consumer_restriction(["ok"], "c1")

    # Only the 'ok' route was updated
    assert len(captured) == 1
    assert captured[0][1] == "ok"


@pytest.mark.asyncio
async def test_sync_consumer_restriction_rollback_on_failure():
    """Mid-failure: previously applied changes must be rolled back."""
    from fastapi import HTTPException

    routes = {
        "items": [
            {
                "id": "r1",
                "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
                "uri": "/r1",
            },
            {
                "id": "r2",
                "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
                "uri": "/r2",
            },
        ]
    }
    state: dict[str, dict] = {}

    async def list_resources(_):
        return routes

    async def put_resource(rt, rid, body):
        if rid == "r2":
            raise RuntimeError("apisix down")
        state[rid] = body
        return {}

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)
        with pytest.raises(HTTPException) as exc_info:
            await _sync_consumer_restriction(["r1", "r2"], "c1")
    assert exc_info.value.status_code == 502
    assert "Rolled back 1 previously applied change(s)" in exc_info.value.detail


@pytest.mark.asyncio
async def test_sync_consumer_restriction_rollback_failure_swallowed():
    """If the rollback PUT also fails, we still raise the original 502 — logged only."""
    from fastapi import HTTPException

    routes = {
        "items": [
            {
                "id": "r1",
                "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
                "uri": "/r1",
            },
            {
                "id": "r2",
                "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}},
                "uri": "/r2",
            },
        ]
    }
    call_count = {"n": 0}

    async def put_resource(rt, rid, body):
        call_count["n"] += 1
        # Successful first PUT (r1), then r2 fails, then rollback for r1 fails
        if call_count["n"] == 1:
            return {}
        raise RuntimeError("network")

    with patch("app.routers.api_keys.apisix_client") as mock_apisix, \
            patch("app.routers.api_keys.logger.error") as log_err:
        mock_apisix.list_resources = AsyncMock(return_value=routes)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)
        with pytest.raises(HTTPException):
            await _sync_consumer_restriction(["r1", "r2"], "c1")
    log_err.assert_called()


# ── list_api_keys with masked key ───────────────────────────────────────────

ROUTE_FIXTURES = {"items": []}


async def _create_key(client, admin_token, name="masked-app", api_key="key-12345678"):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={"username": name})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("nope"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        await client.post(
            "/admin/api-keys",
            json={"name": name, "api_key": api_key},
            headers=auth_header(admin_token),
        )


@pytest.mark.asyncio
async def test_list_api_keys_includes_masked_key(client, admin_token):
    await _create_key(client, admin_token, "li-app", "veryverysecret123")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "li-app",
            "plugins": {"key-auth": {"key": "veryverysecret123"}},
        })
        resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["api_key"] == "***t123"


@pytest.mark.asyncio
async def test_list_api_keys_handles_apisix_failure_gracefully(client, admin_token):
    await _create_key(client, admin_token, "fail-app", "key-fail-789")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(side_effect=RuntimeError("apisix down"))
        resp = await client.get("/admin/api-keys", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()[0]["api_key"] is None


# ── create_api_key error paths ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_api_key_apisix_http_status_error(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(side_effect=_http_status_error(500, "explosion"))
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "fail-create", "api_key": "k1"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502
    assert "explosion" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_api_key_apisix_generic_error(client, admin_token):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(side_effect=RuntimeError("boom"))
        resp = await client.post(
            "/admin/api-keys",
            json={"name": "fail-create2", "api_key": "k1"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502
    assert "Failed to create APISIX consumer" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_api_key_sync_failure_cleans_up_consumer(client, admin_token):
    """If consumer-restriction sync fails, the just-created consumer must be deleted."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={"username": "rollback-app"})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        # list_resources blows up → triggers HTTPException 502 from sync
        mock_apisix.list_resources = AsyncMock(side_effect=RuntimeError("apisix"))
        mock_apisix.delete_resource = AsyncMock()

        resp = await client.post(
            "/admin/api-keys",
            json={"name": "rollback-app", "api_key": "k1", "allowed_routes": ["x"]},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502
    mock_apisix.delete_resource.assert_awaited_once_with("consumers", "rollback-app")


@pytest.mark.asyncio
async def test_create_api_key_sync_failure_consumer_cleanup_failure_logged(
    client, admin_token,
):
    """Cleanup itself failing should log error but not block raising 502."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix, \
            patch("app.routers.api_keys.logger.error") as log_err:
        mock_apisix.put_resource = AsyncMock(return_value={"username": "ugly-app"})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(side_effect=RuntimeError("x"))
        mock_apisix.delete_resource = AsyncMock(side_effect=RuntimeError("cleanup failed"))

        resp = await client.post(
            "/admin/api-keys",
            json={"name": "ugly-app", "api_key": "k1", "allowed_routes": ["x"]},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502
    log_err.assert_called()


# ── update_api_key error paths ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_api_key_404(client, admin_token):
    resp = await client.put(
        "/admin/api-keys/nope",
        json={"description": "hi"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_api_key_existing_consumer_get_fails_uses_empty_plugins(
    client, admin_token,
):
    """When fetching the existing consumer fails, we still update with new key."""
    await _create_key(client, admin_token, "upd-misfetch", "k0")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        # First put_resource (during update) succeeds
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": "upd-misfetch",
            "plugins": {"key-auth": {"key": "newk"}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("nope"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        resp = await client.put(
            "/admin/api-keys/upd-misfetch",
            json={"api_key": "newk"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["api_key"] == "newk"
    assert resp.json()["key_created"] is True


@pytest.mark.asyncio
async def test_update_api_key_put_consumer_failure(client, admin_token):
    await _create_key(client, admin_token, "upd-putfail", "k0")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "upd-putfail",
            "plugins": {"key-auth": {"key": "k0"}},
        })
        mock_apisix.put_resource = AsyncMock(side_effect=RuntimeError("apisix down"))
        resp = await client.put(
            "/admin/api-keys/upd-putfail",
            json={"api_key": "newk"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502
    assert "Failed to update APISIX consumer" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_api_key_sync_failure_rolls_back_key(client, admin_token):
    """If consumer-restriction sync fails after key change, key change is rolled back."""
    await _create_key(client, admin_token, "rb-app", "old-key")

    state = {"plugins": {"key-auth": {"key": "old-key"}}}

    async def get_resource(_, name):
        return {"username": name, "plugins": dict(state["plugins"])}

    put_calls = []

    async def put_resource(rt, rid, body):
        if rt == "consumers":
            put_calls.append((rid, body))
            state["plugins"] = body["plugins"]
            return body
        # routes - blow up to fail sync
        raise RuntimeError("sync down")

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(side_effect=get_resource)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)
        mock_apisix.list_resources = AsyncMock(return_value={
            "items": [
                {"id": "r1", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}}},
            ],
        })

        resp = await client.put(
            "/admin/api-keys/rb-app",
            json={"api_key": "new-key", "allowed_routes": ["r1"]},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502
    # First call: set new-key, second call: rollback to old plugins
    consumer_calls = [c for c in put_calls if c[0] == "rb-app"]
    assert len(consumer_calls) >= 2
    final_plugins = consumer_calls[-1][1]["plugins"]
    assert final_plugins["key-auth"]["key"] == "old-key"


@pytest.mark.asyncio
async def test_update_api_key_sync_failure_rollback_failure_logged(client, admin_token):
    await _create_key(client, admin_token, "rblog-app", "old-key")

    call_count = {"n": 0}

    async def put_resource(rt, rid, body):
        if rt == "consumers":
            call_count["n"] += 1
            if call_count["n"] == 2:  # rollback fails
                raise RuntimeError("rollback down")
            return body
        raise RuntimeError("sync down")

    with patch("app.routers.api_keys.apisix_client") as mock_apisix, \
            patch("app.routers.api_keys.logger.error") as log_err:
        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "rblog-app",
            "plugins": {"key-auth": {"key": "old-key"}},
        })
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)
        mock_apisix.list_resources = AsyncMock(return_value={
            "items": [
                {"id": "r1", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}}},
            ],
        })

        resp = await client.put(
            "/admin/api-keys/rblog-app",
            json={"api_key": "new-key", "allowed_routes": ["r1"]},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502
    log_err.assert_called()


@pytest.mark.asyncio
async def test_update_api_key_no_key_change_fetches_masked(client, admin_token):
    """When no api_key is sent, masked key should be fetched from APISIX."""
    await _create_key(client, admin_token, "no-key-up", "abcdefghijk")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(return_value={
            "username": "no-key-up",
            "plugins": {"key-auth": {"key": "abcdefghijk"}},
        })
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        resp = await client.put(
            "/admin/api-keys/no-key-up",
            json={"description": "tag"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["api_key"] == "***hijk"
    assert resp.json()["key_created"] is False


@pytest.mark.asyncio
async def test_update_api_key_no_key_change_get_fails(client, admin_token):
    """get_resource failing during display lookup is silently handled."""
    await _create_key(client, admin_token, "no-key-up2", "abc1234567")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.get_resource = AsyncMock(side_effect=RuntimeError("nope"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        resp = await client.put(
            "/admin/api-keys/no-key-up2",
            json={"description": "x"},
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 200
    assert resp.json()["api_key"] is None


# ── delete_api_key error paths ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_api_key_404(client, admin_token):
    resp = await client.delete(
        "/admin/api-keys/notthere",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_api_key_404_from_apisix_is_swallowed(client, admin_token):
    """APISIX returning 404 on delete is treated as already-deleted (success)."""
    await _create_key(client, admin_token, "del-404", "k1")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.delete_resource = AsyncMock(side_effect=_http_status_error(404, "gone"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        resp = await client.delete(
            "/admin/api-keys/del-404",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_api_key_apisix_500_propagates(client, admin_token):
    await _create_key(client, admin_token, "del-500", "k1")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.delete_resource = AsyncMock(side_effect=_http_status_error(500, "boom"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        resp = await client.delete(
            "/admin/api-keys/del-500",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_delete_api_key_generic_error(client, admin_token):
    await _create_key(client, admin_token, "del-gen", "k1")
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.delete_resource = AsyncMock(side_effect=RuntimeError("oh no"))
        mock_apisix.list_resources = AsyncMock(return_value=ROUTE_FIXTURES)
        resp = await client.delete(
            "/admin/api-keys/del-gen",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_delete_api_key_with_old_routes_triggers_unwhitelist(client, admin_token):
    """Deleting a key that had allowed_routes should clear it from those routes' whitelist."""
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={"username": "wl-app"})
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("nope"))
        mock_apisix.list_resources = AsyncMock(return_value={
            "items": [
                {"id": "r1", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": []}}},
            ],
        })
        await client.post(
            "/admin/api-keys",
            json={"name": "wl-app", "api_key": "kx", "allowed_routes": ["r1"]},
            headers=auth_header(admin_token),
        )

    routes_state = {
        "r1": {"id": "r1", "plugins": {"key-auth": {}, "consumer-restriction": {"whitelist": ["wl-app"]}}, "uri": "/r1"},
    }

    async def list_resources(_):
        return {"items": list(routes_state.values())}

    async def put_resource(rt, rid, body):
        if rt == "routes":
            routes_state[rid] = {"id": rid, **body}
        return {}

    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.list_resources = AsyncMock(side_effect=list_resources)
        mock_apisix.put_resource = AsyncMock(side_effect=put_resource)
        mock_apisix.delete_resource = AsyncMock()
        resp = await client.delete(
            "/admin/api-keys/wl-app",
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 204
    # Consumer was unwhitelisted from r1
    final_wl = routes_state["r1"]["plugins"]["consumer-restriction"]["whitelist"]
    assert "wl-app" not in final_wl
    assert DENY_ALL_CONSUMER in final_wl
