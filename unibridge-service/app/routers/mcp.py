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
from urllib.parse import urlparse

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
from app.db_types import utcnow
from app.models import ApiKeyAccess, McpRemoteServer, QueryTemplate
from app.routers.gateway import _is_masked_value, _mask_value
from app.schemas import (
    McpPreview,
    McpPreviewGroup,
    McpPreviewTool,
    McpRemoteHeader,
    McpRemoteServerCreate,
    McpRemoteServerInfo,
    McpRemoteServerUpdate,
    McpRemoteToolApproval,
    McpRemoteToolInfo,
    McpRemoteToolVersion,
    McpRouteInfo,
    McpSettings,
    McpTemplateInfo,
)
from app.services import mcp_remote, mcp_tools
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
        return {"tools": [tool.to_mcp() for tool in await mcp_tools.build_tools(ctx)]}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "tools/call needs a name and an arguments object")
        tool = next((t for t in await mcp_tools.build_tools(ctx) if t.name == name), None)
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


def _user_tokens(request: Request) -> dict[str, str]:
    """X-MCP-Auth-<server> headers → server name → token ("" if sent twice)."""
    prefix = mcp_remote.USER_TOKEN_HEADER_PREFIX.lower()
    tokens: dict[str, str] = {}
    for name, value in request.headers.items():  # names come lowercased
        if name.startswith(prefix):
            server = name.removeprefix(prefix)
            tokens[server] = "" if server in tokens else value.strip()
    return tokens


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
        user_tokens=_user_tokens(request),
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
    tools = await mcp_tools.build_tools(ctx)
    templates = (
        await mcp_tools.accessible_templates(ctx)
        if any(tool.group == "templates" for tool in tools)
        else []
    )
    servers = await mcp_remote.load_servers(db)
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
        ]
        + [
            # One row per external server: its relay route is the grant.
            McpPreviewGroup(
                group="external",
                enabled=mcp_settings.tool_groups.external and server.enabled,
                granted=mcp_tools.has_route(user, mcp_remote.route_id(server.name)),
                required_route=mcp_remote.route_id(server.name),
                server=server.name,
            )
            for server in servers
        ],
        tools=[
            McpPreviewTool(
                name=tool.name,
                title=tool.title,
                description=tool.description,
                group=tool.group,
                server=tool.server,
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


# ── External MCP servers ────────────────────────────────────────────────────


def _server_info(server: McpRemoteServer) -> McpRemoteServerInfo:
    approved = mcp_remote.approved_entries(server)
    try:
        headers = mcp_remote.decrypt_headers(server)
    except Exception:
        logger.warning("Could not decrypt the headers of MCP server %s", server.name)
        headers = {}
    try:
        refresh_token = mcp_remote.decrypt_refresh_token(server)
    except Exception:
        logger.warning("Could not decrypt the refresh token of MCP server %s", server.name)
        refresh_token = None
    tools = []
    for tool in mcp_remote.listed_tools(server):
        annotations = tool.get("annotations") or {}
        read_only = annotations.get("readOnlyHint")
        tool_status = mcp_remote.tool_status(server, tool, approved)
        before = approved.get(tool["name"]) if tool_status == "changed" else None
        tools.append(
            McpRemoteToolInfo(
                name=tool["name"],
                exposed_name=mcp_remote.exposed_name(server.name, tool["name"]),
                title=tool.get("title") or "",
                description=tool.get("description") or "",
                input_schema=tool.get("inputSchema") or {},
                read_only=read_only if isinstance(read_only, bool) else None,
                annotations=tool.get("annotations"),
                status=tool_status,  # type: ignore[arg-type]
                digest=mcp_remote.tool_digest(tool),
                approved_version=McpRemoteToolVersion(
                    title=before.get("title") or "",
                    description=before.get("description") or "",
                    input_schema=before.get("inputSchema") or {},
                    annotations=before.get("annotations"),
                )
                if before is not None
                else None,
            )
        )
    return McpRemoteServerInfo(
        name=server.name,
        url=server.url,
        description=server.description or "",
        enabled=server.enabled,
        timeout_seconds=server.timeout_seconds,
        headers=[
            McpRemoteHeader.model_construct(name=name, value=_mask_value(value))
            for name, value in headers.items()
        ],
        user_token_header=server.user_token_header,
        user_token_format=server.user_token_format or "Bearer {token}",
        user_token_client_header=mcp_remote.client_token_header(server.name)
        if server.user_token_header
        else None,
        user_token_refresh=_mask_value(refresh_token) if refresh_token else None,
        route_id=mcp_remote.route_id(server.name),
        tools=tools,
        refreshed_at=server.refreshed_at,
        last_error=server.last_error,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _audit_view(server: McpRemoteServer | None) -> dict[str, Any] | None:
    """The audit log's copy of a server: headers masked, tools as names only."""
    if server is None:
        return None
    info = _server_info(server).model_dump(mode="json")
    info["tools"] = {tool["name"]: tool["status"] for tool in info["tools"]}
    return info


async def _get_server(db: AsyncSession, name: str) -> McpRemoteServer:
    server = await db.get(McpRemoteServer, name)
    if server is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"MCP server '{name}' not found"
        )
    return server


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port


def _merged_headers(
    submitted: list[McpRemoteHeader], stored: dict[str, str], *, moved: bool = False
) -> dict[str, str]:
    """Submitted headers; a masked value keeps what is stored under that name.

    Not when the server moved to another origin: stored credentials go only
    where they were entered for, so they must be typed again.
    """
    stored_by_lower = {name.lower(): value for name, value in stored.items()}
    merged: dict[str, str] = {}
    for header in submitted:
        value = header.value
        if _is_masked_value(value) or value == "":
            kept = None if moved else stored_by_lower.get(header.name.lower())
            if kept is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Header '{header.name}': enter the value again for the new address"
                        if moved
                        else f"Header '{header.name}' needs a value"
                    ),
                )
            value = kept
        merged[header.name] = value
    return merged


