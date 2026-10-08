"""Tests for per-node path prefixes on gateway upstreams.

Covers the validator and global-rule helpers in app/services/node_path_prefix.py
and every writer that uses them: PUT /admin/gateway/upstreams/{id}, the config
import's upstream section and the best-effort reconcile at boot.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.main import lifespan
from app.services.node_path_prefix import (
    GLOBAL_RULE_ID,
    MAX_PATH_PREFIX_LENGTH,
    MIXED_PREFIX_CHECKS,
    PLUGIN_NAME,
    RECREATE_APISIX_ADVICE,
    describe_apisix_error,
    ensure_global_rule,
    is_unknown_plugin_error,
    node_path_prefixes,
    plugin_unavailable_detail,
    uses_node_path_prefixes,
    validate_upstream_node_prefixes,
)
from tests.conftest import auth_header
from tests.test_main import _DummyTask, _fake_get_db

UNKNOWN_PLUGIN_BODY = '{"error_msg":"unknown plugin [unibridge-node-path-prefix]"}'
RULE_BODY = {"plugins": {PLUGIN_NAME: {}}}


def _http_status(code: int, body: str) -> httpx.HTTPStatusError:
    req = httpx.Request("PUT", "http://apisix/apisix/admin/global_rules/x")
    res = httpx.Response(code, request=req, text=body)
    return httpx.HTTPStatusError("err", request=req, response=res)


def _node(host: str, port: int | None = 8080, prefix=None, **extra) -> dict:
    node = {"host": host, "weight": 1, **extra}
    if port is not None:
        node["port"] = port
    if prefix is not None:
        node["metadata"] = {"path_prefix": prefix}
    return node


def _upstream(*nodes: dict, **fields) -> dict:
    return {"type": "roundrobin", "nodes": list(nodes), **fields}


# ── Validator ───────────────────────────────────────────────────────────────


class TestPrefixFormat:
    @pytest.mark.parametrize(
        "prefix",
        [
            "/api",
            "/api/v1",
            "/a-b_c.d~e",
            "/%2F",
            "/%2f/v1",
            "/.well-known",
            "/a:b@c",
            "/x;y=z",
            "/!$&'()*+,",
            "/" + "a" * (MAX_PATH_PREFIX_LENGTH - 1),
        ],
    )
    def test_accepts(self, prefix):
        upstream = _upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix=prefix))

        validate_upstream_node_prefixes(upstream)

        assert node_path_prefixes(upstream["nodes"]) == {"10.0.0.2:8080": prefix}

    @pytest.mark.parametrize(
        "prefix, problem",
        [
            ("api", 'must start with "/"'),
            ("/api/", 'must not end with "/"'),
            ("/", 'must not end with "/"'),
            ("//api", 'must not contain "//"'),
            ("/a//b", 'must not contain "//"'),
            ("/.", 'must not contain "." or ".." segments'),
            ("/./a", 'must not contain "." or ".." segments'),
            ("/a/..", 'must not contain "." or ".." segments'),
            # An escaped dot segment is still a dot segment to a decoding backend.
            ("/%2e%2E", 'must not contain "." or ".." segments'),
            ("/a?b", "may only contain"),
            ("/a#b", "may only contain"),
            ("/a b", "may only contain"),
            ("/a\tb", "may only contain"),
            # A `$`-anchored regex would let a trailing newline through.
            ("/a\n", "may only contain"),
            ("/%G1", "may only contain"),
            ("/%2", "may only contain"),
            ("/ä", "may only contain"),
            (
                "/" + "a" * MAX_PATH_PREFIX_LENGTH,
                f"must be at most {MAX_PATH_PREFIX_LENGTH} characters",
            ),
            ("", "must not be empty"),
            (123, "must be a string"),
            (None, "must be a string"),
            (["/api"], "must be a string"),
        ],
    )
    def test_rejects(self, prefix, problem):
        # metadata spelled out: _node(prefix=None) would mean "no prefix".
        upstream = _upstream(
            _node("10.0.0.1"), _node("10.0.0.2", metadata={"path_prefix": prefix})
        )

        with pytest.raises(ValueError) as excinfo:
            validate_upstream_node_prefixes(upstream)

        message = str(excinfo.value)
        assert message.startswith("Node 10.0.0.2:8080: path prefix ")
        assert problem in message


class TestUpstreamRules:
    @pytest.mark.parametrize("metadata", ["x", ["/api"], 3])
    def test_metadata_must_be_an_object(self, metadata):
        upstream = _upstream(_node("10.0.0.2", metadata=metadata))

        with pytest.raises(ValueError, match="Node 10.0.0.2:8080: metadata must be an object"):
            validate_upstream_node_prefixes(upstream)

    def test_node_without_host_is_named_by_position(self):
        upstream = _upstream(_node("10.0.0.1"), {"weight": 1, "metadata": "x"})

        with pytest.raises(ValueError, match="Node #2: metadata must be an object"):
            validate_upstream_node_prefixes(upstream)

    def test_null_metadata_is_left_to_apisix(self):
        validate_upstream_node_prefixes(_upstream(_node("10.0.0.2", metadata=None)))

    def test_other_metadata_keys_are_not_a_prefix(self):
        upstream = _upstream(_node("10.0.0.2", metadata={"zone": "a"}))

        validate_upstream_node_prefixes(upstream)

        assert not uses_node_path_prefixes(upstream)
        assert node_path_prefixes(upstream["nodes"]) == {}

    @pytest.mark.parametrize("scheme", ["grpc", "grpcs", "tcp", "kafka"])
    def test_prefix_needs_an_http_scheme(self, scheme):
        upstream = _upstream(_node("10.0.0.2", prefix="/api"), scheme=scheme)

        with pytest.raises(ValueError) as excinfo:
            validate_upstream_node_prefixes(upstream)

        assert "only work on http and https upstreams" in str(excinfo.value)
        assert f'"{scheme}"' in str(excinfo.value)

    @pytest.mark.parametrize("scheme", ["http", "https", None])
    def test_http_schemes_take_a_prefix(self, scheme):
        fields = {} if scheme is None else {"scheme": scheme}
        validate_upstream_node_prefixes(
            _upstream(_node("10.0.0.2", prefix="/api"), **fields)
        )

    def test_non_http_scheme_without_prefix_is_untouched(self):
        validate_upstream_node_prefixes(_upstream(_node("10.0.0.2"), scheme="grpc"))

    def test_duplicate_address_with_a_prefix_is_rejected(self):
        upstream = _upstream(_node("10.0.0.2"), _node("10.0.0.2", prefix="/api"))

        with pytest.raises(ValueError, match="10.0.0.2:8080 is listed more than once"):
            validate_upstream_node_prefixes(upstream)

    def test_duplicate_address_without_prefixes_is_left_to_apisix(self):
        validate_upstream_node_prefixes(
            _upstream(_node("10.0.0.2"), _node("10.0.0.2"))
        )

    @pytest.mark.parametrize(
        "first, second, fields, names",
        [
            # APISIX fills a missing port from the scheme.
            (_node("svc", port=None), _node("svc", port=80, prefix="/api"), {}, "svc and svc:80"),
            (
                _node("svc", port=None),
                _node("svc", port=443, prefix="/api"),
                {"scheme": "https"},
                "svc and svc:443",
            ),
            # IPv6 brackets and host case do not tell nodes apart either.
            (_node("[::1]", port=80), _node("::1", port=80, prefix="/api"), {}, "::1:80 and [::1]:80"),
            (
                _node("Svc.Internal"),
                _node("svc.internal", prefix="/api"),
                {},
                "Svc.Internal:8080 and svc.internal:8080",
            ),
        ],
    )
    def test_spellings_of_one_address_count_as_duplicates(self, first, second, fields, names):
        with pytest.raises(ValueError, match=f"Nodes {re.escape(names)} are the same host and port"):
            validate_upstream_node_prefixes(_upstream(first, second, **fields))

    def test_implicit_port_follows_the_scheme(self):
        # svc:80 (http default) and svc:443 are different nodes.
        validate_upstream_node_prefixes(
            _upstream(_node("svc", port=None), _node("svc", port=443, prefix="/api"))
        )

    def test_zero_weight_node_still_counts_as_a_duplicate(self):
        # APISIX and the plugin index every listed node, weight 0 included.
        upstream = _upstream(_node("10.0.0.2", weight=0), _node("10.0.0.2", prefix="/api"))

        with pytest.raises(ValueError, match="10.0.0.2:8080 is listed more than once"):
            validate_upstream_node_prefixes(upstream)

    @pytest.mark.parametrize("scheme", [["http"], {"name": "http"}, 1])
    def test_non_string_scheme_with_a_prefix_is_a_validation_error(self, scheme):
        with pytest.raises(ValueError, match="only work on http and https upstreams"):
            validate_upstream_node_prefixes(
                _upstream(_node("10.0.0.2", prefix="/api"), scheme=scheme)
            )

    @pytest.mark.parametrize(
        "upstream",
        [
            {"type": "roundrobin", "nodes": {"10.0.0.1:80": 1, "[::1]:8080": 2}},
            {"type": "roundrobin", "nodes": {}},
            {"type": "roundrobin"},
            {"type": "roundrobin", "nodes": None},
            {"type": "roundrobin", "nodes": ["10.0.0.1:80", 7]},
        ],
    )
    def test_upstreams_without_prefixes_pass_untouched(self, upstream):
        validate_upstream_node_prefixes(upstream)

        assert not uses_node_path_prefixes(upstream)
        assert node_path_prefixes(upstream.get("nodes")) == {}


class TestPrefixLookup:
    def test_maps_addresses_the_way_upstream_node_addresses_does(self):
        nodes = [
            _node("10.0.0.1"),
            _node("10.0.0.2", prefix="/api"),
            _node("svc.internal", port=None, prefix="/v2"),
            {"weight": 1, "metadata": {"path_prefix": "/no-host"}},
            "not-a-node",
        ]

        assert node_path_prefixes(nodes) == {"10.0.0.2:8080": "/api", "svc.internal": "/v2"}

    def test_uses_prefixes_detects_any_node(self):
        assert uses_node_path_prefixes(_upstream(_node("a"), _node("b", prefix="/api")))
        assert not uses_node_path_prefixes(_upstream(_node("a"), _node("b")))


# ── Global rule and error wording ───────────────────────────────────────────


async def test_ensure_global_rule_puts_the_plugin_rule():
    put_resource = AsyncMock(return_value={})
    with patch("app.services.apisix_client.put_resource", put_resource):
        await ensure_global_rule()

    put_resource.assert_awaited_once_with("global_rules", GLOBAL_RULE_ID, RULE_BODY)


class TestDescribeApisixError:
    def test_prefers_apisix_error_msg(self):
        assert (
            describe_apisix_error(_http_status(400, UNKNOWN_PLUGIN_BODY))
            == "unknown plugin [unibridge-node-path-prefix]"
        )

    def test_falls_back_to_the_body(self):
        assert describe_apisix_error(_http_status(502, "bad gateway")) == "bad gateway"
        assert describe_apisix_error(_http_status(400, '{"x": 1}')) == '{"x": 1}'
        assert describe_apisix_error(_http_status(500, "x" * 500)) == "x" * 200

    def test_empty_body_names_the_status(self):
        assert describe_apisix_error(_http_status(400, "")) == "HTTP 400"

    def test_transport_errors(self):
        assert describe_apisix_error(httpx.ConnectError("refused")) == "refused"
        assert describe_apisix_error(RuntimeError()) == "RuntimeError"


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_http_status(400, UNKNOWN_PLUGIN_BODY), True),
        (_http_status(400, '{"error_msg":"unknown plugin [other-plugin]"}'), False),
        (_http_status(400, '{"error_msg":"invalid configuration"}'), False),
        (_http_status(500, UNKNOWN_PLUGIN_BODY), False),
        (httpx.ConnectError("unknown plugin [unibridge-node-path-prefix]"), False),
    ],
)
def test_only_apisix_unknown_plugin_answer_means_the_plugin_is_missing(exc, expected):
    assert is_unknown_plugin_error(exc) is expected


def test_plugin_unavailable_detail_wording():
    exc = _http_status(400, UNKNOWN_PLUGIN_BODY)

    assert plugin_unavailable_detail(exc) == (
        "Per-node path prefixes need the unibridge-node-path-prefix plugin in "
        "APISIX (unknown plugin [unibridge-node-path-prefix]). Recreate the APISIX "
        "container from the current compose files, which mount the plugin and "
        "list it in apisix/config.yaml, then save again."
    )
    assert plugin_unavailable_detail(exc, retry="import again").endswith(
        "then import again."
    )


# ── PUT /admin/gateway/upstreams/{id} ───────────────────────────────────────


@pytest.fixture
def apisix_writes():
    """Records admin-API writes in order; ``failures`` makes chosen ones raise."""
    calls: list[tuple[str, str, dict]] = []
    failures: dict[tuple[str, str], Exception] = {}

    async def put_resource(resource: str, resource_id: str, body: dict) -> dict:
        calls.append((resource, resource_id, body))
        failure = failures.get((resource, resource_id))
        if failure is not None:
            raise failure
        return {**body, "id": resource_id}

    with patch(
        "app.services.apisix_client.put_resource", new=AsyncMock(side_effect=put_resource)
    ), patch(
        "app.services.apisix_client.get_resource",
        new=AsyncMock(side_effect=RuntimeError("404 not found")),
    ):
        yield SimpleNamespace(calls=calls, failures=failures)


class TestSaveUpstream:
    @pytest.mark.parametrize(
        "body",
        [
            {"type": "roundrobin", "nodes": {"10.0.0.1:8080": 1}},
            _upstream(_node("10.0.0.1"), _node("10.0.0.2", metadata={"zone": "a"})),
        ],
    )
    async def test_without_prefixes_the_global_rule_is_left_alone(
        self, client, admin_token, apisix_writes, body
    ):
        resp = await client.put(
            "/admin/gateway/upstreams/u1", json=body, headers=auth_header(admin_token)
        )

        assert resp.status_code == 200
        assert apisix_writes.calls == [("upstreams", "u1", body)]

    async def test_prefix_installs_the_global_rule_before_the_upstream(
        self, client, admin_token, apisix_writes
    ):
        body = _upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix="/api"))

        resp = await client.put(
            "/admin/gateway/upstreams/u1", json=body, headers=auth_header(admin_token)
        )

        assert resp.status_code == 200
        assert apisix_writes.calls == [
            ("global_rules", GLOBAL_RULE_ID, RULE_BODY),
            ("upstreams", "u1", {**body, "retries": 0, "checks": MIXED_PREFIX_CHECKS}),
        ]
        assert resp.json()["nodes"][1]["metadata"] == {"path_prefix": "/api"}

    async def test_missing_plugin_fails_the_save_without_writing_the_upstream(
        self, client, admin_token, apisix_writes
    ):
        apisix_writes.failures[("global_rules", GLOBAL_RULE_ID)] = _http_status(
            400, UNKNOWN_PLUGIN_BODY
        )
        body = _upstream(_node("10.0.0.2", prefix="/api"))

        resp = await client.put(
            "/admin/gateway/upstreams/u1", json=body, headers=auth_header(admin_token)
        )

        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "(unknown plugin [unibridge-node-path-prefix])" in detail
        assert detail.endswith(
            "list it in apisix/config.yaml, then save again."
        )
        assert [call[0] for call in apisix_writes.calls] == ["global_rules"]

    @pytest.mark.parametrize(
        "failure, detail",
        [
            (
                httpx.ConnectError("connection refused"),
                "Failed to connect to APISIX: connection refused",
            ),
            (httpx.ConnectTimeout(""), "Failed to connect to APISIX: ConnectTimeout"),
            (
                _http_status(500, "boom"),
                "APISIX refused the unibridge-node-path-prefix global rule (boom); "
                "nothing was saved.",
            ),
            (
                _http_status(400, '{"error_msg":"invalid configuration"}'),
                "APISIX refused the unibridge-node-path-prefix global rule "
                "(invalid configuration); nothing was saved.",
            ),
        ],
    )
    async def test_other_rule_failures_are_gateway_errors_not_a_missing_plugin(
        self, client, admin_token, apisix_writes, failure, detail
    ):
        # An outage must not tell the admin to redeploy APISIX.
        apisix_writes.failures[("global_rules", GLOBAL_RULE_ID)] = failure
        body = _upstream(_node("10.0.0.2", prefix="/api"))

        resp = await client.put(
            "/admin/gateway/upstreams/u1", json=body, headers=auth_header(admin_token)
        )

        assert resp.status_code == 502
        assert resp.json()["detail"] == detail
        assert [call[0] for call in apisix_writes.calls] == ["global_rules"]

    async def test_non_string_scheme_with_a_prefix_is_a_400(
        self, client, admin_token, apisix_writes
    ):
        body = _upstream(_node("10.0.0.2", prefix="/api"), scheme=["http"])

        resp = await client.put(
            "/admin/gateway/upstreams/u1", json=body, headers=auth_header(admin_token)
        )

        assert resp.status_code == 400
        assert apisix_writes.calls == []

    @pytest.mark.parametrize(
        "nodes, checks, retries",
        [
            # One node per path: every retry would cross paths and be refused, and
            # a refused one would use up the next node's turn.
            ((_node("10.0.0.1"), _node("10.0.0.2", prefix="/api")), True, 0),
            ((_node("10.0.0.1", prefix="/v1"), _node("10.0.0.2", prefix="/api")), True, 0),
            # A weight-0 node still counts: ewma ignores weights.
            ((_node("10.0.0.1"), _node("10.0.0.2", prefix="/api", weight=0)), True, 0),
            # Two nodes share /v1, so a failed /v1 node can still be retried on
            # its sibling: APISIX's retry default stays.
            (
                (_node("10.0.0.1", prefix="/v1"), _node("10.0.0.2", prefix="/v1"),
                 _node("10.0.0.3", prefix="/api")),
                True,
                None,
            ),
            # One path for every node: nothing to add.
            ((_node("10.0.0.1", prefix="/api"), _node("10.0.0.2", prefix="/api")), False, None),
            ((_node("10.0.0.1"), _node("10.0.0.2")), False, None),
        ],
    )
    async def test_mixed_prefixes_get_health_checks_and_retries_only_where_useful(
        self, client, admin_token, apisix_writes, nodes, checks, retries
    ):
        body = _upstream(*nodes)

        resp = await client.put(
            "/admin/gateway/upstreams/u1", json=body, headers=auth_header(admin_token)
        )

        assert resp.status_code == 200
        resource, _, stored = apisix_writes.calls[-1]
        assert resource == "upstreams"
        assert stored.get("checks") == (MIXED_PREFIX_CHECKS if checks else None)
        assert stored.get("retries") == retries

    def test_health_check_intervals_are_explicit(self):
        # APISIX stores checks as sent and the health-check library's own default
        # interval is 0, which never probes.
        active = MIXED_PREFIX_CHECKS["active"]
        assert active["type"] == "tcp"
        assert active["healthy"]["interval"] >= 1
        assert active["unhealthy"]["interval"] >= 1

    async def test_retries_and_checks_the_body_sets_are_kept(
        self, client, admin_token, apisix_writes
    ):
        checks = {"active": {"type": "http", "http_path": "/health", "healthy": {"interval": 5}}}
        body = _upstream(
            _node("10.0.0.1"), _node("10.0.0.2", prefix="/api"), retries=2, checks=checks
        )

        resp = await client.put(
            "/admin/gateway/upstreams/u1", json=body, headers=auth_header(admin_token)
        )

        assert resp.status_code == 200
        stored = apisix_writes.calls[-1][2]
        assert stored["retries"] == 2
        assert stored["checks"] == checks

    async def test_saved_defaults_do_not_leak_between_upstreams(
        self, client, admin_token, apisix_writes
    ):
        # The defaults are copied, so one stored upstream can never alias another's.
        for upstream_id in ("u1", "u2"):
            body = _upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix="/api"))
            resp = await client.put(
                f"/admin/gateway/upstreams/{upstream_id}", json=body, headers=auth_header(admin_token)
            )
            assert resp.status_code == 200
        first, second = (call[2]["checks"] for call in apisix_writes.calls if call[0] == "upstreams")
        assert first == second == MIXED_PREFIX_CHECKS
        assert first is not second and first is not MIXED_PREFIX_CHECKS
        # nested dicts too, so a deep copy and not a shallow one
        assert first["active"] is not second["active"]
        assert first["active"] is not MIXED_PREFIX_CHECKS["active"]
        assert first["active"]["healthy"] is not MIXED_PREFIX_CHECKS["active"]["healthy"]

    async def test_invalid_prefix_is_rejected_before_any_write(
        self, client, admin_token, apisix_writes
    ):
        body = _upstream(_node("10.0.0.2", prefix="api/"))

        resp = await client.put(
            "/admin/gateway/upstreams/u1", json=body, headers=auth_header(admin_token)
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == (
            'Node 10.0.0.2:8080: path prefix must start with "/".'
        )
        assert apisix_writes.calls == []

    async def test_system_upstream_is_still_refused_first(
        self, client, admin_token, apisix_writes
    ):
        body = _upstream(_node("attacker.example.com", prefix="/api"))

        resp = await client.put(
            "/admin/gateway/upstreams/unibridge-service",
            json=body,
            headers=auth_header(admin_token),
        )

        assert resp.status_code == 400
        assert "System-managed" in resp.json()["detail"]
        assert apisix_writes.calls == []


# ── Config import ───────────────────────────────────────────────────────────


@pytest.fixture
def apisix_state():
    """In-memory APISIX admin API with global rules and injectable PUT failures."""
    state: dict[str, dict[str, dict]] = {"routes": {}, "upstreams": {}, "global_rules": {}}
    failures: dict[tuple[str, str], Exception] = {}

    async def list_resources(resource: str) -> dict:
        items = [dict(item) for item in state[resource].values()]
        return {"items": items, "total": len(items)}

    async def get_resource(resource: str, resource_id: str) -> dict:
        if resource_id not in state[resource]:
            raise RuntimeError(f"APISIX 404 not found: {resource}/{resource_id}")
        return dict(state[resource][resource_id])

    async def put_resource(resource: str, resource_id: str, body: dict) -> dict:
        failure = failures.get((resource, resource_id))
        if failure is not None:
            raise failure
        state[resource][resource_id] = {**body, "id": resource_id}
        return dict(state[resource][resource_id])

    with patch(
        "app.services.apisix_client.list_resources", new=AsyncMock(side_effect=list_resources)
    ), patch(
        "app.services.apisix_client.get_resource", new=AsyncMock(side_effect=get_resource)
    ), patch(
        "app.services.apisix_client.put_resource", new=AsyncMock(side_effect=put_resource)
    ):
        yield SimpleNamespace(state=state, failures=failures)


async def _import_upstreams(client, token, upstreams: list[dict], *, dry_run: bool) -> dict:
    resp = await client.post(
        "/admin/config/import",
        json={
            "dry_run": dry_run,
            "sections": ["upstreams"],
            "data": {
                "unibridge_export_version": 1,
                "exported_at": "2026-10-07T00:00:00+00:00",
                "sections": {"upstreams": upstreams},
                "excluded": {},
            },
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    return {
        row["name"]: row
        for row in resp.json()["results"]
        if row["section"] == "upstreams"
    }


class TestConfigImport:
    async def test_prefixed_upstream_imports_with_the_global_rule(
        self, client, admin_token, apisix_state
    ):
        item = {"id": "svc-up", **_upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix="/api"))}

        rows = await _import_upstreams(client, admin_token, [item], dry_run=False)

        assert rows["svc-up"]["action"] == "create"
        assert apisix_state.state["global_rules"][GLOBAL_RULE_ID]["plugins"] == RULE_BODY["plugins"]
        stored = apisix_state.state["upstreams"]["svc-up"]
        assert stored["nodes"][1]["metadata"] == {"path_prefix": "/api"}

    async def test_upstream_without_prefix_does_not_create_the_rule(
        self, client, admin_token, apisix_state
    ):
        item = {"id": "svc-up", "type": "roundrobin", "nodes": {"10.0.0.1:8080": 1}}

        rows = await _import_upstreams(client, admin_token, [item], dry_run=False)

        assert rows["svc-up"]["action"] == "create"
        assert apisix_state.state["global_rules"] == {}

    async def test_invalid_prefix_fails_only_that_item(self, client, admin_token, apisix_state):
        bad = {"id": "bad-up", **_upstream(_node("10.0.0.2", prefix="/api?x=1"))}
        good = {"id": "good-up", "type": "roundrobin", "nodes": {"10.0.0.1:8080": 1}}

        rows = await _import_upstreams(client, admin_token, [bad, good], dry_run=False)

        assert rows["bad-up"]["action"] == "error"
        assert rows["bad-up"]["reason"].startswith("Node 10.0.0.2:8080: path prefix may only contain")
        assert rows["good-up"]["action"] == "create"
        assert set(apisix_state.state["upstreams"]) == {"good-up"}
        assert apisix_state.state["global_rules"] == {}

    async def test_dry_run_validates_but_never_writes(self, client, admin_token, apisix_state):
        good = {"id": "svc-up", **_upstream(_node("10.0.0.2", prefix="/api"))}
        bad = {"id": "bad-up", **_upstream(_node("10.0.0.2", prefix="/api/"))}

        rows = await _import_upstreams(client, admin_token, [good, bad], dry_run=True)

        assert rows["svc-up"]["action"] == "create"
        assert rows["bad-up"]["action"] == "error"
        assert 'must not end with "/"' in rows["bad-up"]["reason"]
        assert apisix_state.state["upstreams"] == {}
        assert apisix_state.state["global_rules"] == {}

    async def test_missing_plugin_fails_the_item_without_writing_it(
        self, client, admin_token, apisix_state
    ):
        apisix_state.failures[("global_rules", GLOBAL_RULE_ID)] = _http_status(
            400, UNKNOWN_PLUGIN_BODY
        )
        item = {"id": "svc-up", **_upstream(_node("10.0.0.2", prefix="/api"))}

        rows = await _import_upstreams(client, admin_token, [item], dry_run=False)

        assert rows["svc-up"]["action"] == "error"
        assert "(unknown plugin [unibridge-node-path-prefix])" in rows["svc-up"]["reason"]
        assert rows["svc-up"]["reason"].endswith("then import again.")
        assert apisix_state.state["upstreams"] == {}

    async def test_mixed_prefixes_import_with_health_checks_instead_of_retries(
        self, client, admin_token, apisix_state
    ):
        item = {"id": "svc-up", **_upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix="/api"))}

        rows = await _import_upstreams(client, admin_token, [item], dry_run=False)

        assert rows["svc-up"]["action"] == "create"
        stored = apisix_state.state["upstreams"]["svc-up"]
        assert stored["retries"] == 0
        assert stored["checks"] == MIXED_PREFIX_CHECKS

    async def test_import_keeps_retries_the_item_sets(self, client, admin_token, apisix_state):
        item = {
            "id": "svc-up",
            **_upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix="/api"), retries=3),
        }

        rows = await _import_upstreams(client, admin_token, [item], dry_run=False)

        assert rows["svc-up"]["action"] == "create"
        assert apisix_state.state["upstreams"]["svc-up"]["retries"] == 3

    async def test_import_keeps_checks_the_item_sets(self, client, admin_token, apisix_state):
        checks = {"active": {"type": "tcp", "healthy": {"interval": 5}, "unhealthy": {"interval": 5}}}
        item = {
            "id": "svc-up",
            **_upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix="/api"), checks=checks),
        }

        rows = await _import_upstreams(client, admin_token, [item], dry_run=False)

        assert rows["svc-up"]["action"] == "create"
        assert apisix_state.state["upstreams"]["svc-up"]["checks"] == checks

    async def test_unreachable_apisix_is_not_reported_as_a_missing_plugin(
        self, client, admin_token, apisix_state
    ):
        apisix_state.failures[("global_rules", GLOBAL_RULE_ID)] = httpx.ConnectError(
            "connection refused"
        )
        item = {"id": "svc-up", **_upstream(_node("10.0.0.2", prefix="/api"))}

        rows = await _import_upstreams(client, admin_token, [item], dry_run=False)

        assert rows["svc-up"]["action"] == "error"
        assert rows["svc-up"]["reason"] == "Failed to connect to APISIX: connection refused"
        assert apisix_state.state["upstreams"] == {}


async def _import_routes(client, token, routes: list[dict], *, dry_run: bool) -> dict:
    resp = await client.post(
        "/admin/config/import",
        json={
            "dry_run": dry_run,
            "sections": ["routes"],
            "data": {
                "unibridge_export_version": 1,
                "exported_at": "2026-10-07T00:00:00+00:00",
                "sections": {"routes": routes},
                "excluded": {},
            },
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    return {row["name"]: row for row in resp.json()["results"] if row["section"] == "routes"}


class TestRouteImportInlineUpstream:
    """save_route refuses inline upstreams, but an import takes them, so the
    upstream rules have to hold there too."""

    @staticmethod
    def _route(prefix: str) -> dict:
        return {
            "id": "inline-route",
            "uri": "/api/inline/*",
            "upstream": _upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix=prefix)),
        }

    @pytest.mark.parametrize("dry_run", [True, False])
    async def test_invalid_inline_prefix_fails_the_route(
        self, client, admin_token, apisix_state, dry_run
    ):
        rows = await _import_routes(client, admin_token, [self._route("/api/")], dry_run=dry_run)

        assert rows["inline-route"]["action"] == "error"
        assert 'must not end with "/"' in rows["inline-route"]["reason"]
        assert apisix_state.state["routes"] == {}
        assert apisix_state.state["global_rules"] == {}

    async def test_inline_prefix_installs_the_global_rule_first(
        self, client, admin_token, apisix_state
    ):
        rows = await _import_routes(client, admin_token, [self._route("/api")], dry_run=False)

        assert rows["inline-route"]["action"] == "create"
        assert GLOBAL_RULE_ID in apisix_state.state["global_rules"]
        inline = apisix_state.state["routes"]["inline-route"]["upstream"]
        assert inline["nodes"][1]["metadata"] == {"path_prefix": "/api"}
        assert (inline["retries"], inline["checks"]) == (0, MIXED_PREFIX_CHECKS)

    async def test_missing_plugin_fails_the_route_without_writing_it(
        self, client, admin_token, apisix_state
    ):
        apisix_state.failures[("global_rules", GLOBAL_RULE_ID)] = _http_status(
            400, UNKNOWN_PLUGIN_BODY
        )

        rows = await _import_routes(client, admin_token, [self._route("/api")], dry_run=False)

        assert rows["inline-route"]["action"] == "error"
        assert rows["inline-route"]["reason"].endswith("then import again.")
        assert apisix_state.state["routes"] == {}


# ── Boot ────────────────────────────────────────────────────────────────────


async def _run_lifespan(put_resource: AsyncMock, list_resources: AsyncMock | None = None) -> None:
    with (
        patch("app.main.validate_settings"),
        patch("app.main.init_db", new=AsyncMock()),
        patch("app.main.get_db", side_effect=lambda: _fake_get_db()),
        patch("app.main.connection_manager.initialize", new=AsyncMock()),
        patch("app.main.connection_manager.dispose_all", new=AsyncMock()),
        patch("app.main.settings_manager.load_from_db", new=AsyncMock()),
        patch("app.main.rate_limiter.update_limits"),
        patch(
            "app.main.settings",
            SimpleNamespace(LITELLM_MASTER_KEY="sk-test", APISIX_PROVISION_ON_START=False),
        ),
        patch("app.services.apisix_client.get_resource", AsyncMock()),
        patch("app.services.apisix_client.put_resource", put_resource),
        patch(
            "app.services.apisix_client.list_resources",
            list_resources or AsyncMock(return_value={"items": []}),
        ),
        patch("app.main.api_keys.sync_all_consumer_route_restrictions", AsyncMock()),
        patch(
            "app.services.alert_checker.start_checker",
            AsyncMock(return_value=_DummyTask()),
        ),
        patch("app.routers.alerts.set_alert_state"),
        patch("app.routers.users._kc_admin", None),
    ):
        async with lifespan(FastAPI()):
            pass


async def test_boot_installs_the_global_rule_after_prometheus():
    put_resource = AsyncMock(return_value={})

    await _run_lifespan(put_resource)

    calls = [call.args for call in put_resource.await_args_list]
    assert calls[1] == ("global_rules", GLOBAL_RULE_ID, RULE_BODY)
    assert calls[0][:2] == ("global_rules", "prometheus")


async def test_boot_continues_when_apisix_lacks_the_plugin(caplog):
    async def put_resource(resource, resource_id, body):
        if (resource, resource_id) == ("global_rules", GLOBAL_RULE_ID):
            raise _http_status(400, UNKNOWN_PLUGIN_BODY)
        return {}

    put_mock = AsyncMock(side_effect=put_resource)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _run_lifespan(put_mock)

    assert ("global_rules", GLOBAL_RULE_ID) in [
        call.args[:2] for call in put_mock.await_args_list
    ]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.main" and record.levelno == logging.WARNING
    ]
    assert any(
        "unibridge-node-path-prefix global rule" in message
        and "unknown plugin [unibridge-node-path-prefix]" in message
        and "Recreate the APISIX container" in message
        for message in warnings
    ), warnings


async def test_boot_flags_prefixed_upstreams_an_apisix_without_the_plugin_ignores(caplog):
    # APISIX skips a plugin it does not load, so stored prefixes silently stop
    # applying: that is an error worth naming the upstreams for, not a warning.
    async def put_resource(resource, resource_id, body):
        if (resource, resource_id) == ("global_rules", GLOBAL_RULE_ID):
            raise _http_status(400, UNKNOWN_PLUGIN_BODY)
        return {}

    async def list_resources(resource):
        if resource == "upstreams":
            return {"items": [
                {"id": "plain", "nodes": {"10.0.0.1:80": 1}},
                {"id": "mixed", **_upstream(_node("10.0.0.1"), _node("10.0.0.2", prefix="/api"))},
            ]}
        return {"items": [{"id": "inline", "upstream": _upstream(_node("10.0.0.3", prefix="/v2"))}]}

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _run_lifespan(
            AsyncMock(side_effect=put_resource), AsyncMock(side_effect=list_resources)
        )

    errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.main" and record.levelno == logging.ERROR
    ]
    assert len(errors) == 1, errors
    assert "route inline, upstream mixed set per-node path prefixes" in errors[0]
    assert "WITHOUT the prefix" in errors[0]


async def test_boot_does_not_blame_the_plugin_for_a_transport_error(caplog):
    async def put_resource(resource, resource_id, body):
        if (resource, resource_id) == ("global_rules", GLOBAL_RULE_ID):
            raise httpx.ConnectError("connection refused")
        return {}

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _run_lifespan(AsyncMock(side_effect=put_resource))

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.main" and "global rule" in record.getMessage()
    ]
    assert len(warnings) == 1, warnings
    assert "(connection refused)" in warnings[0]
    assert "tried again on the next boot" in warnings[0]
    assert RECREATE_APISIX_ADVICE not in warnings[0]
