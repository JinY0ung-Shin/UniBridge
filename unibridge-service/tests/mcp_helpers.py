"""Helpers shared by the MCP test modules (test_mcp*.py).

The ``gateway`` fixture that installs :class:`FakeGateway` lives in conftest.py.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from app.services import mcp_tools
from tests.conftest import auth_header
from tests.helpers import insert_api_key

ALL_ROUTES = ["mcp-api", "query-api", "s3-api", "nas-api"]


class FakeGateway(httpx.AsyncBaseTransport):
    """APISIX stand-in for the tool calls MCP sends back out.

    key-auth: ``apikey: key:<consumer>`` authenticates as <consumer>; the
    route prefixes are rewritten like the fixed routes' proxy-rewrite, and each
    call is recorded with the route APISIX would count it on.
    """

    _ROUTES = (
        (b"/api/query/", b"/query/", "query-api"),
        (b"/api/s3/", b"/s3/", "s3-api"),
        (b"/api/nas/", b"/nas/", "nas-api"),
        # Split by method like the fixed routes: GET reads, the rest writes.
        (b"/api/network/", b"/network/", "network-api"),
    )

    def __init__(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.calls: list[tuple[str, str, str]] = []
        self.reply: httpx.Response | None = None
        # Requests to the stand-in external APIs under /api/ext/.
        self.external: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.startswith(b"/api/ext/"):
            await request.aread()
            self.external.append(request)
            self.calls.append(("ext", request.method, request.url.path))
            if self.reply is not None:
                return self.reply
            return httpx.Response(
                200,
                json={"method": request.method, "path": request.url.path},
                headers={"content-type": "application/json"},
            )
        key = request.headers.get("apikey", "")
        if not key.startswith("key:"):
            return httpx.Response(401, json={"message": "Invalid API key in request"})
        raw = request.url.raw_path
        for prefix, target, route in self._ROUTES:
            if raw.startswith(prefix):
                if route == "network-api" and request.method != "GET":
                    route = "network-write-api"
                self.calls.append((route, request.method, request.url.path))
                break
        else:
            return httpx.Response(404, json={"error_msg": "404 Route Not Found"})
        if self.reply is not None:
            return self.reply
        headers = {k: v for k, v in request.headers.items() if k != "apikey"}
        headers["x-consumer-username"] = key.removeprefix("key:")
        forwarded = httpx.Request(
            request.method,
            request.url.copy_with(raw_path=target + raw[len(prefix):]),
            headers=headers,
            content=await request.aread(),
        )
        return await self.inner.handle_async_request(forwarded)


async def create_mcp_key(
    seeded_db,
    name: str = "agent",
    *,
    routes: list[str] | None = None,
    databases: list[str] | None = None,
    tables: list[str] | None = None,
    allow_insert: bool = False,
) -> dict[str, str]:
    """An API key granted ``routes`` (default: ALL_ROUTES) on ``databases``
    (default: maindb), and the headers APISIX hands the app on /api/mcp for it:
    the consumer it authenticated (trusted without the proxy secret in
    dev-token mode) and the key itself."""
    headers = await insert_api_key(
        seeded_db,
        name,
        allowed_databases=["maindb"] if databases is None else databases,
        allowed_routes=ALL_ROUTES if routes is None else routes,
        allowed_tables=tables,
        allow_insert=allow_insert,
    )
    return {**headers, "apikey": f"key:{name}"}


async def mcp_rpc(client, headers, method: str, params: dict | None = None, request_id: Any = 1):
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return await client.post("/mcp", json=message, headers=headers)


async def mcp_tool_names(client, headers) -> list[str]:
    resp = await mcp_rpc(client, headers, "tools/list")
    assert resp.status_code == 200, resp.text
    return [tool["name"] for tool in resp.json()["result"]["tools"]]


async def mcp_call(client, headers, name: str, arguments: dict) -> dict:
    resp = await mcp_rpc(client, headers, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def mcp_text(result: dict, index: int = 0) -> str:
    return result["content"][index]["text"]


async def put_mcp_settings(client, admin_token, **changes) -> None:
    body = mcp_tools.McpSettings().model_dump()
    for key, value in changes.items():
        if key in body["tool_groups"]:
            body["tool_groups"][key] = value
        else:
            body[key] = value
    resp = await client.put("/admin/mcp/settings", json=body, headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text


# ── External APIs (search_apis / call_api) ──────────────────────────────────

EXT_ROUTES = [
    {
        "id": "orders",
        "name": "Orders",
        "uri": "/api/ext/orders/*",
        "methods": ["GET", "POST", "DELETE"],
        "plugins": {"key-auth": {}},
        "desc": "Order service",
    },
    # More specific than orders: answers its sub-paths, and is not offered.
    {"id": "orders-admin", "name": "Orders admin", "uri": "/api/ext/orders/admin/*", "plugins": {"key-auth": {}}},
    {"id": "status", "name": "Status", "uri": "/api/ext/status", "methods": ["GET"]},
    {"id": "put-only", "name": "Put only", "uri": "/api/ext/put", "methods": ["PUT"]},
    {"id": "hosted", "name": "Hosted", "uri": "/api/ext/h/*", "hosts": ["api.example.com"]},
    {"id": "query-api", "name": "query-api", "uri": "/api/query/*"},
    # Conditional routes under orders: an MCP call can meet both conditions
    # (it sets the query args; it comes from inside the network).
    {"id": "orders-v", "name": "Orders v", "uri": "/api/ext/orders/v/*", "vars": [["arg_x", "==", "1"]]},
    {"id": "orders-internal", "name": "Orders internal", "uri": "/api/ext/orders/internal/*", "remote_addrs": ["0.0.0.0/0"]},
    # Disabled: APISIX never picks it.
    {"id": "orders-off", "name": "Orders off", "uri": "/api/ext/orders/off/*", "status": 0},
    # Plugins bound through a service: whether key-auth runs is unknown.
    {"id": "svc", "name": "Service bound", "uri": "/api/ext/svc/*", "service_id": "s1"},
    # key-auth configured but switched off: an open route.
    {"id": "meta-off", "name": "Meta off", "uri": "/api/ext/meta/*", "plugins": {"key-auth": {"_meta": {"disable": True}}}},
]
EXT_KEY_ROUTES = [*ALL_ROUTES, "orders", "orders-admin", "status", "meta-off"]


@contextmanager
def ext_route_listing(routes: list[dict] | None = None):
    """The APISIX route listing the API tools read: EXT_ROUTES unless ``routes``."""
    with patch(
        "app.services.mcp_tools.apisix_client.list_resources",
        new_callable=AsyncMock,
        return_value={"items": EXT_ROUTES if routes is None else routes},
    ) as listing:
        yield listing


async def expose_routes(client, admin_token, *routes: str | tuple[str, str]) -> None:
    exposed = [
        {"route_id": r, "description": ""} if isinstance(r, str) else {"route_id": r[0], "description": r[1]}
        for r in routes
    ]
    await put_mcp_settings(client, admin_token, exposed_routes=exposed)