def _user_token_settings(
    body: McpRemoteServerUpdate,
    headers: dict[str, str],
    server: McpRemoteServer | None,
    *,
    moved: bool = False,
) -> tuple[str | None, str | None, str | None]:
    """(header, format, refresh token) to store for ``body``.

    An update that leaves the header out keeps the stored setting: a client
    that predates it must not switch it off. The refresh token follows the
    stored headers' rules (masked or "" keeps it, not across origins).
    """
    keep = server is not None and "user_token_header" not in body.model_fields_set
    header = server.user_token_header if keep and server is not None else body.user_token_header
    if header is None:
        return None, None, None
    if server is not None and "user_token_format" not in body.model_fields_set:
        token_format = server.user_token_format or body.user_token_format
    else:
        token_format = body.user_token_format
    if any(name.lower() == header.lower() for name in headers):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Header '{header}' carries the end user's token: remove it from the headers "
                "(a token for refresh goes in the end-user token settings)"
            ),
        )
    submitted = body.user_token_refresh
    if submitted is None:
        return header, token_format, None
    if submitted != "" and not _is_masked_value(submitted):
        return header, token_format, submitted
    stored = mcp_remote.decrypt_refresh_token(server) if server is not None else None
    if stored is None:
        if submitted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No refresh token is stored: enter one or leave it blank",
            )
        return header, token_format, None
    if moved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Refresh token: enter the value again for the new address",
        )
    return header, token_format, stored


async def _provision_or_502(db: AsyncSession, server: McpRemoteServer) -> None:
    try:
        await mcp_remote.provision(db, server)
    except Exception as exc:
        await db.rollback()
        logger.warning("Could not provision MCP server %s: %s", server.name, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to write the gateway route: {exc}",
        ) from exc


@router.get("/admin/mcp/servers", response_model=list[McpRemoteServerInfo])
async def list_mcp_servers(
    _admin: CurrentUser = Depends(require_permission("admin.config.read")),
    db: AsyncSession = Depends(get_db),
) -> list[McpRemoteServerInfo]:
    return [_server_info(server) for server in await mcp_remote.load_servers(db)]


