"""External MCP servers relayed through /api/mcp (app/services/mcp_remote.py)."""
from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AdminAuditLog, ApiKeyAccess
from app.services import apisix_client, mcp_remote
from tests.conftest import auth_header
from tests.test_mcp import _call, _create_api_key, _put_settings, _rpc, _tool_names

URL = "http://mcp.internal:8080/mcp"
RID = "mcp-ext-docs"


def _tool(name: str, description: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"The {name} tool",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        **extra,
    }


class FakeMcp:
    """A Streamable HTTP MCP server: JSON or SSE answers, optional sessions."""

    def __init__(self) -> None:
        self.tools = [
            _tool("search", annotations={"readOnlyHint": True}),
            _tool("write"),
            _tool("bad.name"),
        ]
        self.sse = False
        self.stateful = False
        self.sessions: set[str] = set()
        self.server_request = False
        self.call_result: dict[str, Any] | None = None
        self.page_size = 2
        self.expired_status = 404
        self.requests: list[httpx.Request] = []
        self.client_replies: list[dict[str, Any]] = []
        self.methods: list[str] = []

    def _reply(self, message: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
        if not self.sse:
            return httpx.Response(200, json=message, headers=headers)
        events = []
        if self.server_request:
            events.append({"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage", "params": {}})
        events.append({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
        events.append(message)
        body = ": keepalive\n\n" + "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream", **(headers or {})}
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        session = request.headers.get("mcp-session-id")
        if request.method == "DELETE":
            self.sessions.discard(session or "")
            return httpx.Response(200)
        message = json.loads(request.content)
        if "method" not in message:
            self.client_replies.append(message)
            return httpx.Response(202)
        self.methods.append(message["method"])
        if message["method"] == "initialize":
            headers = {}
            if self.stateful:
                sid = f"s{len(self.sessions) + len(self.methods)}"
                self.sessions.add(sid)
                headers["mcp-session-id"] = sid
            return self._reply(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": message["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1"},
                    },
                },
                headers,
            )
        if self.stateful and session not in self.sessions:
            return httpx.Response(
                self.expired_status, json={"error": {"code": -32001, "message": "Session not found"}}
            )
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "tools/list":
            start = int(message.get("params", {}).get("cursor") or 0)
            page = self.tools[start : start + self.page_size]
            result: dict[str, Any] = {"tools": page}
            if start + self.page_size < len(self.tools):
                result["nextCursor"] = str(start + self.page_size)
            return self._reply({"jsonrpc": "2.0", "id": message["id"], "result": result})
        if message["method"] == "tools/call":
            params = message["params"]
            if params["name"] == "write" and params["arguments"].get("q") == "boom":
                return self._reply(
                    {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": "bad q"}}
                )
            result = self.call_result or {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "tool": params["name"],
                                "arguments": params["arguments"],
                                "path": request.url.path,
                                "authorization": request.headers.get("authorization"),
                                "apikey": request.headers.get("apikey"),
                                "relay": request.headers.get(mcp_remote.RELAY_HEADER),
                            }
                        ),
                    }
                ]
            }
            return self._reply({"jsonrpc": "2.0", "id": message["id"], "result": result})
        return self._reply(
            {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "nope"}}
        )


