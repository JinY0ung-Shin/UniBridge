"""External MCP servers whose tools /api/mcp offers next to its own.

An administrator registers a Streamable HTTP server (``McpRemoteServer``). It
gets an APISIX route and upstream, ``mcp-ext-<name>``, that only this app uses:

- The route sits at ``/_mcp-ext/<name>``, outside the ``/api/`` prefix nginx
  forwards, and also needs the ``X-UniBridge-MCP-Relay`` header carrying a
  secret kept in ``system_config``. Clients cannot reach it; they use /api/mcp.
- ``tools/call`` on ``<name>__<tool>`` goes out on that route with the caller's
  own key, so key-auth, the route grant, rate limits and request counts apply
  exactly as on every other route. key-auth hides the key from the upstream and
  proxy-rewrite swaps in the server's own headers (stored encrypted here).
- ``tools/list`` never reaches the server: it answers from the listing an
  administrator last fetched (``refresh``, sent straight from this app), and
  only with the tools approved against that listing's digests. A tool the
  server adds or changes stays hidden until it is approved again.

The upstream speaks MCP over Streamable HTTP. This side is a client of the
minimum the spec needs: ``initialize`` → ``notifications/initialized`` →
``tools/list``/``tools/call``, JSON or SSE answers, ``Mcp-Session-Id``. Server
requests (sampling, elicitation, roots) are refused; notifications are dropped.
Sessions for tool calls are kept per (server, key) in this process.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import itertools
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models import ApiKeyAccess, McpRemoteServer, SystemConfig
from app.services import apisix_client
from app.services.apisix_system_resources import MCP_REMOTE_ID_PREFIX
from app.services.connection_manager import decrypt_password, encrypt_password

logger = logging.getLogger(__name__)

RELAY_PATH_PREFIX = "/_mcp-ext/"
RELAY_HEADER = "X-UniBridge-MCP-Relay"
# What the relay route removes before the request leaves: the relay secret and
# the identity headers key-auth attaches for upstreams (the caller's consumer
# name is no business of an external server).
_RELAY_REMOVED_HEADERS = [
    RELAY_HEADER,
    "X-Consumer-Username",
    "X-Credential-Identifier",
    "X-Consumer-Custom-Id",
]
RELAY_SECRET_KEY = "mcp_relay_secret"
PROTOCOL_VERSION = "2025-06-18"
TOOL_SEPARATOR = "__"
# What MCP clients accept as a tool name (Claude caps it at 64).
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TOOLS = 500
_LISTING_MAX_PAGES = 20
_LISTING_MAX_BYTES = 4 * 1024 * 1024
_CONNECT_TIMEOUT_SECONDS = 10
# Room past the server's own timeout for initialize and the gateway's answer.
_DEADLINE_MARGIN_SECONDS = 10
_SESSION_IDLE_SECONDS = 600
_SESSION_CACHE_MAX = 1_000
# Fields of a listed tool kept and approved. outputSchema is left out: results
# may be cut to max_result_bytes, which could drop the structured content a
# declared schema makes mandatory.
_TOOL_FIELDS = ("name", "title", "description", "inputSchema", "annotations")


class RemoteError(Exception):
    """The external server, or the hop to it, failed; the text is for the model."""


class _SessionExpired(RemoteError):
    pass


# ── Names ───────────────────────────────────────────────────────────────────


def route_id(name: str) -> str:
    return f"{MCP_REMOTE_ID_PREFIX}{name}"


def relay_path(name: str) -> str:
    return f"{RELAY_PATH_PREFIX}{name}"


def exposed_name(server: str, tool: str) -> str:
    return f"{server}{TOOL_SEPARATOR}{tool}"


def valid_exposed_name(server: str, tool: str) -> bool:
    return bool(_TOOL_NAME_RE.match(exposed_name(server, tool)))


# ── Stored state ────────────────────────────────────────────────────────────


def tool_digest(tool: dict[str, Any]) -> str:
    canonical = json.dumps(
        {field: tool.get(field) for field in _TOOL_FIELDS},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def listed_tools(server: McpRemoteServer) -> list[dict[str, Any]]:
    try:
        tools = json.loads(server.tools or "[]")
    except ValueError:
        return []
    return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []


def approved_entries(server: McpRemoteServer) -> dict[str, dict[str, Any]]:
    """Tool name → the listing entry as it was approved."""
    try:
        approved = json.loads(server.approved_tools or "{}")
    except ValueError:
        return {}
    if not isinstance(approved, dict):
        return {}
    return {k: v for k, v in approved.items() if isinstance(k, str) and isinstance(v, dict)}


def tool_status(
    server: McpRemoteServer, tool: dict[str, Any], approved: dict[str, dict[str, Any]]
) -> str:
    if not valid_exposed_name(server.name, tool["name"]):
        return "invalid_name"
    entry = approved.get(tool["name"])
    if entry is None:
        return "new"
    return "approved" if tool_digest(entry) == tool_digest(tool) else "changed"


def offered_tools(server: McpRemoteServer) -> list[dict[str, Any]]:
    """The listed tools /api/mcp offers: approved, unchanged since, and well named."""
    approved = approved_entries(server)
    return [
        tool
        for tool in listed_tools(server)
        if tool_status(server, tool, approved) == "approved"
    ]


def encrypt_headers(headers: dict[str, str]) -> str | None:
    return encrypt_password(json.dumps(headers)) if headers else None


def decrypt_headers(server: McpRemoteServer) -> dict[str, str]:
    if not server.headers_encrypted:
        return {}
    decoded = json.loads(decrypt_password(server.headers_encrypted))
    return {str(k): str(v) for k, v in decoded.items()} if isinstance(decoded, dict) else {}


async def load_servers(db: AsyncSession, *, enabled_only: bool = False) -> list[McpRemoteServer]:
    stmt = select(McpRemoteServer).order_by(McpRemoteServer.name.asc())
    if enabled_only:
        stmt = stmt.where(McpRemoteServer.enabled.is_(True))
    return list((await db.execute(stmt)).scalars().all())


@dataclass(frozen=True)
class ServerRef:
    """What a tool call needs of a server row, read before the session is released."""

    name: str
    timeout_seconds: int
    # Changes whenever the row does; a cached upstream session dies with it.
    revision: str

    @classmethod
    def of(cls, server: McpRemoteServer) -> ServerRef:
        return cls(
            name=server.name,
            timeout_seconds=server.timeout_seconds,
            revision=server.updated_at.isoformat() if server.updated_at else "",
        )


# ── Relay secret ────────────────────────────────────────────────────────────

async def relay_secret(db: AsyncSession) -> str:
    """The value the relay routes require in ``X-UniBridge-MCP-Relay``.

    Made once and shared by both blue/green colors through ``system_config``;
    it only has to be unknown outside this app and APISIX.
    """
    # Read on every use (one primary-key lookup), so both colors always agree
    # with what the routes were written with.
    row = await db.get(SystemConfig, RELAY_SECRET_KEY)
    if row is not None:
        return row.value
    # Its own session: the caller's transaction (a server being created) must
    # neither be committed by this nor rolled back with it.
    session_factory = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as own:
        row = await own.get(SystemConfig, RELAY_SECRET_KEY)
        if row is None:
            own.add(SystemConfig(key=RELAY_SECRET_KEY, value=secrets.token_urlsafe(32)))
            try:
                await own.commit()
            except IntegrityError:
                await own.rollback()  # the other color made it first
            row = await own.get(SystemConfig, RELAY_SECRET_KEY)
            if row is None:
                raise RemoteError("The MCP relay secret could not be stored")
        return row.value


# ── Gateway resources ───────────────────────────────────────────────────────


def _node(url: str) -> tuple[str, str, str]:
    """``url`` → (scheme, host:port node, path)."""
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return parsed.scheme, f"{host}:{port}", parsed.path or "/"


def upstream_body(server: McpRemoteServer) -> dict[str, Any]:
    scheme, node, _path = _node(server.url)
    return {
        "name": route_id(server.name),
        "desc": f"External MCP server '{server.name}' (managed on the MCP page)",
        "type": "roundrobin",
        "scheme": scheme,
        "pass_host": "node",
        "nodes": {node: 1},
    }


def route_body(
    server: McpRemoteServer, secret: str, consumer_restriction: dict[str, Any]
) -> dict[str, Any]:
    _scheme, _node_addr, path = _node(server.url)
    headers: dict[str, Any] = {"remove": list(_RELAY_REMOVED_HEADERS)}
    upstream_headers = decrypt_headers(server)
    if upstream_headers:
        headers["set"] = upstream_headers
    timeout = server.timeout_seconds
    return {
        "name": route_id(server.name),
        "desc": (
            f"External MCP server '{server.name}': relay for /api/mcp tool calls, "
            "not a client endpoint (managed on the MCP page)"
        ),
        "uri": relay_path(server.name),
        "methods": ["POST", "DELETE"],
        "vars": [[f"http_{RELAY_HEADER.lower().replace('-', '_')}", "==", secret]],
        "upstream_id": route_id(server.name),
        "timeout": {"connect": _CONNECT_TIMEOUT_SECONDS, "send": timeout, "read": timeout},
        "plugins": {
            # The caller's key unlocks the route but is no business of the server.
            "key-auth": {"hide_credentials": True},
            "consumer-restriction": consumer_restriction,
            "proxy-rewrite": {"uri": path, "headers": headers},
        },
        "status": 1 if server.enabled else 0,
    }


def _is_missing(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


async def provision(db: AsyncSession, server: McpRemoteServer) -> None:
    """Write the server's upstream and route, keeping the route's key whitelist."""
    # Imported here: the API-key router imports the services this module sits beside.
    from app.routers.api_keys import (
        DENY_ALL_CONSUMER,
        apply_master_consumer_restriction,
        list_master_consumer_names,
    )

    rid = route_id(server.name)
    secret = await relay_secret(db)
    try:
        existing = await apisix_client.get_resource("routes", rid)
    except Exception as exc:
        if not _is_missing(exc):
            raise
        existing = None
    restriction = (existing or {}).get("plugins", {}).get("consumer-restriction")
    body = route_body(
        server,
        secret,
        restriction if isinstance(restriction, dict) else {"whitelist": [DENY_ALL_CONSUMER]},
    )
    if existing is None:
        # A new route starts with the master keys only; grants follow per key.
        body = apply_master_consumer_restriction(body, await list_master_consumer_names(db))
    await apisix_client.put_resource("upstreams", rid, upstream_body(server))
    await apisix_client.put_resource("routes", rid, body)


async def deprovision(name: str) -> None:
    rid = route_id(name)
    for resource in ("routes", "upstreams"):
        try:
            await apisix_client.delete_resource(resource, rid)
        except Exception as exc:
            if not _is_missing(exc):
                raise
    forget_sessions(name)


async def revoke_grants(db: AsyncSession, name: str) -> int:
    """Drop the server's route from every key's grants; returns how many keys had it.

    A server registered again under the same name must not inherit them.
    """
    rid = route_id(name)
    changed = 0
    for access in (await db.execute(select(ApiKeyAccess))).scalars().all():
        try:
            routes = json.loads(access.allowed_routes or "[]")
        except ValueError:
            continue
        if isinstance(routes, list) and rid in routes:
            access.allowed_routes = json.dumps([r for r in routes if r != rid])
            changed += 1
    return changed


async def provision_all(db: AsyncSession) -> None:
    """Boot: write every registered server's resources (an etcd reset loses them).

    Nothing is deleted here: removing a server deletes its route and upstream
    before its row, and keeps the row if APISIX refuses.
    """
    for server in await load_servers(db):
        try:
            await provision(db, server)
        except Exception as exc:
            # One bad entry must not keep the service from starting.
            logger.error("Could not provision external MCP server %s: %s", server.name, exc)


# ── Transport ───────────────────────────────────────────────────────────────

_ids = itertools.count(1)


@dataclass
class _Session:
    session_id: str | None
    protocol_version: str
    last_used: float = 0.0


@dataclass
class _Transport:
    client: httpx.AsyncClient
    url: str
    headers: dict[str, str]
    max_bytes: int
    timeout: float


def _request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        message["params"] = params
    return message


def _headers(t: _Transport, session: _Session | None) -> dict[str, str]:
    headers = {
        **t.headers,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session is not None:
        headers["MCP-Protocol-Version"] = session.protocol_version
        if session.session_id:
            headers["Mcp-Session-Id"] = session.session_id
    return headers


def _error_text(status_code: int, body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict):
        error = decoded.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            text = error["message"]
        else:
            # APISIX plugins say "message"/"error_msg"; FastAPI says "detail".
            for field in ("message", "error_msg", "detail"):
                if isinstance(decoded.get(field), str):
                    text = decoded[field]
                    break
    return f"HTTP {status_code}: {text[:300] or 'no detail'}"


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > max_bytes:
            raise RemoteError(f"The answer is over {max_bytes} bytes")
    return bytes(data)


def _answer_to(payload: Any, request_id: int) -> dict[str, Any] | None:
    for message in payload if isinstance(payload, list) else [payload]:
        if (
            isinstance(message, dict)
            and message.get("id") == request_id
            and ("result" in message or "error" in message)
        ):
            return message
    return None


async def _refuse_server_request(t: _Transport, session: _Session | None, message: dict[str, Any]) -> None:
    """Answer a request the server sent us: ping succeeds, the rest is unsupported."""
    if message.get("method") == "ping":
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}
    else:
        reply = {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32601, "message": "UniBridge relays tool calls only"},
        }
    try:
        await t.client.post(
            t.url, content=json.dumps(reply), headers=_headers(t, session), timeout=t.timeout
        )
    except httpx.HTTPError as exc:
        logger.debug("Could not answer a server request on %s: %s", t.url, exc)


async def _read_sse(
    t: _Transport, response: httpx.Response, request_id: int, session: _Session | None
) -> dict[str, Any]:
    buffer = bytearray()
    data_lines: list[str] = []
    total = 0

    async def dispatch() -> dict[str, Any] | None:
        if not data_lines:
            return None
        text = "\n".join(data_lines)
        data_lines.clear()
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        answer = _answer_to(payload, request_id)
        if answer is not None:
            return answer
        for message in payload if isinstance(payload, list) else [payload]:
            if isinstance(message, dict) and "method" in message and "id" in message:
                await _refuse_server_request(t, session, message)
        return None

    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > t.max_bytes:
            raise RemoteError(f"The answer is over {t.max_bytes} bytes")
        buffer.extend(chunk)
        while (end := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:end]).rstrip(b"\r").decode("utf-8", errors="replace")
            del buffer[: end + 1]
            if line == "":
                if (answer := await dispatch()) is not None:
                    return answer
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                if field == "data":
                    data_lines.append(value[1:] if value.startswith(" ") else value)
    if buffer:
        line = bytes(buffer).rstrip(b"\r").decode("utf-8", errors="replace")
        if line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if (answer := await dispatch()) is not None:
        return answer
    raise RemoteError("The server closed the stream without answering")


async def _send(
    t: _Transport, message: dict[str, Any], session: _Session | None
) -> tuple[dict[str, Any] | None, httpx.Headers]:
    """POST one message; a request's answer comes back, a notification's is None."""
    async with t.client.stream(
        "POST",
        t.url,
        content=json.dumps(message, ensure_ascii=False).encode("utf-8"),
        headers=_headers(t, session),
        timeout=httpx.Timeout(t.timeout, connect=_CONNECT_TIMEOUT_SECONDS),
    ) as response:
        # The spec says 404; some servers answer an unknown session with 400.
        if response.status_code in (400, 404) and session is not None and session.session_id:
            detail = _error_text(response.status_code, await _read_capped(response, 4096))
            raise _SessionExpired(detail)
        if response.status_code >= 400:
            raise RemoteError(_error_text(response.status_code, await _read_capped(response, 4096)))
        if "id" not in message:
            return None, response.headers
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type == "text/event-stream":
            answer = await _read_sse(t, response, message["id"], session)
        else:
            data = await _read_capped(response, t.max_bytes)
            try:
                answer = _answer_to(json.loads(data), message["id"])
            except ValueError as exc:
                raise RemoteError("The server's answer is not JSON") from exc
            if answer is None:
                raise RemoteError("The server's answer is not a JSON-RPC response")
        return answer, response.headers


def _result(answer: dict[str, Any] | None) -> dict[str, Any]:
    if answer is None:
        raise RemoteError("The server sent no answer")
    error = answer.get("error")
    if error is not None:
        message = error.get("message") if isinstance(error, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        raise RemoteError(f"MCP error {code}: {message or 'no message'}")
    result = answer.get("result")
    if not isinstance(result, dict):
        raise RemoteError("The server's result is not an object")
    return result


async def _initialize(t: _Transport) -> _Session:
    answer, headers = await _send(
        t,
        _request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "unibridge", "version": settings.APP_VERSION},
            },
        ),
        None,
    )
    result = _result(answer)
    version = result.get("protocolVersion")
    session_id = headers.get("mcp-session-id") or None
    # The spec allows visible ASCII only; anything else could not be sent back.
    if session_id is not None and not all(0x21 <= ord(ch) <= 0x7E for ch in session_id):
        raise RemoteError("The server's Mcp-Session-Id is not visible ASCII")
    if not isinstance(version, str) or not all(0x21 <= ord(ch) <= 0x7E for ch in version):
        version = None
    session = _Session(
        session_id=session_id,
        protocol_version=version if isinstance(version, str) and version else PROTOCOL_VERSION,
    )
    await _send(t, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    return session


async def _close(t: _Transport, session: _Session) -> None:
    if not session.session_id:
        return
    try:
        await t.client.delete(t.url, headers=_headers(t, session), timeout=t.timeout)
    except httpx.HTTPError:
        pass  # the server expires it on its own


# ── Listing (admin refresh, straight to the server) ─────────────────────────


def _clean_tool(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        return None
    schema = raw.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {"type": "object"}
    annotations = raw.get("annotations")
    tool: dict[str, Any] = {
        "name": raw["name"],
        "title": raw.get("title") if isinstance(raw.get("title"), str) else None,
        "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
        "inputSchema": schema,
        "annotations": annotations if isinstance(annotations, dict) else None,
    }
    return tool


def _direct_client() -> httpx.AsyncClient:
    # No env proxy, like APISIX on the relay route. Unlike APISIX, which does
    # not verify upstream certificates (as on every gateway route), this checks
    # them with the service's CA settings.
    verify: str | bool = settings.SSL_CA_CERT_PATH or settings.SSL_VERIFY
    return httpx.AsyncClient(verify=verify, trust_env=False)


@dataclass(frozen=True)
class DirectTarget:
    """What a refresh needs of a server row, read before the session is released."""

    url: str
    headers: dict[str, str]
    timeout_seconds: int

    @classmethod
    def of(cls, server: McpRemoteServer) -> DirectTarget:
        return cls(server.url, decrypt_headers(server), server.timeout_seconds)


async def list_tools(server: DirectTarget) -> list[dict[str, Any]]:
    """The server's tools, fetched directly with its stored headers."""
    async with _direct_client() as client:
        t = _Transport(
            client=client,
            url=server.url,
            headers=server.headers,
            max_bytes=_LISTING_MAX_BYTES,
            timeout=float(server.timeout_seconds),
        )
        try:
            async with asyncio.timeout(server.timeout_seconds + _DEADLINE_MARGIN_SECONDS):
                session = await _initialize(t)
                try:
                    tools: list[dict[str, Any]] = []
                    seen: set[str] = set()
                    cursor: str | None = None
                    for _page in range(_LISTING_MAX_PAGES):
                        params = {"cursor": cursor} if cursor else {}
                        result = _result((await _send(t, _request("tools/list", params), session))[0])
                        for raw in result.get("tools") or []:
                            tool = _clean_tool(raw)
                            if tool is None or tool["name"] in seen:
                                continue
                            seen.add(tool["name"])
                            tools.append(tool)
                            if len(tools) > MAX_TOOLS:
                                raise RemoteError(f"The server lists more than {MAX_TOOLS} tools")
                        next_cursor = result.get("nextCursor")
                        if not isinstance(next_cursor, str) or not next_cursor:
                            break
                        cursor = next_cursor
                    return tools
                finally:
                    await _close(t, session)
        except TimeoutError as exc:
            raise RemoteError(f"No answer within {server.timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise RemoteError(f"Could not reach the server: {exc}") from exc


# ── Tool calls (through the gateway, as the caller) ─────────────────────────

_sessions: OrderedDict[tuple[str, str, str], _Session] = OrderedDict()


def forget_sessions(name: str) -> None:
    for key in [key for key in _sessions if key[0] == name]:
        _sessions.pop(key, None)


def _cached_session(key: tuple[str, str, str]) -> _Session | None:
    now = time.monotonic()
    for stale in [k for k, s in _sessions.items() if now - s.last_used > _SESSION_IDLE_SECONDS]:
        _sessions.pop(stale, None)
    session = _sessions.get(key)
    if session is not None:
        _sessions.move_to_end(key)
    return session


def _store_session(key: tuple[str, str, str], session: _Session) -> None:
    session.last_used = time.monotonic()
    _sessions[key] = session
    _sessions.move_to_end(key)
    while len(_sessions) > _SESSION_CACHE_MAX:
        _sessions.popitem(last=False)


async def call_tool(
    client: httpx.AsyncClient,
    server: ServerRef,
    *,
    api_key: str,
    consumer: str,
    secret: str,
    tool: str,
    arguments: dict[str, Any],
    max_bytes: int,
) -> dict[str, Any]:
    """``tools/call`` on the server through its relay route; the CallToolResult."""
    t = _Transport(
        client=client,
        url=relay_path(server.name),
        headers={"apikey": api_key, RELAY_HEADER: secret},
        max_bytes=max_bytes,
        timeout=float(server.timeout_seconds),
    )
    key = (server.name, server.revision, consumer)
    try:
        async with asyncio.timeout(server.timeout_seconds + _DEADLINE_MARGIN_SECONDS):
            for attempt in range(2):
                session = _cached_session(key)
                if session is None:
                    session = await _initialize(t)
                    _store_session(key, session)
                try:
                    answer, _headers_ = await _send(
                        t, _request("tools/call", {"name": tool, "arguments": arguments}), session
                    )
                except _SessionExpired:
                    _sessions.pop(key, None)
                    if attempt:
                        raise
                    continue
                session.last_used = time.monotonic()
                return _result(answer)
    except TimeoutError as exc:
        raise RemoteError(f"No answer within {server.timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        logger.warning("MCP relay call %s on %s failed: %s", tool, server.name, exc)
        raise RemoteError("The UniBridge gateway did not answer") from exc
    raise RemoteError("The server keeps ending the session")