@router.post(
    "/admin/mcp/servers",
    response_model=McpRemoteServerInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_mcp_server(
    body: McpRemoteServerCreate,
    admin: CurrentUser = Depends(require_permission("admin.config.write")),
    db: AsyncSession = Depends(get_db),
) -> McpRemoteServerInfo:
    if await db.get(McpRemoteServer, body.name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"MCP server '{body.name}' already exists"
        )
    headers = _merged_headers(body.headers, {})
    token_header, token_format, refresh_token = _user_token_settings(body, headers, None)
    # Made (and committed) before anything of this request is pending.
    await mcp_remote.relay_secret(db)
    server = McpRemoteServer(
        name=body.name,
        url=body.url,
        description=body.description,
        enabled=body.enabled,
        timeout_seconds=body.timeout_seconds,
        headers_encrypted=mcp_remote.encrypt_headers(headers),
        user_token_header=token_header,
        user_token_format=token_format,
        user_token_refresh_encrypted=mcp_remote.encrypt_refresh_token(refresh_token),
        tools="[]",
        approved_tools="{}",
    )
    db.add(server)
    await db.flush()
    # A name used before may have left grants behind (an older image deleted it).
    await mcp_remote.revoke_grants(db, server.name)
    await _provision_or_502(db, server)
    await db.commit()
    await db.refresh(server)
    await log_admin_action(
        db,
        actor=admin.username,
        action="create",
        resource_type="mcp_server",
        resource_id=server.name,
        summary=server.url,
        before=None,
        after=_audit_view(server),
    )
    return _server_info(server)


@router.put("/admin/mcp/servers/{name}", response_model=McpRemoteServerInfo)
async def update_mcp_server(
    name: str,
    body: McpRemoteServerUpdate,
    admin: CurrentUser = Depends(require_permission("admin.config.write")),
    db: AsyncSession = Depends(get_db),
) -> McpRemoteServerInfo:
    server = await _get_server(db, name)
    before = _audit_view(server)
    moved = _origin(body.url) != _origin(server.url)
    headers = _merged_headers(body.headers, mcp_remote.decrypt_headers(server), moved=moved)
    token_header, token_format, refresh_token = _user_token_settings(
        body, headers, server, moved=moved
    )
    if body.url != server.url:
        # Another address may be another server: its tools are approved anew.
        server.tools = "[]"
        server.approved_tools = "{}"
        server.refreshed_at = None
        server.last_error = None
    server.url = body.url
    server.description = body.description
    server.enabled = body.enabled
    server.timeout_seconds = body.timeout_seconds
    server.headers_encrypted = mcp_remote.encrypt_headers(headers)
    server.user_token_header = token_header
    server.user_token_format = token_format
    server.user_token_refresh_encrypted = mcp_remote.encrypt_refresh_token(refresh_token)
    await db.flush()
    await _provision_or_502(db, server)
    await db.commit()
    await db.refresh(server)
    mcp_remote.forget_sessions(name)
    await log_admin_action(
        db,
        actor=admin.username,
        action="update",
        resource_type="mcp_server",
        resource_id=name,
        summary=server.url,
        before=before,
        after=_audit_view(server),
    )
    return _server_info(server)


@router.delete(
    "/admin/mcp/servers/{name}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_mcp_server(
    name: str,
    admin: CurrentUser = Depends(require_permission("admin.config.write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    server = await _get_server(db, name)
    before = _audit_view(server)
    try:
        await mcp_remote.deprovision(name)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to delete the gateway route: {exc}",
        ) from exc
    revoked = await mcp_remote.revoke_grants(db, name)
    await db.delete(server)
    await db.commit()
    logger.info("MCP server %s deleted; grant removed from %d key(s)", name, revoked)
    await log_admin_action(
        db,
        actor=admin.username,
        action="delete",
        resource_type="mcp_server",
        resource_id=name,
        summary=(before or {}).get("url"),
        before=before,
        after=None,
    )


@router.post("/admin/mcp/servers/{name}/refresh", response_model=McpRemoteServerInfo)
async def refresh_mcp_server(
    name: str,
    _admin: CurrentUser = Depends(require_permission("admin.config.write")),
    db: AsyncSession = Depends(get_db),
) -> McpRemoteServerInfo:
    """Fetch the server's tool listing; new or changed tools wait for approval."""
    target = mcp_remote.DirectTarget.of(await _get_server(db, name))
    # No transaction stays open while the server takes its time.
    await db.rollback()
    try:
        tools = await mcp_remote.list_tools(target)
    except mcp_remote.RemoteError as exc:
        server = await _get_server(db, name)
        server.last_error = str(exc)[:2_000]
        await db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    server = await _get_server(db, name)
    if server.url != target.url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The server's address changed during the refresh; refresh again",
        )
    listed = {tool["name"] for tool in tools}
    # Approvals of tools the server dropped go too: one coming back is new again.
    approved = {k: v for k, v in mcp_remote.approved_entries(server).items() if k in listed}
    server.tools = json.dumps(tools, ensure_ascii=False)
    server.approved_tools = json.dumps(approved, ensure_ascii=False)
    server.refreshed_at = utcnow()
    server.last_error = None
    await db.commit()
    await db.refresh(server)
    return _server_info(server)


@router.put("/admin/mcp/servers/{name}/tools", response_model=McpRemoteServerInfo)
async def approve_mcp_server_tools(
    name: str,
    body: McpRemoteToolApproval,
    admin: CurrentUser = Depends(require_permission("admin.config.write")),
    db: AsyncSession = Depends(get_db),
) -> McpRemoteServerInfo:
    """Offer exactly ``approved`` of the last fetched listing.

    Each tool comes with the digest the administrator saw: if a refresh has
    changed the entry since, nothing is approved (409), so an approval never
    covers a description or schema nobody looked at.
    """
    server = await _get_server(db, name)
    before = _audit_view(server)
    listed = {tool["name"]: tool for tool in mcp_remote.listed_tools(server)}
    wanted = {item.name: item.digest for item in body.approved}
    unknown = sorted(set(wanted) - set(listed))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not in the fetched listing: {', '.join(unknown[:20])}",
        )
    invalid = sorted(n for n in wanted if not mcp_remote.valid_exposed_name(name, n))
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not valid as MCP tool names: {', '.join(invalid[:20])}",
        )
    stale = sorted(n for n, digest in wanted.items() if mcp_remote.tool_digest(listed[n]) != digest)
    if stale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Changed since you loaded them; review again: {', '.join(stale[:20])}",
        )
    server.approved_tools = json.dumps(
        {tool_name: listed[tool_name] for tool_name in sorted(wanted)}, ensure_ascii=False
    )
    await db.commit()
    await db.refresh(server)
    await log_admin_action(
        db,
        actor=admin.username,
        action="update",
        resource_type="mcp_server_tools",
        resource_id=name,
        before=before,
        after=_audit_view(server),
    )
    return _server_info(server)