class FakeApisix:
    """The admin API's routes/upstreams, and the data plane for relay routes."""

    def __init__(self, mcp: FakeMcp) -> None:
        self.mcp = mcp
        self.store: dict[str, dict[str, dict[str, Any]]] = {"routes": {}, "upstreams": {}}
        self.counted: list[tuple[str, str]] = []

    async def get_resource(self, resource: str, resource_id: str) -> dict[str, Any]:
        item = self.store[resource].get(resource_id)
        if item is None:
            request = httpx.Request("GET", f"http://apisix:9180/{resource}/{resource_id}")
            raise httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))
        return copy.deepcopy(item)

    async def put_resource(self, resource: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.store[resource][resource_id] = {**copy.deepcopy(body), "id": resource_id}
        return copy.deepcopy(self.store[resource][resource_id])

    async def delete_resource(self, resource: str, resource_id: str) -> None:
        await self.get_resource(resource, resource_id)
        del self.store[resource][resource_id]

    async def list_resources(self, resource: str) -> dict[str, Any]:
        return {"items": [copy.deepcopy(item) for item in self.store[resource].values()]}

    def whitelist(self, rid: str) -> list[str]:
        return self.store["routes"][rid]["plugins"]["consumer-restriction"]["whitelist"]

    def relay(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.removeprefix(mcp_remote.RELAY_PATH_PREFIX)
        rid = mcp_remote.route_id(name)
        route = self.store["routes"].get(rid)
        if (
            route is None
            or route.get("status") == 0
            or request.method not in route["methods"]
            or request.url.path != route["uri"]
        ):
            return httpx.Response(404, json={"error_msg": "404 Route Not Found"})
        var, op, expected = route["vars"][0]
        assert op == "=="
        if request.headers.get(var.removeprefix("http_").replace("_", "-")) != expected:
            return httpx.Response(404, json={"error_msg": "404 Route Not Found"})
        key = request.headers.get("apikey", "")
        if not key.startswith("key:"):
            return httpx.Response(401, json={"message": "Invalid API key in request"})
        if key.removeprefix("key:") not in self.whitelist(rid):
            return httpx.Response(403, json={"message": "The consumer_name is forbidden."})
        self.counted.append((rid, request.method))
        plugins = route["plugins"]
        assert plugins["key-auth"] == {"hide_credentials": True}
        rewrite = plugins["proxy-rewrite"]
        headers = {k: v for k, v in request.headers.items() if k.lower() not in {"apikey", "host"}}
        for removed in rewrite["headers"].get("remove", []):
            headers = {k: v for k, v in headers.items() if k.lower() != removed.lower()}
        headers.update(rewrite["headers"].get("set", {}))
        upstream = self.store["upstreams"][route["upstream_id"]]
        (node,) = upstream["nodes"]
        forwarded = httpx.Request(
            request.method,
            f"{upstream['scheme']}://{node}{rewrite['uri']}",
            headers=headers,
            content=request.content,
        )
        return self.mcp.handle(forwarded)


class Net(httpx.AsyncBaseTransport):
    def __init__(self, apisix: FakeApisix) -> None:
        self.apisix = apisix
        self.direct: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        if request.url.host == "apisix":
            return self.apisix.relay(request)
        if request.url.host == "mcp.internal":
            self.direct.append(request)
            return self.apisix.mcp.handle(request)
        raise httpx.ConnectError("unreachable", request=request)


@pytest.fixture
async def net():
    mcp_remote._sessions.clear()
    mcp = FakeMcp()
    apisix = FakeApisix(mcp)
    transport = Net(apisix)
    async with httpx.AsyncClient(transport=transport, base_url="http://apisix:9080") as gateway_client:
        with patch.object(apisix_client, "get_resource", apisix.get_resource), patch.object(
            apisix_client, "put_resource", apisix.put_resource
        ), patch.object(apisix_client, "delete_resource", apisix.delete_resource), patch.object(
            apisix_client, "list_resources", apisix.list_resources
        ), patch(
            "app.services.mcp_tools._gateway_client", return_value=gateway_client
        ), patch(
            "app.services.mcp_remote._direct_client",
            side_effect=lambda: httpx.AsyncClient(transport=transport),
        ):
            yield transport
    mcp_remote._sessions.clear()


def _server_body(**changes: Any) -> dict[str, Any]:
    return {
        "name": "docs",
        "url": URL,
        "description": "Docs search",
        "enabled": True,
        "timeout_seconds": 30,
        "headers": [{"name": "Authorization", "value": "Bearer upstream-secret-1234"}],
        **changes,
    }


async def _register(client, admin_token, *, approve: list[str] | None = None, **changes: Any) -> dict:
    resp = await client.post("/admin/mcp/servers", json=_server_body(**changes), headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.text
    resp = await client.post(f"/admin/mcp/servers/{resp.json()['name']}/refresh", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    if approve is not None:
        digests = {tool["name"]: tool["digest"] for tool in resp.json()["tools"]}
        resp = await client.put(
            f"/admin/mcp/servers/{resp.json()['name']}/tools",
            json={"approved": [{"name": name, "digest": digests[name]} for name in approve]},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text
    return resp.json()


async def _key(seeded_db, net: Net, name: str = "agent", *, granted: bool = True) -> dict[str, str]:
    headers = await _create_api_key(seeded_db, name, routes=["mcp-api", RID] if granted else ["mcp-api"])
    if granted:  # what the API-key route sync does on the gateway
        net.apisix.whitelist(RID).append(name)
    return headers


# ── Admin ───────────────────────────────────────────────────────────────────


async def test_registering_writes_a_relay_route_clients_cannot_reach(client, admin_token, net):
    resp = await client.post("/admin/mcp/servers", json=_server_body(), headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.text
    info = resp.json()
    assert info["route_id"] == RID
    assert info["headers"] == [{"name": "Authorization", "value": "***1234"}]
    assert info["tools"] == []

    upstream = net.apisix.store["upstreams"][RID]
    assert upstream["scheme"] == "http"
    assert upstream["nodes"] == {"mcp.internal:8080": 1}
    assert upstream["pass_host"] == "node"
    route = net.apisix.store["routes"][RID]
    assert route["uri"] == "/_mcp-ext/docs"  # outside /api/: nginx never forwards it
    assert route["upstream_id"] == RID
    assert route["status"] == 1
    assert route["timeout"]["read"] == 30
    (var,) = route["vars"]
    assert var[0] == "http_x_unibridge_mcp_relay" and len(var[2]) > 30
    plugins = route["plugins"]
    assert plugins["key-auth"] == {"hide_credentials": True}
    assert plugins["consumer-restriction"] == {"whitelist": ["__deny_all__"]}
    assert plugins["proxy-rewrite"] == {
        "uri": "/mcp",
        "headers": {
            "remove": [
                mcp_remote.RELAY_HEADER,
                "X-Consumer-Username",
                "X-Credential-Identifier",
                "X-Consumer-Custom-Id",
            ],
            "set": {"Authorization": "Bearer upstream-secret-1234"},
        },
    }

    resp = await client.post("/admin/mcp/servers", json=_server_body(), headers=auth_header(admin_token))
    assert resp.status_code == 409


async def test_a_failed_route_write_leaves_no_server_behind(client, admin_token, net):
    # The first server also creates the relay secret: in its own transaction.
    with patch.object(apisix_client, "put_resource", side_effect=OSError("apisix down")):
        resp = await client.post("/admin/mcp/servers", json=_server_body(), headers=auth_header(admin_token))
    assert resp.status_code == 502
    assert (await client.get("/admin/mcp/servers", headers=auth_header(admin_token))).json() == []
    resp = await client.post("/admin/mcp/servers", json=_server_body(), headers=auth_header(admin_token))
    assert resp.status_code == 201


async def test_master_keys_get_a_new_route(client, admin_token, seeded_db, net):
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        db.add(ApiKeyAccess(consumer_name="root", allowed_databases='["*"]', allowed_routes='["*"]'))
        await db.commit()
    await client.post("/admin/mcp/servers", json=_server_body(), headers=auth_header(admin_token))
    assert net.apisix.whitelist(RID) == ["root"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": "Docs"}, "pattern"),
        ({"name": "a" * 17}, "pattern"),
        ({"name": "my__x"}, "pattern"),
        ({"url": "ftp://x/mcp"}, "http"),
        ({"url": "https://u:p@x/mcp"}, "header"),
        ({"url": "https://x/mcp?key=1"}, "query"),
        ({"url": "https://x/$uri"}, "$"),
        ({"url": "https://x/a%2Fb"}, "percent"),
        ({"headers": [{"name": "Authorization", "value": "Bearer $http_x_unibridge_mcp_relay"}]}, "'$'"),
        ({"headers": [{"name": "apikey", "value": "x"}]}, "set by UniBridge"),
        ({"headers": [{"name": "X-UniBridge-MCP-Relay", "value": "x"}]}, "set by UniBridge"),
        ({"headers": [{"name": "A", "value": "1"}, {"name": "a", "value": "2"}]}, "once"),
        ({"headers": [{"name": "A", "value": "x\r\nB: y"}]}, "control"),
        ({"headers": [{"name": "A", "value": "토큰"}]}, "ASCII"),
        ({"timeout_seconds": 1}, "greater"),
    ],
)
async def test_server_entries_are_validated(client, admin_token, net, changes, message):
    resp = await client.post("/admin/mcp/servers", json=_server_body(**changes), headers=auth_header(admin_token))
    assert resp.status_code == 422, resp.text
    assert message in resp.text


async def test_server_admin_needs_config_permissions(client, user_token, net):
    assert (await client.get("/admin/mcp/servers", headers=auth_header(user_token))).status_code == 403
    resp = await client.post("/admin/mcp/servers", json=_server_body(), headers=auth_header(user_token))
    assert resp.status_code == 403


async def test_update_keeps_masked_secrets_and_the_whitelist(client, admin_token, net):
    info = await _register(client, admin_token, approve=["search"])
    net.apisix.whitelist(RID).append("agent")
    body = _server_body(timeout_seconds=90, enabled=False)
    body["headers"] = [*info["headers"], {"name": "X-Tenant", "value": "t1"}]
    resp = await client.put("/admin/mcp/servers/docs", json={k: v for k, v in body.items() if k != "name"}, headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    route = net.apisix.store["routes"][RID]
    assert route["plugins"]["proxy-rewrite"]["headers"]["set"] == {
        "Authorization": "Bearer upstream-secret-1234",
        "X-Tenant": "t1",
    }
    assert route["status"] == 0
    assert route["timeout"]["read"] == 90
    assert "agent" in net.apisix.whitelist(RID)
    # Same URL: the listing and approvals stay.
    assert [t["status"] for t in resp.json()["tools"]] == ["approved", "new", "invalid_name"]

    # A masked value for a header that has nothing stored is refused.
    body["headers"] = [{"name": "X-Other", "value": "***abcd"}]
    resp = await client.put("/admin/mcp/servers/docs", json={k: v for k, v in body.items() if k != "name"}, headers=auth_header(admin_token))
    assert resp.status_code == 400

    # Another origin: the stored secret is not sent there unless typed again.
    body = _server_body(url="https://other.internal/v2/mcp")
    body["headers"] = [{"name": "Authorization", "value": "***1234"}]
    resp = await client.put("/admin/mcp/servers/docs", json={k: v for k, v in body.items() if k != "name"}, headers=auth_header(admin_token))
    assert resp.status_code == 400
    assert "enter the value again" in resp.json()["detail"]
    body["headers"] = [{"name": "Authorization", "value": "Bearer other-secret"}]
    resp = await client.put("/admin/mcp/servers/docs", json={k: v for k, v in body.items() if k != "name"}, headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert resp.json()["tools"] == [] and resp.json()["refreshed_at"] is None
    assert net.apisix.store["upstreams"][RID]["nodes"] == {"other.internal:443": 1}
    assert net.apisix.store["upstreams"][RID]["scheme"] == "https"
    assert net.apisix.store["routes"][RID]["plugins"]["proxy-rewrite"]["uri"] == "/v2/mcp"


async def test_audit_log_never_holds_the_upstream_secret(client, admin_token, seeded_db, net):
    await _register(client, admin_token, approve=["search"])
    await client.delete("/admin/mcp/servers/docs", headers=auth_header(admin_token))
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        rows = (
            await db.execute(select(AdminAuditLog).where(AdminAuditLog.resource_type.like("mcp_server%")))
        ).scalars().all()
    assert {(row.action, row.resource_type) for row in rows} == {
        ("create", "mcp_server"),
        ("update", "mcp_server_tools"),
        ("delete", "mcp_server"),
    }
    for row in rows:
        assert "upstream-secret" not in (row.before or "") + (row.after or "")


async def test_refresh_lists_every_page_and_approval_follows_digests(client, admin_token, net):
    net.apisix.mcp.stateful = True
    info = await _register(client, admin_token)
    assert [(t["name"], t["exposed_name"], t["status"]) for t in info["tools"]] == [
        ("search", "docs__search", "new"),
        ("write", "docs__write", "new"),
        ("bad.name", "docs__bad.name", "invalid_name"),
    ]
    assert info["tools"][0]["read_only"] is True
    # Fetched straight from the server with its own headers, session closed after.
    direct = net.direct
    assert all(r.headers["authorization"] == "Bearer upstream-secret-1234" for r in direct)
    assert all("apikey" not in r.headers for r in direct)
    assert net.apisix.mcp.methods == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    assert direct[-1].method == "DELETE"

    seen = {tool["name"]: tool["digest"] for tool in info["tools"]}

    async def approve(*names: str, digests: dict[str, str] = seen):
        return await client.put(
            "/admin/mcp/servers/docs/tools",
            json={"approved": [{"name": n, "digest": digests.get(n, "x")} for n in names]},
            headers=auth_header(admin_token),
        )

    assert (await approve("bad.name")).status_code == 400
    assert (await approve("missing")).status_code == 400
    resp = await approve("search", "write")
    assert [t["status"] for t in resp.json()["tools"]] == ["approved", "approved", "invalid_name"]
    assert all(t["approved_version"] is None for t in resp.json()["tools"])

    # The server changes one tool and drops another: the changed one waits for
    # approval again, showing what was approved; the dropped one comes back as new.
    net.apisix.mcp.tools = [_tool("search", "Now also deletes things"), _tool("bad.name")]
    resp = await client.post("/admin/mcp/servers/docs/refresh", headers=auth_header(admin_token))
    assert [t["status"] for t in resp.json()["tools"]] == ["changed", "invalid_name"]
    changed = resp.json()["tools"][0]
    assert changed["description"] == "Now also deletes things"
    assert changed["approved_version"]["description"] == "The search tool"
    net.apisix.mcp.tools = [_tool("search", "Now also deletes things"), _tool("write")]
    resp = await client.post("/admin/mcp/servers/docs/refresh", headers=auth_header(admin_token))
    assert [t["status"] for t in resp.json()["tools"]] == ["changed", "new"]

    # Approving with the digest from before the refresh approves nothing.
    resp = await approve("search")
    assert resp.status_code == 409
    assert "search" in resp.json()["detail"]
    current = {tool["name"]: tool["digest"] for tool in resp_tools(await client.get("/admin/mcp/servers", headers=auth_header(admin_token)))}
    resp = await approve("search", digests=current)
    assert [t["status"] for t in resp.json()["tools"]] == ["approved", "new"]


def resp_tools(resp: httpx.Response) -> list[dict[str, Any]]:
    (server,) = resp.json()
    return server["tools"]


async def test_refresh_failure_is_kept_as_last_error(client, admin_token, net):
    await client.post("/admin/mcp/servers", json=_server_body(url="http://down.internal/mcp"), headers=auth_header(admin_token))
    resp = await client.post("/admin/mcp/servers/docs/refresh", headers=auth_header(admin_token))
    assert resp.status_code == 502
    assert "Could not reach" in resp.json()["detail"]
    (info,) = (await client.get("/admin/mcp/servers", headers=auth_header(admin_token))).json()
    assert "Could not reach" in info["last_error"]


async def test_delete_removes_the_route_and_every_grant(client, admin_token, seeded_db, net):
    await _register(client, admin_token)
    await _key(seeded_db, net)
    resp = await client.delete("/admin/mcp/servers/docs", headers=auth_header(admin_token))
    assert resp.status_code == 204
    assert net.apisix.store == {"routes": {}, "upstreams": {}}
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        access = (await db.execute(select(ApiKeyAccess))).scalar_one()
    assert json.loads(access.allowed_routes) == ["mcp-api"]
    assert (await client.delete("/admin/mcp/servers/docs", headers=auth_header(admin_token))).status_code == 404


async def test_boot_provisioning_restores_routes(client, admin_token, seeded_db, net):
    await _register(client, admin_token)
    net.apisix.whitelist(RID).append("agent")
    del net.apisix.store["upstreams"][RID]
    session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await mcp_remote.provision_all(db)
    assert set(net.apisix.store["upstreams"]) == {RID}
    # The route's grants survive the rewrite.
    assert "agent" in net.apisix.whitelist(RID)
    net.apisix.store = {"routes": {}, "upstreams": {}}  # an etcd reset
    async with session_factory() as db:
        await mcp_remote.provision_all(db)
    assert set(net.apisix.store["routes"]) == {RID}


async def test_gateway_editor_and_config_transfer_leave_relay_routes_alone(client, admin_token, net):
    await _register(client, admin_token)
    headers = auth_header(admin_token)
    resp = await client.put(f"/admin/gateway/routes/{RID}", json={"uri": "/api/x", "upstream_id": "u"}, headers=headers)
    assert resp.status_code == 400
    assert (await client.delete(f"/admin/gateway/routes/{RID}", headers=headers)).status_code == 400
    resp = await client.put(f"/admin/gateway/upstreams/{RID}", json={"nodes": {"evil:80": 1}}, headers=headers)
    assert resp.status_code == 400
    assert (await client.delete(f"/admin/gateway/upstreams/{RID}", headers=headers)).status_code == 400
    routes = (await client.get("/admin/gateway/routes", headers=headers)).json()["items"]
    (route,) = [r for r in routes if r["id"] == RID]
    assert route["system"] is True
    # Neither the relay secret nor the upstream's credentials show.
    secret = net.apisix.store["routes"][RID]["vars"][0][2]
    view = json.dumps(route) + (await client.get(f"/admin/gateway/routes/{RID}", headers=headers)).text
    assert secret not in view and "upstream-secret" not in view


# ── /api/mcp ────────────────────────────────────────────────────────────────


async def test_tools_list_offers_approved_tools_to_granted_keys(client, admin_token, seeded_db, net):
    await _register(client, admin_token, approve=["search"])
    granted = await _key(seeded_db, net)
    plain = await _key(seeded_db, net, "plain", granted=False)

    resp = await _rpc(client, granted, "tools/list")
    tools = {tool["name"]: tool for tool in resp.json()["result"]["tools"]}
    assert set(tools) == {"docs__search"}
    tool = tools["docs__search"]
    assert tool["title"] == "docs: search"
    assert tool["inputSchema"] == _tool("search")["inputSchema"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert await _tool_names(client, plain) == []
    # tools/list is served from the stored listing: nothing went out.
    assert net.apisix.counted == []

    await _put_settings(client, admin_token, external=False)
    assert await _tool_names(client, granted) == []
    await _put_settings(client, admin_token, external=True)
    body = _server_body(enabled=False)
    del body["name"]
    body["headers"] = [{"name": "Authorization", "value": "***1234"}]
    await client.put("/admin/mcp/servers/docs", json=body, headers=auth_header(admin_token))
    assert await _tool_names(client, granted) == []


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
async def test_tool_calls_go_through_the_relay_as_the_caller(client, admin_token, seeded_db, net, sse):
    await _register(client, admin_token, approve=["search", "write"])
    headers = await _key(seeded_db, net)
    mcp = net.apisix.mcp
    mcp.sse = sse
    mcp.stateful = True
    mcp.server_request = sse
    mcp.methods.clear()

    result = await _call(client, headers, "docs__search", {"q": "hello", "extra": [1, 2]})
    assert result["isError"] is False
    seen = json.loads(result["content"][0]["text"])
    assert seen == {
        "tool": "search",
        "arguments": {"q": "hello", "extra": [1, 2]},  # passed on unchecked
        "path": "/mcp",
        "authorization": "Bearer upstream-secret-1234",
        "apikey": None,
        "relay": None,
    }
    await _call(client, headers, "docs__write", {"q": "x"})
    # One session per key: initialize once, then a single request per call.
    assert mcp.methods == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    # The refusals below go back on the relay too, so they count.
    assert net.apisix.counted == [(RID, "POST")] * (7 if sse else 4)
    if sse:
        # The sampling request in each streamed answer is refused, not left hanging.
        assert [reply["error"]["code"] for reply in mcp.client_replies] == [-32601] * 3


@pytest.mark.parametrize("expired_status", [404, 400])
async def test_an_ended_session_is_opened_again(client, admin_token, seeded_db, net, expired_status):
    await _register(client, admin_token, approve=["search"])
    headers = await _key(seeded_db, net)
    mcp = net.apisix.mcp
    mcp.stateful = True
    mcp.expired_status = expired_status
    await _call(client, headers, "docs__search", {"q": "1"})
    mcp.sessions.clear()  # the server restarted
    mcp.methods.clear()
    result = await _call(client, headers, "docs__search", {"q": "2"})
    assert result["isError"] is False
    assert mcp.methods == ["tools/call", "initialize", "notifications/initialized", "tools/call"]


async def test_relay_refusals_and_server_errors_reach_the_model(client, admin_token, seeded_db, net):
    await _register(client, admin_token, approve=["search", "write"])
    headers = await _key(seeded_db, net)

    result = await _call(client, headers, "docs__write", {"q": "boom"})
    assert result["isError"] is True
    assert result["content"][0]["text"] == "docs: MCP error -32602: bad q"

    net.apisix.whitelist(RID).remove("agent")  # the grant was revoked at the gateway
    mcp_remote._sessions.clear()
    result = await _call(client, headers, "docs__search", {"q": "x"})
    assert result["isError"] is True
    assert "HTTP 403: The consumer_name is forbidden." in result["content"][0]["text"]

    result = await _call(client, {**headers, "apikey": ""}, "docs__search", {"q": "x"})
    assert result["isError"] is True


async def test_results_are_cut_to_max_result_bytes(client, admin_token, seeded_db, net):
    await _register(client, admin_token, approve=["search"])
    headers = await _key(seeded_db, net)
    await _put_settings(client, admin_token, max_result_bytes=2048)
    net.apisix.mcp.call_result = {
        "content": [
            {"type": "text", "text": "a" * 500},
            {"type": "text", "text": "é" * 2000},
            {"type": "image", "data": "A" * 4000, "mimeType": "image/png"},
        ],
        "structuredContent": {"big": "x" * 3000},
        "isError": False,
    }
    result = await _call(client, headers, "docs__search", {"q": "x"})
    texts = [item["text"] for item in result["content"]]
    assert texts[0] == "a" * 500
    assert texts[1].endswith("[truncated at max_result_bytes]")
    assert texts[2] == "[image item omitted: over max_result_bytes]"
    assert "structuredContent" not in result
    assert len(json.dumps(result["content"], ensure_ascii=False).encode()) < 2048 + 300


async def test_a_streamed_answer_is_capped(client, admin_token, seeded_db, net):
    await _register(client, admin_token, approve=["search"])
    headers = await _key(seeded_db, net)
    await _put_settings(client, admin_token, max_result_bytes=1024)
    mcp = net.apisix.mcp
    mcp.sse = True
    mcp.call_result = {"content": [{"type": "text", "text": "x" * 200_000}]}
    result = await _call(client, headers, "docs__search", {"q": "x"})
    assert result["isError"] is True
    assert "The answer is over" in result["content"][0]["text"]


async def test_gateway_route_test_skips_relay_routes(client, admin_token, net):
    await _register(client, admin_token)
    resp = await client.post(f"/admin/gateway/routes/{RID}/test", headers=auth_header(admin_token))
    assert resp.status_code == 400


async def test_preview_lists_one_row_per_server(client, admin_token, seeded_db, net):
    await _register(client, admin_token, approve=["search"])
    await _key(seeded_db, net)
    resp = await client.get("/admin/mcp/preview", params={"consumer": "agent"}, headers=auth_header(admin_token))
    preview = resp.json()
    external = [group for group in preview["groups"] if group["group"] == "external"]
    assert external == [
        {"group": "external", "enabled": True, "granted": True, "required_route": RID, "server": "docs"}
    ]
    assert [(tool["name"], tool["server"]) for tool in preview["tools"] if tool["group"] == "external"] == [
        ("docs__search", "docs")
    ]
