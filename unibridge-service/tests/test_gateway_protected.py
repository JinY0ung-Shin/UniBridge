"""System-resource write protection and service-key redaction in the gateway router.

Six things are pinned here:

1. A system-managed route accepts safe edits (service keys, timeout) but refuses
   any change to its topology or auth. Those routes inject service-key headers,
   so re-pointing uri/upstream_id/methods would deliver the secrets to a host of
   the caller's choosing, and dropping key-auth would expose a built-in endpoint.
   The check fails closed: no verified copy of the route, no write.
2. That protection is an *allowlist*: only service keys, ``timeout``, ``desc``
   and ``labels`` may differ from what APISIX holds. A denylist left every field
   nobody thought of open — ``status: 0`` to take a built-in endpoint offline,
   ``priority`` to lose a race with another route, ``hosts``/``vars`` to re-scope
   it, ``proxy-rewrite.regex_uri`` to change the forwarded path.
3. Service keys are the one editable plugin field, and a service-key edit replaces
   ``headers.set`` wholesale — so on a system route the headers startup injects
   (the internal-proxy marker API-key auth depends on, the LiteLLM master key,
   the end-user id) survive that edit and cannot be given a caller-supplied
   value. Custom routes keep the plain replace-wholesale semantics.
4. A non-system route may not claim a uri inside a system route's namespace.
   APISIX picks between overlapping patterns by priority and specificity, so such
   a route captures the system route's traffic — caller API keys and request
   bodies — toward an upstream of the writer's choosing.
5. ``PUT`` on a system-managed *upstream* is refused outright — its nodes are
   boot-provisioned and there is no safe field to edit.
6. A route read never carries a service-key secret in cleartext. ``service_keys``
   has always been masked, but the raw ``plugins`` block rode along in the same
   response body with the real values.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.routers.gateway import (
    PROTECTED_UPSTREAM_IDS,
    _attach_service_key_fields,
    _attach_timeout_fields,
    _extract_strip_prefix,
    _mask_value,
    _redact_service_key_headers,
)
from tests.conftest import auth_header

SECRET = "sk-live-upstream-secret-9876"
MASKED = "***9876"
NEW_SECRET = "sk-live-rotated-secret-4321"

PROTECTED_METHODS = ["POST", "GET", "PUT", "DELETE", "OPTIONS"]

# Headers startup provisioning injects on system routes.
INTERNAL_HEADER = "X-UniBridge-Internal-Proxy"
INTERNAL_SECRET = "internal-proxy-secret-2f9c"
MASTER_KEY = "Bearer sk-litellm-master-0001"
END_USER_HEADER = "x-litellm-end-user-id"


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


def _provisioned_query_route() -> dict:
    """``query-api`` with the full field set app startup gives it.

    Unlike :func:`_protected_route` this carries the fields a denylist left
    unguarded — ``status``, ``priority``, and a ``regex_uri`` that rewrites the
    forwarded path to something ``strip_prefix`` would not reproduce.
    """
    return {
        "id": "query-api",
        "name": "query-api",
        "uri": "/api/query/*",
        "methods": ["POST", "GET"],
        "upstream_id": "unibridge-service",
        "priority": 20,
        "status": 1,
        "timeout": {"connect": 10, "send": 300, "read": 300},
        "plugins": {
            "key-auth": {},
            "consumer-restriction": {"whitelist": ["master-key"]},
            "proxy-rewrite": {
                "regex_uri": ["^/api/query(.*)", "/query$1"],
                "use_real_request_uri_unsafe": True,
                "headers": {
                    "set": {
                        INTERNAL_HEADER: INTERNAL_SECRET,
                        "X-Internal-Key": SECRET,
                    }
                },
            },
        },
        "create_time": 1700000000,
        "update_time": 1700000001,
    }


def _provisioned_llm_route() -> dict:
    """``llm-proxy`` with the headers startup actually injects on it."""
    route = _protected_route()
    route["plugins"]["proxy-rewrite"] = {
        "regex_uri": ["^/api/llm(.*)", "$1"],
        "use_real_request_uri_unsafe": True,
        "headers": {
            "set": {
                "Authorization": MASTER_KEY,
                END_USER_HEADER: "$consumer_name",
                "X-Api-Key": SECRET,
            }
        },
    }
    return route


def _query_body(**overrides) -> dict:
    """A PUT body whose frozen fields match :func:`_provisioned_query_route`."""
    body = {
        "name": "query-api",
        "uri": "/api/query/*",
        "methods": ["POST", "GET"],
        "upstream_id": "unibridge-service",
        "priority": 20,
        "status": 1,
    }
    body.update(overrides)
    return body


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


def _read_view(route: dict) -> dict:
    """What ``GET /admin/gateway/routes/{id}`` hands a client for a route."""
    view = deepcopy(route)
    _attach_service_key_fields(view)
    view["require_auth"] = "key-auth" in view.get("plugins", {})
    view["strip_prefix"] = _extract_strip_prefix(view)
    _attach_timeout_fields(view)
    return view


@contextmanager
def _apisix(existing: dict | None, put_return: dict | None = None):
    """Mock the route listing and PUT that ``save_route`` performs."""
    items = [deepcopy(existing)] if existing else []
    echo = put_return if put_return is not None else (existing or {})
    with (
        patch(
            "app.routers.gateway.apisix_client.list_resources",
            new_callable=AsyncMock,
            return_value={"items": items, "total": len(items)},
        ),
        patch(
            "app.routers.gateway.apisix_client.put_resource",
            new_callable=AsyncMock,
            return_value=deepcopy(echo),
        ) as mock_put,
    ):
        yield mock_put


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

    async def test_masked_value_round_trip_preserves_the_stored_secret(
        self, client, admin_token
    ):
        """A scripted client that GETs a route and PUTs it back verbatim sends
        the mask it was handed; storing it would destroy the real secret."""
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
                    service_keys=[{"header_name": "X-Api-Key", "header_value": MASKED}]
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


class TestSaveRouteProtectedAllowlist:
    """Only service keys, timeout, desc and labels may differ from APISIX.

    Every other field reaches APISIX verbatim, so the guard has to enumerate what
    is *allowed* — the fields below all rode straight through a denylist.
    """

    @pytest.mark.parametrize(
        "overrides,field,vector",
        [
            ({"status": 0}, "status", "take a built-in endpoint offline"),
            ({"priority": 99}, "priority", "win a race against another route"),
            ({"hosts": ["evil.example.com"]}, "hosts", "re-scope by Host header"),
            ({"host": "evil.example.com"}, "host", "re-scope by Host header"),
            ({"vars": [["http_x", "==", "1"]]}, "vars", "match on a chosen header"),
            ({"service_id": "attacker-service"}, "service_id", "inherit a service"),
            ({"plugin_config_id": "attacker-cfg"}, "plugin_config_id", "pull in plugins"),
            ({"script": "return 1"}, "script", "run arbitrary Lua"),
            ({"remote_addrs": ["10.0.0.0/8"]}, "remote_addrs", "restrict by client IP"),
            ({"filter_func": "function() return true end"}, "filter_func", "add a matcher"),
            ({"enable_websocket": True}, "enable_websocket", "flip the protocol"),
        ],
    )
    async def test_rejects_every_unlisted_field(
        self, client, admin_token, overrides, field, vector
    ):
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(**overrides),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400, vector
        assert f"'{field}'" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    async def test_dropping_a_field_is_a_change_too(self, client, admin_token):
        """A PUT replaces the whole route: omitting ``priority`` deletes it, which
        is how llm-messages loses its race against the /api/llm/* catch-all."""
        body = _query_body()
        body.pop("priority")
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=body,
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert "'priority'" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    async def test_rewriting_the_forwarded_path_is_refused(self, client, admin_token):
        """``strip_prefix`` derives regex_uri from the uri, which for query-api is
        not what provisioning set — that edit re-points the upstream path."""
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(strip_prefix=True),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert "'plugins.proxy-rewrite.regex_uri'" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    async def test_dropping_the_forwarded_path_rewrite_is_refused(
        self, client, admin_token
    ):
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(strip_prefix=False),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert "'plugins.proxy-rewrite.regex_uri'" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    async def test_injected_plugin_never_reaches_apisix(self, client, admin_token):
        """``_inject_plugins`` rebuilds plugins from the stored route, so a plugin
        named in the request body is dropped rather than compared — it cannot
        reach APISIX either way."""
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(
                    plugins={"ip-restriction": {"whitelist": ["0.0.0.0/0"]}}
                ),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert "ip-restriction" not in mock_put.call_args[0][2]["plugins"]

    async def test_key_auth_config_cannot_be_replaced(self, client, admin_token):
        existing = _provisioned_query_route()
        existing["plugins"]["key-auth"] = {"header": "apikey"}
        with _apisix(existing) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(require_auth=True),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert "'plugins.key-auth'" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    async def test_service_key_rotation_with_timeout_and_desc_is_allowed(
        self, client, admin_token
    ):
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(
                    desc="rotated 2026-08-26",
                    timeout=420,
                    service_keys=[
                        {"header_name": "X-Internal-Key", "header_value": NEW_SECRET}
                    ],
                ),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        call_body = mock_put.call_args[0][2]
        assert call_body["desc"] == "rotated 2026-08-26"
        assert call_body["timeout"]["read"] == 420
        assert call_body["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Internal-Key": NEW_SECRET,
            INTERNAL_HEADER: INTERNAL_SECRET,
        }
        # Frozen neighbours in the same subtree survive the rotation.
        assert call_body["plugins"]["proxy-rewrite"]["regex_uri"] == [
            "^/api/query(.*)",
            "/query$1",
        ]
        assert call_body["plugins"]["consumer-restriction"] == {
            "whitelist": ["master-key"]
        }

    async def test_unchanged_save_is_allowed(self, client, admin_token):
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        mock_put.assert_awaited_once()

    async def test_read_view_round_trip_is_accepted(self, client, admin_token):
        """A client that GETs a route and PUTs it back changes nothing: the read
        view's decorations and masked secrets must not read as a diff."""
        existing = _protected_route()
        with _apisix(existing) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_read_view(existing),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        call_body = mock_put.call_args[0][2]
        # The mask in the round-tripped body did not overwrite the real secret.
        assert call_body["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Api-Key": SECRET
        }


class TestSystemInjectedHeaders:
    """A service-key edit replaces ``headers.set`` wholesale — the provisioned
    headers have to outlive that, and may not be written by hand."""

    async def test_rotation_preserves_the_internal_proxy_header(
        self, client, admin_token
    ):
        """Dropping it 401s every API-key call on the route until the next boot."""
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(
                    service_keys=[
                        {"header_name": "X-Internal-Key", "header_value": NEW_SECRET}
                    ]
                ),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock_put.call_args[0][2]["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "X-Internal-Key": NEW_SECRET,
            INTERNAL_HEADER: INTERNAL_SECRET,
        }

    async def test_clearing_every_service_key_keeps_the_system_header(
        self, client, admin_token
    ):
        with _apisix(_provisioned_query_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/query-api",
                json=_query_body(service_keys=[]),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock_put.call_args[0][2]["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            INTERNAL_HEADER: INTERNAL_SECRET
        }

    async def test_llm_route_keeps_the_master_key_and_end_user_id(
        self, client, admin_token
    ):
        with _apisix(_provisioned_llm_route()) as mock_put:
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
        assert mock_put.call_args[0][2]["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "Authorization": MASTER_KEY,
            END_USER_HEADER: "$consumer_name",
            "X-Api-Key": NEW_SECRET,
        }

    @pytest.mark.parametrize(
        "header,vector",
        [
            (INTERNAL_HEADER, "forge the internal-proxy secret"),
            ("x-unibridge-internal-proxy", "same header, lowercased"),
            ("Authorization", "swap the LiteLLM master key"),
            ("authorization", "same header, lowercased"),
            (END_USER_HEADER, "misattribute another consumer's LLM spend"),
        ],
    )
    async def test_explicit_value_for_a_system_header_is_refused(
        self, client, admin_token, header, vector
    ):
        with _apisix(_provisioned_llm_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(
                    service_keys=[
                        {"header_name": header, "header_value": "attacker-value"}
                    ]
                ),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400, vector
        assert "startup provisioning" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    @pytest.mark.parametrize("value", ["", _mask_value(MASTER_KEY)])
    async def test_blank_or_masked_system_header_is_not_an_override(
        self, client, admin_token, value
    ):
        """The read view hands out a mask, and a round-trip resends it — that means
        "keep what is stored", so it must not read as an override attempt."""
        with _apisix(_provisioned_llm_route()) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(
                    service_keys=[{"header_name": "Authorization", "header_value": value}]
                ),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        headers_set = mock_put.call_args[0][2]["plugins"]["proxy-rewrite"]["headers"]["set"]
        assert headers_set["Authorization"] == MASTER_KEY

    async def test_custom_route_headers_keep_replace_wholesale_semantics(
        self, client, admin_token
    ):
        """The rule is scoped to system routes: an ordinary route's Authorization
        header is the operator's to set, and its old headers still give way."""
        existing = _route_with_secret(
            "custom-1", plugins={"proxy-rewrite": {"headers": {"set": {"X-Old": SECRET}}}}
        )
        with _apisix(existing) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/custom-1",
                json={
                    "name": "custom route",
                    "uri": "/api/thing/*",
                    "upstream_id": "u1",
                    "service_keys": [
                        {"header_name": "Authorization", "header_value": "Bearer mine"}
                    ],
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock_put.call_args[0][2]["plugins"]["proxy-rewrite"]["headers"]["set"] == {
            "Authorization": "Bearer mine"
        }


class TestSaveRouteUriShadowing:
    """A non-system route may not claim a uri a system route already serves."""

    @pytest.mark.parametrize(
        "uri,vector",
        [
            ("/api/query/exec", "capture query traffic on an exact path"),
            ("/api/query/*", "duplicate the query namespace"),
            ("/api/query", "sit on the namespace root"),
            ("/api/query/templates/x", "capture template writes"),
            ("/api/s3/list", "capture S3 traffic"),
            ("/api/nas/*", "duplicate the NAS namespace"),
            ("/api/usages", "shadow an exact system route"),
            ("/api/prometheus/api/v1/query", "capture PromQL traffic"),
            ("/api/llm/v1/messages", "shadow the converter route"),
            ("/api/llm/metrics", "shadow the LiteLLM metrics route"),
            ("/api/llm/chat/*", "capture LLM traffic (API keys ride along)"),
            ("/api/llm-admin/x", "reach the LiteLLM admin API"),
            ("/api/*", "swallow every namespace at once"),
            ("/api/q*", "partial-segment wildcard over /api/query"),
        ],
    )
    async def test_rejects_a_uri_inside_a_system_namespace(
        self, client, admin_token, uri, vector
    ):
        with _apisix(None) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/custom-1",
                json={"name": "custom route", "uri": uri, "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400, vector
        assert "system route namespace" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    @pytest.mark.parametrize(
        "uri",
        [
            "/api/myservice/*",
            "/api/myservice",
            "/api/queryless/*",
            "/api/llm-admin-extra/*",
            "/api/s3x/*",
            "/api/usages-report/*",
        ],
    )
    async def test_allows_an_unclaimed_prefix(self, client, admin_token, uri):
        """The guard must not be over-broad: a neighbouring name is not a nesting."""
        with _apisix(None, put_return={"id": "custom-1", "uri": uri}) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/custom-1",
                json={"name": "custom route", "uri": uri, "upstream_id": "u1"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200, uri
        mock_put.assert_awaited_once()

    async def test_guard_holds_when_apisix_lists_no_system_routes(
        self, client, admin_token
    ):
        """The namespaces come from the provisioning constants too, so an APISIX
        that has lost its routes cannot open them to a new one."""
        with _apisix(None) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/custom-1",
                json={
                    "name": "custom route",
                    "uri": "/api/query/exec",
                    "upstream_id": "u1",
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        mock_put.assert_not_awaited()

    async def test_guard_covers_a_system_uri_only_the_listing_knows(
        self, client, admin_token
    ):
        """A system route whose uri drifted from the constants is still protected."""
        drifted = _protected_route()
        drifted["uri"] = "/api/llm-v2/*"
        with _apisix(drifted) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/custom-1",
                json={
                    "name": "custom route",
                    "uri": "/api/llm-v2/chat",
                    "upstream_id": "u1",
                },
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        assert "/api/llm-v2/*" in resp.json()["detail"]
        mock_put.assert_not_awaited()

    async def test_system_route_may_still_save_its_own_uri(self, client, admin_token):
        """The guard applies to non-system ids only — llm-proxy owns /api/llm/*."""
        existing = _protected_route()
        with _apisix(existing) as mock_put:
            resp = await client.put(
                "/admin/gateway/routes/llm-proxy",
                json=_matching_body(),
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        mock_put.assert_awaited_once()


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
