"""System-resource write protection and service-key redaction in the gateway router.

Three things are pinned here:

1. A system-managed route accepts safe edits (service keys, timeout) but refuses
   any change to its topology or auth. Those routes inject service-key headers,
   so re-pointing uri/upstream_id/methods would deliver the secrets to a host of
   the caller's choosing, and dropping key-auth would expose a built-in endpoint.
   The check fails closed: no verified copy of the route, no write.
2. ``PUT`` on a system-managed *upstream* is refused outright — its nodes are
   boot-provisioned and there is no safe field to edit.
3. A route read never carries a service-key secret in cleartext. ``service_keys``
   has always been masked, but the raw ``plugins`` block rode along in the same
   response body with the real values.
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.routers.gateway import (
    PROTECTED_UPSTREAM_IDS,
    _redact_service_key_headers,
)
from tests.conftest import auth_header

SECRET = "sk-live-upstream-secret-9876"
MASKED = "***9876"
NEW_SECRET = "sk-live-rotated-secret-4321"

PROTECTED_METHODS = ["POST", "GET", "PUT", "DELETE", "OPTIONS"]


def _route_with_secret(route_id: str = "r1", **extra) -> dict:
    route = {
        "id": route_id,
        "name": route_id,
        "uri": "/api/thing/*",
        "upstream_id": "u1",
        "plugins": {
            "key-auth": {},
            "proxy-rewrite": {"headers": {"set": {"X-Api-Key": SECRET}}},
        },
    }
    route.update(extra)
    return route


def _protected_route() -> dict:
    """``llm-proxy`` as app startup provisions it."""
    return {
        "id": "llm-proxy",
        "name": "llm-proxy",
        "uri": "/api/llm/*",
        "upstream_id": "litellm",
        "methods": list(PROTECTED_METHODS),
        "plugins": {
            "key-auth": {},
            "proxy-rewrite": {"headers": {"set": {"X-Api-Key": SECRET}}},
        },
    }


def _matching_body(**overrides) -> dict:
    """A PUT body whose topology matches :func:`_protected_route` exactly."""
    body = {
        "name": "llm-proxy",
        "uri": "/api/llm/*",
        "upstream_id": "litellm",
        "methods": list(PROTECTED_METHODS),
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Write protection: routes
# ---------------------------------------------------------------------------


class TestSaveRouteProtectedTopology:
    """Topology/auth changes to a system route are refused; safe edits are not."""

    @pytest.mark.parametrize(
        "overrides,vector",
        [
            ({"upstream_id": "attacker-upstream"}, "steal the service key"),
            ({"uri": "/api/llm-hijacked/*"}, "move the endpoint"),
            ({"methods": ["POST"]}, "narrow the methods"),
            ({"methods": None}, "widen to every method"),
            ({"require_auth": False}, "strip key-auth"),
        ],
    )
    async def test_rejects_topology_and_auth_changes(
        self, client, admin_token, overrides, vector
    ):
        body = _matching_body(**overrides)
        if body.get("methods") is None:
            # Omitting methods entirely would clear the restriction on a PUT.
            body.pop("methods")
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": [_protected_route()]},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=body,
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400, vector
        assert "topology" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    async def test_fails_closed_when_route_absent_from_apisix(
        self, client, admin_token
    ):
        """Nothing to compare against — and letting it through would let a caller
        re-create a built-in route pointing anywhere."""
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": []},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert "not registered in APISIX" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    async def test_fails_closed_when_listing_unavailable(self, client, admin_token):
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                side_effect=ConnectionError("refused"),
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 502
        mock_put.assert_not_awaited()

    async def test_method_reordering_is_not_a_change(self, client, admin_token):
        """The list's order carries no meaning — comparing it as a set avoids a
        spurious refusal on an otherwise safe edit."""
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": [_protected_route()]},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=_protected_route(),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(methods=list(reversed(PROTECTED_METHODS))),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        mock_put.assert_awaited_once()

    async def test_service_key_rotation_is_allowed(self, client, admin_token):
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": [_protected_route()]},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=_protected_route(),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(
                    service_keys=[
                        {"header_name": "X-Api-Key", "header_value": NEW_SECRET}
                    ]
                ),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        call_body = mock_put.call_args[0][2]
        assert call_body["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Api-Key": NEW_SECRET
        }
        # key-auth survives an edit that never mentions it.
        assert "key-auth" in call_body["plugins"]

    async def test_blank_header_value_preserves_the_stored_secret(
        self, client, admin_token
    ):
        """The UI's edit flow sends an empty value to mean "keep the secret" —
        masking the read path must not have broken it."""
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": [_protected_route()]},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=_protected_route(),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(
                    service_keys=[{"header_name": "X-Api-Key", "header_value": ""}]
                ),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        call_body = mock_put.call_args[0][2]
        assert call_body["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Api-Key": SECRET
        }

    async def test_timeout_override_is_allowed(self, client, admin_token):
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": [_protected_route()]},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=_protected_route(),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(timeout=300),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        call_body = mock_put.call_args[0][2]
        assert call_body["timeout"]["read"] == 300

    async def test_exempt_from_the_name_collision_check(self, client, admin_token):
        """A system route's name is forced to its id, which by definition matches
        its own id — the uniqueness loop must not fire on that."""
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={
                    "items": [_protected_route(), {"id": "other", "name": "llm-proxy"}]
                },
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=_protected_route(),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(name="whatever-the-caller-sent"),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock_put.call_args[0][2]["name"] == "llm-proxy"


class TestSaveRouteUnprotected:
    async def test_custom_route_still_saves(self, client, admin_token):
        """The guard must not be over-broad: ordinary routes still write."""
        saved = {"id": "custom-1", "uri": "/api/thing/*", "plugins": {}}
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": []},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=deepcopy(saved),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/custom-1",
                json={
                    "name": "custom route",
                    "uri": "/api/thing/*",
                    "upstream_id": "u1",
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        mock_put.assert_awaited_once()

    async def test_custom_route_name_may_not_shadow_a_protected_id(
        self, client, admin_token
    ):
        """Name/id collision detection still runs for custom routes: under APISIX
        prefer_name a route named ``query-api`` would merge its Prometheus series
        with the built-in route's."""
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": [{"id": "query-api", "name": "query-api"}]},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/custom-1",
                json={
                    "name": "query-api",
                    "uri": "/api/thing/*",
                    "upstream_id": "u1",
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 409
        mock_put.assert_not_awaited()


# ---------------------------------------------------------------------------
# Write protection: upstreams
# ---------------------------------------------------------------------------


class TestSaveUpstreamProtected:
    @pytest.mark.parametrize("upstream_id", sorted(PROTECTED_UPSTREAM_IDS))
    async def test_rejects_every_protected_upstream_id(
        self, client, admin_token, upstream_id
    ):
        """The attack: point a built-in upstream's nodes at a host the caller
        controls and collect the service-key headers the routes inject."""
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                return_value={"id": upstream_id},
            ) as mock_get,
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value={"id": upstream_id},
            ) as mock_put,
        ):
            resp = await client.put(
                f"/admin/gateway/upstreams/{upstream_id}",
                json={"type": "roundrobin", "nodes": {"attacker.example.com:80": 1}},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert "System-managed" in resp.json()["detail"]
        mock_put.assert_not_awaited()
        mock_get.assert_not_awaited()

    async def test_custom_upstream_still_saves(self, client, admin_token):
        with (
            patch(
                "app.routers.gateway.apisix_client.get_resource",
                new_callable=AsyncMock,
                side_effect=RuntimeError("absent"),
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value={"id": "custom-up", "type": "roundrobin"},
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/upstreams/custom-up",
                json={"type": "roundrobin", "nodes": {"svc.internal:8080": 1}},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        mock_put.assert_awaited_once()


# ---------------------------------------------------------------------------
# Service-key redaction on read
# ---------------------------------------------------------------------------


class TestServiceKeyRedaction:
    async def test_list_routes_never_returns_the_secret(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value={"items": [_route_with_secret()], "total": 1},
        ):
            resp = await client.get(
                "/admin/gateway/routes", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert SECRET not in resp.text
        item = resp.json()["items"][0]
        headers_set = item["plugins"]["proxy-rewrite"]["headers"]["set"]
        assert headers_set == {"X-Api-Key": MASKED}
        # The masked plugins value and the masked service_keys entry agree.
        assert item["service_keys"] == [
            {"header_name": "X-Api-Key", "header_value": MASKED}
        ]

    async def test_get_route_never_returns_the_secret(self, client, admin_token):
        with patch(
            "app.routers.gateway.apisix_client.get_resource",
            new_callable=AsyncMock,
            return_value=_route_with_secret(),
        ):
            resp = await client.get(
                "/admin/gateway/routes/r1", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert SECRET not in resp.text
        data = resp.json()
        assert data["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Api-Key": MASKED
        }
        assert data["service_key"] == {
            "header_name": "X-Api-Key",
            "header_value": MASKED,
        }

    async def test_save_route_echo_never_returns_the_secret(self, client, admin_token):
        """The PUT response echoes what APISIX stored, secrets included."""
        with (
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": []},
            ),
            patch(
                "app.routers.gateway.apisix_client.put_resource",
                new_callable=AsyncMock,
                return_value=_route_with_secret(),
            ) as mock_put,
        ):
            resp = await client.put(
                "/admin/gateway/routes/r1",
                json={
                    "name": "thing route",
                    "uri": "/api/thing/*",
                    "upstream_id": "u1",
                    "service_keys": [
                        {"header_name": "X-Api-Key", "header_value": SECRET}
                    ],
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert SECRET not in resp.text
        # Redaction shapes the response only — the real value still reaches APISIX.
        call_body = mock_put.call_args[0][2]
        assert call_body["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Api-Key": SECRET
        }

    async def test_route_test_still_forwards_the_real_header(self, client, admin_token):
        """The health probe must send the true secret: it reads the route from
        APISIX directly rather than through the redacted view."""
        fake_response = httpx.Response(
            200, json={"ok": True}, request=httpx.Request("GET", "http://x")
        )
        with (
            patch("app.routers.gateway.apisix_client") as apisix,
            patch("httpx.AsyncClient") as cls,
        ):
            apisix.get_resource = AsyncMock(
                side_effect=[
                    _route_with_secret(),
                    {"nodes": {"svc.internal:8080": 1}},
                ]
            )
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.__aexit__.return_value = None
            instance.get = AsyncMock(return_value=fake_response)
            cls.return_value = instance

            resp = await client.post(
                "/admin/gateway/routes/r1/test", headers=auth_header(admin_token)
            )
        assert resp.status_code == 200
        assert instance.get.await_args.kwargs["headers"]["X-Api-Key"] == SECRET


# ---------------------------------------------------------------------------
# _redact_service_key_headers unit tests
# ---------------------------------------------------------------------------


class TestRedactServiceKeyHeaders:
    def test_masks_both_set_and_add(self):
        route = {
            "plugins": {
                "proxy-rewrite": {
                    "headers": {
                        "set": {"X-Api-Key": SECRET},
                        "add": {"X-Extra": SECRET},
                        "remove": ["X-Internal"],
                    }
                }
            }
        }
        _redact_service_key_headers(route)
        headers = route["plugins"]["proxy-rewrite"]["headers"]
        assert headers["set"] == {"X-Api-Key": MASKED}
        assert headers["add"] == {"X-Extra": MASKED}
        # remove holds header *names*, not values — left alone.
        assert headers["remove"] == ["X-Internal"]

    def test_keeps_header_names_and_other_proxy_rewrite_config(self):
        route = {
            "plugins": {
                "proxy-rewrite": {
                    "regex_uri": ["^/api/thing(.*)", "$1"],
                    "headers": {"set": {"X-Api-Key": SECRET, "X-Tenant": "acme"}},
                }
            }
        }
        _redact_service_key_headers(route)
        pr = route["plugins"]["proxy-rewrite"]
        assert pr["regex_uri"] == ["^/api/thing(.*)", "$1"]
        assert set(pr["headers"]["set"]) == {"X-Api-Key", "X-Tenant"}
        assert SECRET not in pr["headers"]["set"].values()

    def test_short_values_collapse_to_stars(self):
        route = {"plugins": {"proxy-rewrite": {"headers": {"set": {"X-K": "abc"}}}}}
        _redact_service_key_headers(route)
        assert route["plugins"]["proxy-rewrite"]["headers"]["set"] == {"X-K": "***"}

    @pytest.mark.parametrize(
        "route",
        [
            {},
            {"plugins": None},
            {"plugins": "bad"},
            {"plugins": {}},
            {"plugins": {"proxy-rewrite": None}},
            {"plugins": {"proxy-rewrite": {}}},
            {"plugins": {"proxy-rewrite": {"headers": "bad"}}},
            {"plugins": {"proxy-rewrite": {"headers": {}}}},
            {"plugins": {"proxy-rewrite": {"headers": {"set": "bad"}}}},
            {"plugins": {"proxy-rewrite": {"headers": {"set": {"X-K": 42}}}}},
        ],
    )
    def test_tolerates_malformed_shapes(self, route):
        """APISIX config can be edited outside UniBridge; nothing here may raise."""
        _redact_service_key_headers(route)
