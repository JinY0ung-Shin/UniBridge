"""MCP server (Streamable HTTP) for API keys, plus its admin settings.

Clients reach ``POST /api/mcp`` through the ``mcp-api`` APISIX route (key-auth,
consumer-restriction), which rewrites to ``/mcp`` here; tool calls go back out
through the gateway's REST routes with the same key. The server is
stateless: no ``Mcp-Session-Id``, JSON responses only, no server-initiated
stream (``GET`` answers 405). Blue/green colors can therefore serve any request
of a session. Tools come from :mod:`app.services.mcp_tools`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ApiKeyUser,
    CurrentUser,
    api_key_user_from_access,
    get_current_user_or_apikey,
    require_permission,
)
from app.config import settings
from app.database import get_db
from app.models import ApiKeyAccess, QueryTemplate
from app.schemas import (
    McpPreview,
    McpPreviewGroup,
    McpPreviewTool,
    McpRouteInfo,
    McpSettings,
    McpTemplateInfo,
)
from app.services import mcp_tools
from app.services.apisix_system_resources import MCP_API_ROUTE_ID
from app.services.audit import log_admin_action

logger = logging.getLogger(__name__)

router = APIRouter(tags=["MCP"])

# Newest first: offered when a client asks for a version we do not know.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "unibridge"
# JSON-RPC batches exist only in protocol 2025-03-26. Each message may read up
# to max_result_bytes and the whole response is built in memory, so a batch
# stays small; current clients send one message per request.
MAX_BATCH_MESSAGES = 16

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def _require_mcp_key(
    user: CurrentUser | ApiKeyUser = Depends(get_current_user_or_apikey),
    db: AsyncSession = Depends(get_db),
) -> tuple[ApiKeyUser, McpSettings]:
    if not isinstance(user, ApiKeyUser):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The MCP endpoint takes an API key (apikey header via /api/mcp)",
        )
    # APISIX enforces the grant at the edge; repeat it for direct internal calls.
    if not mcp_tools.has_route(user, MCP_API_ROUTE_ID):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Required API key route: {MCP_API_ROUTE_ID}",
        )
    mcp_settings = await mcp_tools.load_settings(db)
    if not mcp_settings.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The MCP endpoint is disabled by the administrator",
        )
    return user, mcp_settings


def _initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    version = (
        requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
    )
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "title": "UniBridge", "version": settings.APP_VERSION},
        "instructions": mcp_tools.SERVER_INSTRUCTIONS,
    }


async def _dispatch(
    ctx: mcp_tools.ToolContext, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    if method == "initialize":
        return _initialize_result(params)
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [tool.to_mcp() for tool in mcp_tools.build_tools(ctx)]}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "tools/call needs a name and an arguments object")
        tool = next((t for t in mcp_tools.build_tools(ctx) if t.name == name), None)
        if tool is None:
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        return await mcp_tools.call_tool(ctx, tool, arguments)
    raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


async def _handle_message(ctx: mcp_tools.ToolContext, message: Any) -> dict[str, Any] | None:
    """One JSON-RPC message → its response, or None for notifications/responses."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc_error(None, INVALID_REQUEST, "Invalid JSON-RPC 2.0 message")
    method = message.get("method")
    if method is None:
        return None  # a response to a server request; we never send any
    is_notification = "id" not in message
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return None if is_notification else _rpc_error(request_id, INVALID_REQUEST, "Invalid request")
    if is_notification:
        return None  # notifications/initialized, notifications/cancelled, ...
    try:
        result = await _dispatch(ctx, method, params)
    except _RpcError as exc:
        return _rpc_error(request_id, exc.code, exc.message)
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(
    request: Request,
    auth: tuple[ApiKeyUser, McpSettings] = Depends(_require_mcp_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    protocol_version = request.headers.get("mcp-protocol-version")
    if protocol_version and protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported MCP-Protocol-Version: {protocol_version}",
        )
    try:
        payload = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            _rpc_error(None, PARSE_ERROR, "Parse error"), status_code=status.HTTP_400_BAD_REQUEST
        )

    user, mcp_settings = auth
    ctx = mcp_tools.ToolContext(
        user=user,
        db=db,
        settings=mcp_settings,
        # key-auth hands the key on (hide_credentials is off); tool calls
        # resend it to the REST routes through the gateway.
        api_key=request.headers.get("apikey"),
    )
    if isinstance(payload, list):  # JSON-RPC batch (protocol 2025-03-26)
        if not payload or len(payload) > MAX_BATCH_MESSAGES:
            return JSONResponse(
                _rpc_error(
                    None, INVALID_REQUEST, f"A batch holds 1 to {MAX_BATCH_MESSAGES} messages"
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        responses = [r for r in [await _handle_message(ctx, m) for m in payload] if r is not None]
        if not responses:
            return Response(status_code=status.HTTP_202_ACCEPTED)
        return JSONResponse(responses)

    response = await _handle_message(ctx, payload)
    if response is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return JSONResponse(response)


@router.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
async def mcp_no_stream(
    _auth: tuple[ApiKeyUser, McpSettings] = Depends(_require_mcp_key),
) -> Response:
    """Stateless server: no standalone SSE stream and no session to delete."""
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})


# ── Admin ───────────────────────────────────────────────────────────────────


@router.get("/admin/mcp/settings", response_model=McpSettings)
async def get_mcp_settings(
    _admin: CurrentUser = Depends(require_permission("admin.config.read")),
    db: AsyncSession = Depends(get_db),
) -> McpSettings:
    return await mcp_tools.load_settings(db)


@router.put("/admin/mcp/settings", response_model=McpSettings)
async def update_mcp_settings(
    body: McpSettings,
    admin: CurrentUser = Depends(require_permission("admin.config.write")),
    db: AsyncSession = Depends(get_db),
) -> McpSettings:
    before = await mcp_tools.load_settings(db)
    await mcp_tools.save_settings(db, body)
    await log_admin_action(
        db,
        actor=admin.username,
        action="update",
        resource_type="mcp_settings",
        resource_id="global",
        before=before.model_dump(),
        after=body.model_dump(),
    )
    return body


@router.get("/admin/mcp/templates", response_model=list[McpTemplateInfo])
async def list_mcp_templates(
    _admin: CurrentUser = Depends(require_permission("admin.config.read")),
    db: AsyncSession = Depends(get_db),
) -> list[McpTemplateInfo]:
    """Every query template, for choosing which ones MCP hides."""
    result = await db.execute(select(QueryTemplate).order_by(QueryTemplate.path.asc()))
    return [
        McpTemplateInfo(
            path=t.path, name=t.name, database=t.db_alias, enabled=t.enabled
        )
        for t in result.scalars().all()
    ]


@router.get("/admin/mcp/routes", response_model=list[McpRouteInfo])
async def list_mcp_routes(
    _admin: CurrentUser = Depends(require_permission("admin.config.read")),
) -> list[McpRouteInfo]:
    """Gateway routes an administrator may offer over MCP (system routes excluded)."""
    try:
        routes = await mcp_tools.gateway_routes()
    except mcp_tools.ToolError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    infos = [mcp_tools.route_info(route) for route in routes if mcp_tools.offerable_route(route)]
    return sorted(infos, key=lambda info: (info.name.casefold(), info.route_id))


@router.get("/admin/mcp/preview", response_model=McpPreview)
async def preview_mcp_tools(
    consumer: str = Query(..., min_length=1),
    _admin: CurrentUser = Depends(require_permission("admin.config.read")),
    db: AsyncSession = Depends(get_db),
) -> McpPreview:
    """The tools ``consumer`` would get from tools/list right now."""
    access = (
        await db.execute(select(ApiKeyAccess).where(ApiKeyAccess.consumer_name == consumer))
    ).scalar_one_or_none()
    if access is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"API key '{consumer}' not found"
        )
    user = api_key_user_from_access(access)
    mcp_settings = await mcp_tools.load_settings(db)
    ctx = mcp_tools.ToolContext(user=user, db=db, settings=mcp_settings)
    tools = mcp_tools.build_tools(ctx)
    templates = (
        await mcp_tools.accessible_templates(ctx)
        if any(tool.group == "templates" for tool in tools)
        else []
    )
    apis: list[McpRouteInfo] = []
    apis_error: str | None = None
    if any(tool.group == "apis" for tool in tools):
        try:
            apis = await mcp_tools.exposed_apis(ctx)
        except mcp_tools.ToolError as exc:
            apis_error = str(exc)
    return McpPreview(
        consumer=consumer,
        mcp_enabled=mcp_settings.enabled,
        has_mcp_grant=mcp_tools.has_route(user, MCP_API_ROUTE_ID),
        groups=[
            McpPreviewGroup(
                group=group,
                enabled=getattr(mcp_settings.tool_groups, group),
                granted=mcp_tools.has_route(user, route_id),
                required_route=route_id,
            )
            for group, route_id in mcp_tools.GROUP_ROUTES.items()
        ]
        + [
            # Granted per route: the exposed routes this key holds.
            McpPreviewGroup(
                group="apis",
                enabled=mcp_settings.tool_groups.apis,
                granted=bool(mcp_tools.offered_route_ids(ctx)),
                required_route="",
            )
        ],
        tools=[
            McpPreviewTool(
                name=tool.name, title=tool.title, description=tool.description, group=tool.group
            )
            for tool in tools
        ],
        templates=[
            McpTemplateInfo(
                path=t.path, name=t.name, database=t.database, enabled=t.enabled
            )
            for t in templates
        ],
        apis=apis,
        apis_error=apis_error,
    )
