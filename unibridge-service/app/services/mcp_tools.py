"""MCP tools over the query, S3, NAS and network-map surfaces, scoped to one API key.

Every tool that reaches a database, S3 or NAS goes out as the matching REST
request through APISIX (``APISIX_GATEWAY_URL``) with the caller's own key. So
each call is authorized, rate-limited, audited and counted on that REST route
(``query-api``, ``s3-api``, ``nas-api``, ``network-api``/``network-write-api``)
exactly like a direct call, and the ``mcp-api`` route itself is left out of
usage numbers. A tool group is offered only when the key holds the route grant
its REST twin needs (``GROUP_ROUTES``); the network map's write tools also need
``network-write-api``, and what they record stays unverified until a person
confirms it.
Gateway APIs (``search_apis``/``call_api``) are the routes an administrator
lists in ``exposed_routes``, each offered only to keys holding its grant;
``call_api`` sends GET or POST only, on the route itself. External MCP
servers' approved tools come in as ``<server>__<tool>``, offered to keys holding
the server's relay route and relayed by :mod:`app.services.mcp_remote`.
Binary files go through staging (:mod:`app.services.mcp_artifacts`):
``download_artifact`` fetches one from S3, NAS or an exposed API as the caller
and returns a reference instead of bytes, or, for a small file, the file
itself as a standard MCP embedded resource; ``upload_staged_artifacts`` posts
staged ones to an exposed API as multipart/form-data. Only that embedded
resource is MCP's own file representation: staging by id, the
/api/mcp/artifacts transfer and the multipart upload are UniBridge's
extensions (MCP defines no upload).
Discovery — tools/list, ``list_databases``, ``search_templates``,
``*_list_connections`` — touches no backend and is answered here.

Query templates are two tools — search_templates and run_template — rather
than one per template, so tools/list stays small however many there are.

Settings (on/off, tool groups, hidden templates, result caps) live in ``system_config`` under
``MCP_SETTINGS_KEY`` and are read per request, so both blue/green colors see an
admin's change at once.
"""
from __future__ import annotations

import asyncio
import codecs
import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any
from urllib.parse import quote, unquote

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ApiKeyUser
from app.config import settings as app_settings
from app.models import McpRemoteServer, SystemConfig
from app.routers import gateway as gateway_router
from app.routers import query as query_router
from app.schemas import McpRouteInfo, McpSettings, McpToolGroup, QueryTemplateResponse
from app.services import apisix_client, mcp_artifacts, mcp_remote
from app.services.apisix_system_resources import (
    NETWORK_API_ROUTE_ID,
    NETWORK_WRITE_ROUTE_ID,
    QUERY_API_ROUTE_ID,
    is_system_route,
)
from app.services.connection_manager import connection_manager
from app.services.nas_manager import nas_manager
from app.services.openapi_export import extract_template_params
from app.services.s3_manager import S3_MAX_PAGE_KEYS, s3_manager
from app.services.schema_browser import GRAPH_DB_TYPES
from app.services.settings_manager import settings_manager
from app.services.sql_analysis import strict_statement_type

logger = logging.getLogger(__name__)

MCP_SETTINGS_KEY = "mcp_settings"

GROUP_ROUTES: dict[McpToolGroup, str] = {
    "templates": QUERY_API_ROUTE_ID,
    "db_explore": QUERY_API_ROUTE_ID,
    "run_query": QUERY_API_ROUTE_ID,
    "s3": "s3-api",
    "nas": "nas-api",
    # Write tools additionally need NETWORK_WRITE_ROUTE_ID (build_tools).
    "network": NETWORK_API_ROUTE_ID,
}

SERVER_INSTRUCTIONS = (
    "UniBridge exposes the databases, saved query templates, S3 storage and NAS "
    "shares this API key may use. Look for a saved query with search_templates "
    "and run it with run_template when one answers the question; otherwise call "
    "list_databases, list_tables and describe_table "
    "before writing SQL for run_query, which runs one SELECT. Gateway APIs the "
    "administrator offers are found with search_apis and called with call_api. "
    "Tools named <server>__<tool> belong to external MCP servers the "
    "administrator connected. "
    "For a binary or whole file call download_artifact: it is staged and the "
    "result gives its id, size and SHA-256, the bytes being fetched with "
    "GET /api/mcp/artifacts/<id> and the same API key; where the tool offers "
    "return_mode=resource, a small file instead comes back as a standard "
    "embedded resource, for an MCP host that saves those into the workspace. "
    "upload_staged_artifacts sends staged artifacts to a gateway API as "
    "multipart/form-data. "
    "For whether one server can reach another, call network_check_access: hosts "
    "in one region reach each other on every port, other regions only through a "
    "recorded firewall opening, and a blocked answer carries a request_draft for "
    "the company firewall request system. To place a new service where its "
    "clients already get through, call network_find_ports. "
    "Results are capped: "
    "truncated=true means rows or bytes were cut, so narrow the query."
)

_TEMPLATE_SQL_PREVIEW = 1500
_TEMPLATE_SEARCH_DEFAULT = 20
_TEMPLATE_SEARCH_MAX = 100
# Room left in a query result for everything except the rows.
_ROW_ENVELOPE_BYTES = 512
_TRUNCATION_MARKER = "\n…[truncated at max_result_bytes]"
# The gateway's own read timeout for query-api/mcp-api is query_route_timeout;
# wait a little longer so its answer, not ours, ends a slow call.
_GATEWAY_TIMEOUT_MARGIN_SECONDS = 10


class ToolError(Exception):
    """A failure reported to the model as a tool result (``isError``)."""


@dataclass
class ToolContext:
    user: ApiKeyUser
    db: AsyncSession
    settings: McpSettings
    # The caller's key, sent on with each tool call. None outside a real MCP
    # request (the admin preview), where tools are listed but never run.
    api_key: str | None = None
    # External server name → the end user's token the client sent for it
    # (X-MCP-Auth-<name>); "" when the header came more than once.
    user_tokens: dict[str, str] = field(default_factory=dict, repr=False)
    # File bytes this HTTP request has already returned inline (embedded
    # resources): the messages of a batch share one allowance.
    inline_bytes_sent: int = 0


@dataclass
class TextFile:
    """A file read: metadata as JSON plus the raw text as its own content item."""

    meta: dict[str, Any]
    text: str
    # An answer worth showing that still failed (call_api's HTTP errors).
    is_error: bool = False


@dataclass
class ResourceFile:
    """A file returned inline: metadata as JSON plus an MCP embedded resource.

    The block is the protocol's own ``EmbeddedResource`` holding
    ``BlobResourceContents``, so a host stores it like any other server's
    file. ``_meta`` carries the size and SHA-256 of the bytes that were
    encoded, for the host to check what it decoded. Not cut to
    ``max_result_bytes``: its size is bounded where the file is read.
    """

    meta: dict[str, Any]
    uri: str
    content_type: str
    blob: str  # base64
    size: int
    sha256: str

    def block(self) -> dict[str, Any]:
        return {
            "type": "resource",
            "resource": {
                "uri": self.uri,
                "mimeType": self.content_type,
                "blob": self.blob,
                "_meta": {"size": self.size, "sha256": self.sha256},
            },
        }


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    group: McpToolGroup
    input_schema: dict[str, Any]
    handler: Handler
    read_only: bool = True
    open_world: bool = False
    # An external server's tool: its arguments go to the server unchecked (it
    # validates them) and the handler returns a whole CallToolResult.
    passthrough: bool = False
    server: str | None = None

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": not self.read_only,
                "openWorldHint": self.open_world,
            },
        }


# ── Settings ────────────────────────────────────────────────────────────────


async def load_settings(db: AsyncSession) -> McpSettings:
    row = await db.get(SystemConfig, MCP_SETTINGS_KEY)
    if row is None:
        return McpSettings()
    try:
        return McpSettings.model_validate_json(row.value)
    except ValidationError:
        logger.warning("Invalid %s in system_config, using defaults", MCP_SETTINGS_KEY)
        return McpSettings()


async def save_settings(db: AsyncSession, new: McpSettings) -> None:
    row = await db.get(SystemConfig, MCP_SETTINGS_KEY)
    value = new.model_dump_json()
    if row is None:
        db.add(SystemConfig(key=MCP_SETTINGS_KEY, value=value))
    else:
        row.value = value
    await db.commit()


# ── Grants ──────────────────────────────────────────────────────────────────


def has_route(user: ApiKeyUser, route_id: str) -> bool:
    return "*" in user.allowed_routes or route_id in user.allowed_routes


def _has_alias(user: ApiKeyUser, alias: str) -> bool:
    return "*" in user.allowed_databases or alias in user.allowed_databases


# ── Gateway ─────────────────────────────────────────────────────────────────

_client: httpx.AsyncClient | None = None


def _gateway_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=app_settings.APISIX_GATEWAY_URL,
            # The request carries the caller's API key: never via an env proxy.
            trust_env=False,
        )
    return _client


async def aclose() -> None:
    """Close the shared gateway client. Call on app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or response.reason_phrase
    return _json_error_detail(body)


def _json_error_detail(body: Any) -> str:
    if isinstance(body, dict):
        # FastAPI says "detail"; APISIX plugins (key-auth, limit-count, ...)
        # say "message" or "error_msg".
        for field in ("detail", "message", "error_msg"):
            if body.get(field):
                detail = body[field]
                return detail if isinstance(detail, str) else _dumps(detail)
    return _dumps(body)[:500]


async def _gateway(
    ctx: ToolContext,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send ``path`` (e.g. ``/api/query/execute``) through APISIX as the caller."""
    if not ctx.api_key:
        raise ToolError("Tool calls need the API key in the apikey header (call /api/mcp)")
    timeout = settings_manager.query_route_timeout + _GATEWAY_TIMEOUT_MARGIN_SECONDS
    # The call comes back into this app, which needs a meta-store connection to
    # authenticate it. Hand ours back first: holding it idle-in-transaction for
    # the whole call would let concurrent tool calls drain the pool their own
    # inner requests wait on. The session reconnects if it is used again.
    await ctx.db.rollback()
    try:
        response = await _gateway_client().request(
            method,
            path,
            params={k: v for k, v in (params or {}).items() if v is not None},
            json=json_body,
            headers={"apikey": ctx.api_key},
            timeout=httpx.Timeout(timeout, connect=10),
        )
    except httpx.HTTPError as exc:
        logger.warning("MCP gateway call %s %s failed: %s", method, path, exc)
        raise ToolError("The UniBridge gateway did not answer") from exc
    if response.status_code >= 400:
        raise ToolError(f"{response.status_code}: {_error_detail(response)}")
    return response


def _segment(value: str) -> str:
    return quote(value, safe="")


# ── Arguments ───────────────────────────────────────────────────────────────

_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def _object_schema(
    properties: dict[str, Any], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _validate_args(schema: dict[str, Any], args: dict[str, Any]) -> None:
    properties: dict[str, Any] = schema["properties"]
    missing = [name for name in schema["required"] if name not in args]
    if missing:
        raise ToolError(f"Missing required argument(s): {', '.join(missing)}")
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ToolError(f"Unknown argument(s): {', '.join(unknown)}")
    for name, value in args.items():
        if value is None and name not in schema["required"]:
            continue  # an optional argument sent as null reads as left out
        spec = properties[name]
        expected = spec.get("type")
        if isinstance(expected, str) and not _JSON_TYPES[expected](value):
            raise ToolError(f"Argument '{name}' must be of type {expected}")
        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                raise ToolError(f"Argument '{name}' must be >= {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                raise ToolError(f"Argument '{name}' must be <= {spec['maximum']}")
        if isinstance(value, str) and spec.get("minLength") and len(value) < spec["minLength"]:
            raise ToolError(f"Argument '{name}' must not be empty")


# ── Results ─────────────────────────────────────────────────────────────────


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_utf8(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[: max(max_bytes, 0)].decode("utf-8", errors="ignore")


def _capped_text(text: str, max_bytes: int) -> str:
    """``text`` within ``max_bytes`` UTF-8 bytes, the cut marker included."""
    if _utf8_len(text) <= max_bytes:
        return text
    return _cut_utf8(text, max_bytes - _utf8_len(_TRUNCATION_MARKER)) + _TRUNCATION_MARKER


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _query_payload(database: str, data: dict[str, Any], cfg: McpSettings) -> dict[str, Any]:
    """A /query response cut to the result caps."""
    if data.get("graph") is not None:
        # The graph is one serialized document: cut it as text, keep the JSON whole.
        budget = cfg.max_result_bytes - _ROW_ENVELOPE_BYTES
        graph = data["graph"]
        cut = False
        # Measured JSON-escaped; every byte cut removes at least one escaped byte.
        while graph and (excess := _utf8_len(_dumps(graph)) - budget) > 0:
            cut = True
            graph = _cut_utf8(graph, _utf8_len(graph) - excess)
        return {
            "database": database,
            "graph": graph,
            "truncated": cut,
            "truncated_reason": "max_result_bytes" if cut else None,
            "elapsed_ms": data["elapsed_ms"],
        }

    used = len(_dumps(data["columns"]).encode("utf-8")) + _ROW_ENVELOPE_BYTES
    rows: list[Any] = []
    cut_by_bytes = False
    for row in data["rows"]:
        used += len(_dumps(row).encode("utf-8")) + 1
        if used > cfg.max_result_bytes:
            cut_by_bytes = True
            break
        rows.append(row)
    if cut_by_bytes:
        reason: str | None = "max_result_bytes"
    elif data["truncated"]:
        # The executor also stops at its own result-size ceiling (MAX_RESULT_BYTES).
        reason = "max_rows" if len(rows) >= cfg.max_rows else "server_result_limit"
    else:
        reason = None
    return {
        "database": database,
        "columns": data["columns"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": reason is not None,
        "truncated_reason": reason,
        "elapsed_ms": data["elapsed_ms"],
    }


def _decode_text(data: bytes, *, truncated: bool) -> str:
    if b"\x00" in data:
        raise ToolError("Binary file: only UTF-8 text files can be read")
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        # A cut file may end inside a multi-byte character; the decoder keeps
        # that tail back instead of failing on it.
        return decoder.decode(data, final=not truncated)
    except UnicodeDecodeError as exc:
        raise ToolError("Not a UTF-8 text file") from exc


async def call_tool(ctx: ToolContext, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run ``tool`` and shape the outcome as an MCP ``CallToolResult``."""
    try:
        if not tool.passthrough:
            _validate_args(tool.input_schema, arguments)
        result = await tool.handler(ctx, arguments)
    except ToolError as exc:
        return _error_result(str(exc))
    except Exception:
        logger.exception("MCP tool %s failed", tool.name)
        return _error_result("Tool failed; see the UniBridge server logs")

    max_bytes = ctx.settings.max_result_bytes
    if tool.passthrough:
        return _capped_call_result(result, max_bytes)
    if isinstance(result, ResourceFile):
        content = [{"type": "text", "text": _dumps(result.meta)}, result.block()]
        return {"content": content, "isError": False}
    if isinstance(result, TextFile):
        meta = _dumps(result.meta)
        if result.meta.get("truncated") is False and _utf8_len(result.text) > max_bytes - _utf8_len(meta):
            # Cut here rather than at the read (decoding grew it): say so.
            meta = _dumps({**result.meta, "truncated": True})
        content = [
            {"type": "text", "text": meta},
            {"type": "text", "text": _capped_text(result.text, max_bytes - _utf8_len(meta))},
        ]
        return {"content": content, "isError": result.is_error}
    content = [{"type": "text", "text": _capped_text(_dumps(result), max_bytes)}]
    return {"content": content, "isError": False}


def _capped_call_result(result: Any, max_bytes: int) -> dict[str, Any]:
    """An external server's CallToolResult within ``max_bytes`` of content.

    Content items are kept in order while they fit; a text item that does not
    is cut, anything else (images, audio, embedded resources) is replaced by a
    note. structuredContent goes along only if it still fits.
    """
    if not isinstance(result, dict):
        return _error_result("The external server returned no tool result")
    budget = max_bytes
    content: list[dict[str, Any]] = []
    raw_content = result.get("content")
    for item in raw_content if isinstance(raw_content, list) else []:
        if not isinstance(item, dict):
            continue
        size = _utf8_len(_dumps(item))
        if size <= budget:
            content.append(item)
            budget -= size
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str) and budget > 256:
            text = _capped_text(item["text"], budget - 64)
            content.append({"type": "text", "text": text})
            budget = 0
            continue
        content.append(
            {"type": "text", "text": f"[{item.get('type', 'content')} item omitted: over max_result_bytes]"}
        )
        budget = 0
    shaped: dict[str, Any] = {"content": content, "isError": result.get("isError") is True}
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and _utf8_len(_dumps(structured)) <= budget:
        shaped["structuredContent"] = structured
    return shaped


# ── Databases ───────────────────────────────────────────────────────────────


def _require_database(user: ApiKeyUser, alias: str) -> str:
    if not _has_alias(user, alias):
        raise ToolError(f"This API key is not allowed to access database '{alias}'")
    db_type = connection_manager.get_db_type(alias)
    if db_type == "unknown":
        raise ToolError(f"Database '{alias}' is not registered or not connected")
    return db_type


async def _list_databases(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    aliases = sorted(
        alias for alias in connection_manager.list_aliases() if _has_alias(ctx.user, alias)
    )
    return {
        "databases": [
            {"database": alias, "type": connection_manager.get_db_type(alias)}
            for alias in aliases
        ]
    }


async def _list_tables(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    alias = args["database"]
    db_type = _require_database(ctx.user, alias)
    if db_type in GRAPH_DB_TYPES:
        return {
            "database": alias,
            "tables": [],
            "note": f"{db_type} has no tables; query it through run_query.",
        }
    response = await _gateway(ctx, "GET", f"/api/query/databases/{_segment(alias)}/tables")
    return response.json()


async def _describe_table(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    alias, table = args["database"], args["table"]
    _require_database(ctx.user, alias)
    response = await _gateway(
        ctx, "GET", f"/api/query/databases/{_segment(alias)}/tables/{_segment(table)}"
    )
    return response.json()


async def _run_query(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    alias, sql = args["database"], args["sql"]
    db_type = _require_database(ctx.user, alias)
    # MCP never writes, whatever the key's allow_insert/update/delete say, and
    # runs only SQL that parses as exactly one SELECT: /query/execute guesses
    # the type of unparsable text with a regex fallback, which errs strict but
    # is still a guess.
    if db_type in GRAPH_DB_TYPES:
        statement_type: str | None = query_router._detect_statement_type(sql, db_type)
    else:
        statement_type = strict_statement_type(sql, db_type)
    if statement_type != "select":
        raise ToolError(
            "run_query is read-only: it runs one SELECT (or a Cypher/SPARQL read) "
            "that the SQL parser can read"
        )
    response = await _gateway(
        ctx,
        "POST",
        "/api/query/execute",
        json_body={
            "database": alias,
            "sql": sql,
            "params": args.get("params"),
            "limit": ctx.settings.max_rows,
        },
    )
    return _query_payload(alias, response.json(), ctx.settings)


_DATABASE_ARG = {"type": "string", "minLength": 1, "description": "Database alias from list_databases"}

DB_EXPLORE_TOOLS = (
    Tool(
        name="list_databases",
        title="List databases",
        description="List the databases this API key may query, with their engine type.",
        group="db_explore",
        input_schema=_object_schema({}),
        handler=_list_databases,
    ),
    Tool(
        name="list_tables",
        title="List tables",
        description=(
            "List the tables of a database that this API key may read "
            "(Postgres: schema public; MSSQL: schema.table)."
        ),
        group="db_explore",
        input_schema=_object_schema({"database": _DATABASE_ARG}, ("database",)),
        handler=_list_tables,
    ),
    Tool(
        name="describe_table",
        title="Describe table",
        description="List the columns of a table: name, type and whether it is nullable.",
        group="db_explore",
        input_schema=_object_schema(
            {
                "database": _DATABASE_ARG,
                "table": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Table name from list_tables (schema.table allowed)",
                },
            },
            ("database", "table"),
        ),
        handler=_describe_table,
    ),
)

RUN_QUERY_TOOL = Tool(
    name="run_query",
    title="Run read-only query",
    description=(
        "Run one read-only statement (SQL SELECT, Cypher read, SPARQL query) "
        "against a database. Use :name placeholders with params for values. Rows "
        "are capped by the administrator; truncated=true means the result was cut."
    ),
    group="run_query",
    input_schema=_object_schema(
        {
            "database": _DATABASE_ARG,
            "sql": {"type": "string", "minLength": 1, "description": "The statement to run"},
            "params": {
                "type": "object",
                "description": "Named bind parameters for :name placeholders",
            },
        },
        ("database", "sql"),
    ),
    handler=_run_query,
)


# ── Query templates ─────────────────────────────────────────────────────────


async def accessible_templates(ctx: ToolContext) -> list[QueryTemplateResponse]:
    """Templates this key may run and the administrator has not hidden from MCP."""
    templates = await query_router.list_accessible_query_templates(user=ctx.user, db=ctx.db)
    excluded = set(ctx.settings.excluded_templates)
    return [template for template in templates if template.path not in excluded]


def _template_matches(template: QueryTemplateResponse, terms: list[str]) -> bool:
    haystack = f"{template.path}\n{template.name}\n{template.description}".casefold()
    return all(term in haystack for term in terms)


def _template_params(template: QueryTemplateResponse) -> list[str]:
    # The engine decides the placeholder syntax: $name on Neo4j, where a
    # :Label would otherwise read as a :name bind.
    return extract_template_params(
        template.sql, connection_manager.get_db_type(template.database)
    )


def _template_summary(template: QueryTemplateResponse) -> dict[str, Any]:
    sql = template.sql
    if len(sql) > _TEMPLATE_SQL_PREVIEW:
        sql = sql[:_TEMPLATE_SQL_PREVIEW] + " …"
    summary = {
        "template": template.path,
        "name": template.name,
        "description": template.description,
        "database": template.database,
        "params": _template_params(template),
        "sql": sql,
    }
    # A saved sample is a call known to return rows: an example of the values
    # each param takes.
    if template.sample_params is not None:
        summary["sample_params"] = template.sample_params
    return summary


async def _search_templates(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    terms = (args.get("query") or "").casefold().split()
    database = args.get("database")
    limit = args.get("limit") or _TEMPLATE_SEARCH_DEFAULT
    matches = [
        template
        for template in await accessible_templates(ctx)
        if (database is None or template.database == database)
        and _template_matches(template, terms)
    ]
    # Whole entries only, so the result stays valid JSON under max_result_bytes.
    used = _ROW_ENVELOPE_BYTES
    found: list[dict[str, Any]] = []
    for template in matches[:limit]:
        entry = _template_summary(template)
        used += _utf8_len(_dumps(entry)) + 1
        if used > ctx.settings.max_result_bytes:
            break
        found.append(entry)
    return {
        "templates": found,
        "total": len(matches),
        "truncated": len(found) < len(matches),
    }


def _find_template(
    templates: list[QueryTemplateResponse], wanted: str
) -> QueryTemplateResponse:
    for template in templates:
        if template.path == wanted:
            return template
    named = [t for t in templates if t.name.casefold() == wanted.strip().casefold()]
    if len(named) == 1:
        return named[0]
    if named:
        paths = ", ".join(t.path for t in named)
        raise ToolError(f"Several templates are named '{wanted}'; pass one of: {paths}")
    raise ToolError(f"No template '{wanted}' is available to this key; call search_templates")


async def _run_template(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    template = _find_template(await accessible_templates(ctx), args["template"])
    expected = _template_params(template)
    given: dict[str, Any] = args.get("params") or {}
    missing = [name for name in expected if name not in given]
    if missing:
        raise ToolError(f"Missing template param(s): {', '.join(missing)}")
    unknown = sorted(set(given) - set(expected))
    if unknown:
        raise ToolError(f"Unknown template param(s): {', '.join(unknown)}")
    max_rows = ctx.settings.max_rows
    limit = min(args.get("row_limit") or template.default_limit or max_rows, max_rows)
    response = await _gateway(
        ctx,
        "POST",
        "/api/query/templates/" + "/".join(_segment(part) for part in template.path.split("/")),
        json_body={"params": given or None, "limit": limit},
    )
    return _query_payload(template.database, response.json(), ctx.settings)


TEMPLATE_TOOLS = (
    Tool(
        name="search_templates",
        title="Search query templates",
        description=(
            "Find saved query templates this API key may run. Every word of query "
            "must appear in the template's path, name or description; leave it out "
            "to list them all. Each result gives the template id to pass to "
            "run_template, its database, its params and its SQL, and "
            "sample_params when the template has a saved example call."
        ),
        group="templates",
        input_schema=_object_schema(
            {
                "query": {"type": "string", "description": "Words to look for"},
                "database": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Only templates on this database alias",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _TEMPLATE_SEARCH_MAX,
                    "description": f"Maximum templates to return (default {_TEMPLATE_SEARCH_DEFAULT})",
                },
            }
        ),
        handler=_search_templates,
    ),
    Tool(
        name="run_template",
        title="Run query template",
        description=(
            "Run a saved query template by its id (the template field from "
            "search_templates) or its exact name. Pass a value for each of its "
            "params. Rows are capped by the administrator; truncated=true means "
            "the result was cut."
        ),
        group="templates",
        input_schema=_object_schema(
            {
                "template": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Template id (path) or exact name",
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Values for the template's params, by the names in the "
                        "params field from search_templates"
                    ),
                },
                "row_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum rows to return",
                },
            },
            ("template",),
        ),
        handler=_run_template,
    ),
)


# ── S3 ──────────────────────────────────────────────────────────────────────


def _require_s3(user: ApiKeyUser, alias: str) -> None:
    if not _has_alias(user, alias):
        raise ToolError(f"This API key is not allowed to access S3 connection '{alias}'")


async def _s3_list_connections(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "connections": sorted(
            alias for alias in s3_manager.list_aliases() if _has_alias(ctx.user, alias)
        )
    }


async def _s3_list_buckets(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _require_s3(ctx.user, args["connection"])
    response = await _gateway(ctx, "GET", f"/api/s3/{_segment(args['connection'])}/buckets")
    return {"buckets": response.json()}


async def _s3_list_objects(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _require_s3(ctx.user, args["connection"])
    response = await _gateway(
        ctx,
        "GET",
        f"/api/s3/{_segment(args['connection'])}/objects",
        params={
            "bucket": args["bucket"],
            "prefix": args.get("prefix") or "",
            "delimiter": "/",
            "max_keys": args.get("max_keys") or 200,
            "continuation_token": args.get("continuation_token"),
        },
    )
    return response.json()


async def _s3_get_object_metadata(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _require_s3(ctx.user, args["connection"])
    response = await _gateway(
        ctx,
        "GET",
        f"/api/s3/{_segment(args['connection'])}/objects/metadata",
        params={"bucket": args["bucket"], "key": args["key"]},
    )
    return response.json()


async def _s3_read_object(ctx: ToolContext, args: dict[str, Any]) -> TextFile:
    alias, bucket, key = args["connection"], args["bucket"], args["key"]
    _require_s3(ctx.user, alias)
    response = await _gateway(
        ctx,
        "GET",
        f"/api/s3/{_segment(alias)}/objects/download",
        params={"bucket": bucket, "key": key, "max_bytes": ctx.settings.max_result_bytes},
    )
    data = response.content
    size = int(response.headers.get("x-object-size", len(data)))
    truncated = size > len(data)
    return TextFile(
        meta={
            "connection": alias,
            "bucket": bucket,
            "key": key,
            "size": size,
            "content_type": response.headers.get("content-type"),
            "truncated": truncated,
        },
        text=_decode_text(data, truncated=truncated),
    )


_CONNECTION_ARG = {"type": "string", "minLength": 1, "description": "Connection alias"}
_BUCKET_ARG = {"type": "string", "minLength": 1, "description": "Bucket name"}
_KEY_ARG = {"type": "string", "minLength": 1, "description": "Object key"}

S3_TOOLS = (
    Tool(
        name="s3_list_connections",
        title="List S3 connections",
        description="List the S3 connections this API key may browse.",
        group="s3",
        input_schema=_object_schema({}),
        handler=_s3_list_connections,
    ),
    Tool(
        name="s3_list_buckets",
        title="List S3 buckets",
        description="List the buckets of an S3 connection.",
        group="s3",
        input_schema=_object_schema({"connection": _CONNECTION_ARG}, ("connection",)),
        handler=_s3_list_buckets,
    ),
    Tool(
        name="s3_list_objects",
        title="List S3 objects",
        description=(
            "List one level of a bucket: folders (common prefixes) and objects under "
            "prefix. When is_truncated is true, call again with next_continuation_token."
        ),
        group="s3",
        input_schema=_object_schema(
            {
                "connection": _CONNECTION_ARG,
                "bucket": _BUCKET_ARG,
                "prefix": {"type": "string", "description": "Key prefix, e.g. 'logs/2026/'"},
                "max_keys": {"type": "integer", "minimum": 1, "maximum": S3_MAX_PAGE_KEYS},
                "continuation_token": {"type": "string"},
            },
            ("connection", "bucket"),
        ),
        handler=_s3_list_objects,
    ),
    Tool(
        name="s3_get_object_metadata",
        title="Get S3 object metadata",
        description="Size, content type, last-modified time and user metadata of an object.",
        group="s3",
        input_schema=_object_schema(
            {"connection": _CONNECTION_ARG, "bucket": _BUCKET_ARG, "key": _KEY_ARG},
            ("connection", "bucket", "key"),
        ),
        handler=_s3_get_object_metadata,
    ),
    Tool(
        name="s3_read_object",
        title="Read S3 text object",
        description=(
            "Read a UTF-8 text object. Larger objects return only their beginning "
            "(truncated=true); binary objects are refused."
        ),
        group="s3",
        input_schema=_object_schema(
            {"connection": _CONNECTION_ARG, "bucket": _BUCKET_ARG, "key": _KEY_ARG},
            ("connection", "bucket", "key"),
        ),
        handler=_s3_read_object,
    ),
)


# ── NAS ─────────────────────────────────────────────────────────────────────


def _require_nas(user: ApiKeyUser, alias: str) -> None:
    if not _has_alias(user, alias):
        raise ToolError(f"This API key is not allowed to access NAS connection '{alias}'")


async def _nas_list_connections(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "connections": sorted(
            alias for alias in nas_manager.list_aliases() if _has_alias(ctx.user, alias)
        )
    }


async def _nas_list_entries(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _require_nas(ctx.user, args["connection"])
    response = await _gateway(
        ctx,
        "GET",
        f"/api/nas/{_segment(args['connection'])}/entries",
        params={
            "path": args.get("path") or "",
            "offset": args.get("offset") or 0,
            "limit": args.get("limit") or 200,
            "q": args.get("query") or "",
        },
    )
    return response.json()


async def _nas_get_metadata(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    _require_nas(ctx.user, args["connection"])
    response = await _gateway(
        ctx,
        "GET",
        f"/api/nas/{_segment(args['connection'])}/metadata",
        params={"path": args["path"]},
    )
    return response.json()


async def _nas_read_file(ctx: ToolContext, args: dict[str, Any]) -> TextFile:
    alias, path = args["connection"], args["path"]
    _require_nas(ctx.user, alias)
    response = await _gateway(
        ctx,
        "GET",
        f"/api/nas/{_segment(alias)}/download",
        params={"path": path, "max_bytes": ctx.settings.max_result_bytes},
    )
    data = response.content
    size = int(response.headers.get("x-file-size", len(data)))
    truncated = size > len(data)
    return TextFile(
        meta={
            "connection": alias,
            "path": path,
            "size": size,
            "content_type": response.headers.get("content-type"),
            "truncated": truncated,
        },
        text=_decode_text(data, truncated=truncated),
    )


_PATH_ARG = {"type": "string", "minLength": 1, "description": "Path relative to the share root"}

NAS_TOOLS = (
    Tool(
        name="nas_list_connections",
        title="List NAS connections",
        description="List the NAS shares this API key may browse.",
        group="nas",
        input_schema=_object_schema({}),
        handler=_nas_list_connections,
    ),
    Tool(
        name="nas_list_entries",
        title="List NAS directory",
        description=(
            "List one directory of a NAS share. query filters names by substring "
            "(case-insensitive); page with offset/limit."
        ),
        group="nas",
        input_schema=_object_schema(
            {
                "connection": _CONNECTION_ARG,
                "path": {"type": "string", "description": "Directory; empty = share root"},
                "query": {"type": "string", "description": "Name substring filter"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": app_settings.NAS_MAX_LIST_ENTRIES,
                },
            },
            ("connection",),
        ),
        handler=_nas_list_entries,
    ),
    Tool(
        name="nas_get_metadata",
        title="Get NAS entry metadata",
        description="Type, size and modification time of a NAS file or directory.",
        group="nas",
        input_schema=_object_schema(
            {"connection": _CONNECTION_ARG, "path": _PATH_ARG}, ("connection", "path")
        ),
        handler=_nas_get_metadata,
    ),
    Tool(
        name="nas_read_file",
        title="Read NAS text file",
        description=(
            "Read a UTF-8 text file. Larger files return only their beginning "
            "(truncated=true); binary files and files over the share's download "
            "limit are refused."
        ),
        group="nas",
        input_schema=_object_schema(
            {"connection": _CONNECTION_ARG, "path": _PATH_ARG}, ("connection", "path")
        ),
        handler=_nas_read_file,
    ),
)


# ── Network map ─────────────────────────────────────────────────────────────

_NETWORK_LIST_DEFAULT = 100
_NETWORK_LIST_MAX = 1000
_PROBE_BATCH_MAX = 500  # ProbeBatch's limit
_AGENT_RECORD_NOTE = (
    " What an API key records is marked source=agent and stays unverified until "
    "a person confirms it on the Network page."
)


def _capped_items(items: list[Any], key: str, limit: int, cfg: McpSettings) -> dict[str, Any]:
    """Whole entries only, so the result stays valid JSON under max_result_bytes."""
    used = _ROW_ENVELOPE_BYTES
    kept: list[Any] = []
    for item in items[:limit]:
        used += _utf8_len(_dumps(item)) + 1
        if used > cfg.max_result_bytes:
            break
        kept.append(item)
    return {key: kept, "total": len(items), "truncated": len(kept) < len(items)}


def _same_ip(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a.strip()) == ipaddress.ip_address(b.strip())
    except ValueError:
        return False


async def _network_host_id(ctx: ToolContext, ref: str) -> int:
    """A registered host's id from its IP, name (case-insensitive) or id.

    IP and name come first, so a host named e.g. "1001" is found by its name;
    a bare number that matches neither is taken as the id.
    """
    ref = ref.strip()
    response = await _gateway(ctx, "GET", "/api/network/hosts", params={"q": ref})
    hosts = [h for h in response.json() if isinstance(h, dict)]
    for host in hosts:
        if _same_ip(str(host.get("ip", "")), ref):
            return int(host["id"])
    for host in hosts:
        if str(host.get("name", "")).casefold() == ref.casefold():
            return int(host["id"])
    if ref.isdigit():
        return int(ref)
    raise ToolError(f"No registered host '{ref}'; call network_find_hosts")


def _body(args: dict[str, Any], *skip: str) -> dict[str, Any]:
    return {k: v for k, v in args.items() if v is not None and k not in skip}


async def _network_overview(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return (await _gateway(ctx, "GET", "/api/network/overview")).json()


async def _network_list_regions(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"regions": (await _gateway(ctx, "GET", "/api/network/regions")).json()}


async def _network_find_hosts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    response = await _gateway(
        ctx,
        "GET",
        "/api/network/hosts",
        params={
            "q": args.get("query"),
            "region_id": args.get("region_id"),
            "unverified": "true" if args.get("unverified") else None,
            "kind": args.get("kind"),
        },
    )
    limit = args.get("limit") or _NETWORK_LIST_DEFAULT
    return _capped_items(response.json(), "hosts", limit, ctx.settings)


async def _network_get_host(ctx: ToolContext, args: dict[str, Any]) -> Any:
    host_id = await _network_host_id(ctx, args["host"])
    return (await _gateway(ctx, "GET", f"/api/network/hosts/{host_id}")).json()


async def _network_check_access(ctx: ToolContext, args: dict[str, Any]) -> Any:
    response = await _gateway(
        ctx,
        "GET",
        "/api/network/check",
        params={
            "src": args["src"],
            "dst": args["dst"],
            "port": args["port"],
            "protocol": args.get("protocol") or "tcp",
        },
    )
    return response.json()


async def _network_find_ports(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sources = [str(ref).strip() for ref in args.get("sources") or [] if str(ref).strip()]
    region_ids = [int(r) for r in args.get("source_region_ids") or []]
    if not sources and not region_ids:
        raise ToolError("Give sources (host names or IPs) or source_region_ids")
    params: dict[str, Any] = {"protocol": args.get("protocol") or "tcp"}
    if sources:
        params["src"] = sources
    if region_ids:
        params["src_region"] = region_ids
    for arg, param in (("dst", "dst"), ("dst_region_id", "dst_region"), ("port", "port")):
        if args.get(arg) not in (None, ""):
            params[param] = args[arg]
    body = (await _gateway(ctx, "GET", "/api/network/port-finder", params=params)).json()
    limit = int(args.get("limit") or _NETWORK_LIST_DEFAULT)
    hosts = body.get("hosts", [])
    if len(hosts) > limit:
        body["hosts"] = hosts[:limit]
        body["truncated"] = True
    return body


async def _network_list_openings(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    host = args.get("host")
    response = await _gateway(
        ctx,
        "GET",
        "/api/network/openings",
        params={
            "host_id": await _network_host_id(ctx, host) if host else None,
            "region_id": args.get("region_id"),
            "status": args.get("status"),
            "port": args.get("port"),
            "unverified": "true" if args.get("unverified") else None,
        },
    )
    limit = args.get("limit") or _NETWORK_LIST_DEFAULT
    return _capped_items(response.json(), "openings", limit, ctx.settings)


async def _network_list_port_usages(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    host = args.get("host")
    response = await _gateway(
        ctx,
        "GET",
        "/api/network/port-usages",
        params={
            "host_id": await _network_host_id(ctx, host) if host else None,
            "port": args.get("port"),
            "source": args.get("source"),
            "unverified": "true" if args.get("unverified") else None,
        },
    )
    limit = args.get("limit") or _NETWORK_LIST_DEFAULT
    return _capped_items(response.json(), "port_usages", limit, ctx.settings)


async def _network_list_probes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    host = args.get("host")
    response = await _gateway(
        ctx,
        "GET",
        "/api/network/probes",
        params={
            "host_id": await _network_host_id(ctx, host) if host else None,
            "mismatch": "true" if args.get("mismatch") else None,
            "result": args.get("result"),
        },
    )
    limit = args.get("limit") or _NETWORK_LIST_DEFAULT
    return _capped_items(response.json(), "probes", limit, ctx.settings)


async def _network_probe_targets(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    response = await _gateway(
        ctx, "GET", "/api/network/probe-targets", params={"src": args["src"].strip()}
    )
    payload = response.json()
    targets = payload.pop("targets", []) if isinstance(payload, dict) else []
    limit = args.get("limit") or _NETWORK_LIST_MAX
    return {**payload, **_capped_items(targets, "targets", limit, ctx.settings)}


async def _network_report_probes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    src = args["src_host"].strip()
    items = args["results"]
    if not items or not all(isinstance(item, dict) for item in items):
        raise ToolError("results must be a non-empty list of objects")
    if len(items) > _PROBE_BATCH_MAX:
        raise ToolError(f"At most {_PROBE_BATCH_MAX} results per call; send the rest in another")
    # Every attempt is from src_host: a per-item source is not taken.
    results = [
        {**_body(item, "src_host", "src_host_id"), "src_host": src} for item in items
    ]
    response = await _gateway(
        ctx, "POST", "/api/network/probes", json_body={"results": results}
    )
    payload = response.json()
    stored = payload.get("stored", []) if isinstance(payload, dict) else []
    mismatches = [
        {
            key: probe.get(key)
            for key in (
                "id", "dst_host_name", "dst_host_ip", "port", "protocol", "result",
                "expected", "expected_reason", "mismatch",
            )
        }
        for probe in stored
        if isinstance(probe, dict) and probe.get("mismatch")
    ]
    return {
        "stored": len(stored),
        "errors": payload.get("errors", []) if isinstance(payload, dict) else [],
        **_capped_items(mismatches, "mismatches", _NETWORK_LIST_MAX, ctx.settings),
    }


async def _network_register_host(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return (await _gateway(ctx, "POST", "/api/network/hosts", json_body=_body(args))).json()


async def _network_update_host(ctx: ToolContext, args: dict[str, Any]) -> Any:
    host_id = await _network_host_id(ctx, args["host"])
    body = _body(args, "host")
    if not body:
        raise ToolError("Nothing to change: pass at least one field")
    return (
        await _gateway(ctx, "PUT", f"/api/network/hosts/{host_id}", json_body=body)
    ).json()


async def _network_record_opening(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return (await _gateway(ctx, "POST", "/api/network/openings", json_body=_body(args))).json()


async def _network_update_opening(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = _body(args, "opening_id")
    if not body:
        raise ToolError("Nothing to change: pass at least one field")
    return (
        await _gateway(
            ctx, "PUT", f"/api/network/openings/{args['opening_id']}", json_body=body
        )
    ).json()


async def _network_delete_opening(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _gateway(ctx, "DELETE", f"/api/network/openings/{args['opening_id']}")
    return {"deleted": True, "opening_id": args["opening_id"]}


async def _network_record_port_usage(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return (
        await _gateway(ctx, "POST", "/api/network/port-usages", json_body=_body(args))
    ).json()


async def _network_delete_port_usage(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    await _gateway(ctx, "DELETE", f"/api/network/port-usages/{args['usage_id']}")
    return {"deleted": True, "usage_id": args["usage_id"]}


_HOST_REF_ARG = {
    "type": "string",
    "minLength": 1,
    "description": "Registered host: its name, IP address or id",
}
_PORT_ARG = {"type": "integer", "minimum": 1, "maximum": 65535}
_PROTOCOL_ARG = {"type": "string", "enum": ["tcp", "udp"], "description": "tcp (default) or udp"}
_UNVERIFIED_ARG = {
    "type": "boolean",
    "description": "Only records nobody has confirmed yet",
}
_REGION_ID_ARG = {"type": "integer", "minimum": 1, "description": "Region id from network_list_regions"}
_LIST_LIMIT_ARG = {
    "type": "integer",
    "minimum": 1,
    "maximum": _NETWORK_LIST_MAX,
    "description": f"Maximum entries to return (default {_NETWORK_LIST_DEFAULT})",
}
_OPENING_ID_ARG = {"type": "integer", "minimum": 1, "description": "Opening id from network_list_openings"}
_OPENING_FIELDS: dict[str, Any] = {
    "src_host": {
        "type": "string",
        "minLength": 1,
        "description": "Source: a registered host's name or IP (or give src_cidr)",
    },
    "src_cidr": {
        "type": "string",
        "minLength": 1,
        "description": "Source as a network range, e.g. 10.1.0.0/16 (instead of src_host)",
    },
    "dst_host": {
        "type": "string",
        "minLength": 1,
        "description": "Destination: a registered host's name or IP",
    },
    "port_from": {**_PORT_ARG, "description": "First port (or the only one)"},
    "port_to": {**_PORT_ARG, "description": "Last port of a range; leave out for one port"},
    "protocol": _PROTOCOL_ARG,
    "status": {
        "type": "string",
        "enum": ["open", "closed"],
        "description": "open (default) or closed",
    },
    "ticket_no": {"type": "string", "description": "Firewall request ticket number"},
    "requester": {"type": "string", "description": "Who requested the opening"},
    "purpose": {"type": "string", "description": "What the opening is for"},
}
_HOST_FIELDS: dict[str, Any] = {
    "name": {"type": "string", "minLength": 1, "description": "Host name"},
    "ip": {"type": "string", "minLength": 1, "description": "IPv4 or IPv6 address"},
    "region": {
        "type": "string",
        "minLength": 1,
        "description": "Region name; leave out (and region_id) to place it by the regions' CIDRs",
    },
    "region_id": _REGION_ID_ARG,
    "description": {"type": "string"},
    "owner": {"type": "string", "description": "Owner or team"},
    "kind": {
        "type": "string",
        "enum": ["managed", "external"],
        "description": (
            "managed (default): a server we run. external: someone else's server, "
            "recorded only so openings can name it; it has no region"
        ),
    },
}

_PROBE_RESULT_ARG = {
    "type": "string",
    "enum": ["open", "refused", "timeout", "error"],
    "description": (
        "open: connected. refused: reached the host but nothing listens on the port "
        "(the firewall let it through). timeout: no answer, which is what a firewall "
        "dropping the connection looks like. error: anything else (DNS, no route, …)"
    ),
}

NETWORK_READ_TOOLS = (
    Tool(
        name="network_overview",
        title="Network map overview",
        description=(
            "Regions, the firewall openings between them (region-to-region ports), and "
            "counts of hosts, openings, unverified records, openings nothing listens "
            "on and usages the UniBridge platform host cannot reach."
        ),
        group="network",
        input_schema=_object_schema({}),
        handler=_network_overview,
    ),
    Tool(
        name="network_list_regions",
        title="List network regions",
        description=(
            "List the network regions (zones) with their CIDRs and host counts. Hosts "
            "in one region reach each other on every port."
        ),
        group="network",
        input_schema=_object_schema({}),
        handler=_network_list_regions,
    ),
    Tool(
        name="network_find_hosts",
        title="Find network hosts",
        description=(
            "Find registered hosts: query matches part of the name, IP or description. "
            "Each result gives the host's id, IP, region, kind (managed = ours, external = "
            "someone else's) and whether a person verified it."
        ),
        group="network",
        input_schema=_object_schema(
            {
                "query": {"type": "string", "description": "Part of a name, IP or description"},
                "region_id": _REGION_ID_ARG,
                "unverified": _UNVERIFIED_ARG,
                "kind": {
                    "type": "string",
                    "enum": ["managed", "external"],
                    "description": "Only hosts we run (managed) or only external ones",
                },
                "limit": _LIST_LIMIT_ARG,
            }
        ),
        handler=_network_find_hosts,
    ),
    Tool(
        name="network_get_host",
        title="Get network host",
        description=(
            "One host with its inbound and outbound firewall openings and the services "
            "recorded or discovered on its ports."
        ),
        group="network",
        input_schema=_object_schema({"host": _HOST_REF_ARG}, ("host",)),
        handler=_network_get_host,
    ),
    Tool(
        name="network_check_access",
        title="Check network access",
        description=(
            "Whether src can reach dst on a port. Hosts in the same region reach each "
            "other on every port, so the answer is always open there. Across regions "
            "it is open only through a recorded firewall opening that is not closed. "
            "src and dst are registered host names or IPs; an unregistered IP is "
            "placed in a region by the regions' CIDRs. status is open, blocked or "
            "unknown (a side is in no region); reason and message say why, openings "
            "lists the matching records and usages the services on that port. When "
            "blocked, request_draft holds text to submit to the company firewall "
            "request system: show it to the user rather than recording an opening "
            "that does not exist yet."
        ),
        group="network",
        input_schema=_object_schema(
            {
                "src": {"type": "string", "minLength": 1, "description": "Source host name or IP"},
                "dst": {"type": "string", "minLength": 1, "description": "Destination host name or IP"},
                "port": _PORT_ARG,
                "protocol": _PROTOCOL_ARG,
            },
            ("src", "dst", "port"),
        ),
        handler=_network_check_access,
    ),
    Tool(
        name="network_find_ports",
        title="Find ports a new service can use",
        description=(
            "Where can a new service listen so that the given sources reach it "
            "without a new firewall request? For each destination host (dst, or every "
            "host ranked best first): reachable = ports every source already reaches "
            "(any_port=true when all sources share its region), free = those minus "
            "ports in use (recorded, discovered, or a listener a probe found), "
            "suggestions = concrete free ports, probed_free ones measured as passing "
            "the firewall with nothing listening. blocked lists recorded-open ports a "
            "source's probe timed out on. With port, requested_port says whether that "
            "port is free, used, blocked or unreachable, and without dst only hosts "
            "where it is free are listed. When nothing fits, use network_check_access "
            "for the request_draft."
        ),
        group="network",
        input_schema=_object_schema(
            {
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 50,
                    "description": "Hosts (names or IPs) that must reach the service",
                },
                "source_region_ids": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "maxItems": 20,
                    "description": "Every host of these regions must reach it",
                },
                "dst": {"type": "string", "description": "Destination host; omit to rank every host"},
                "dst_region_id": _REGION_ID_ARG,
                "port": _PORT_ARG,
                "protocol": _PROTOCOL_ARG,
                "limit": _LIST_LIMIT_ARG,
            },
            (),
        ),
        handler=_network_find_ports,
    ),
    Tool(
        name="network_list_openings",
        title="List firewall openings",
        description=(
            "List recorded firewall openings between regions, optionally for one host "
            "(as source or destination), one region, a status, or a port inside the "
            "opening's range. in_use says whether a known service listens there."
        ),
        group="network",
        input_schema=_object_schema(
            {
                "host": _HOST_REF_ARG,
                "region_id": _REGION_ID_ARG,
                "status": {"type": "string", "enum": ["open", "closed"]},
                "port": _PORT_ARG,
                "unverified": _UNVERIFIED_ARG,
                "limit": _LIST_LIMIT_ARG,
            }
        ),
        handler=_network_list_openings,
    ),
    Tool(
        name="network_list_port_usages",
        title="List port usages",
        description=(
            "List which services use which host ports: recorded by people (manual) or "
            "agents (agent), or discovered from UniBridge's own configuration (auto)."
        ),
        group="network",
        input_schema=_object_schema(
            {
                "host": _HOST_REF_ARG,
                "port": _PORT_ARG,
                "source": {"type": "string", "enum": ["manual", "agent", "auto"]},
                "unverified": _UNVERIFIED_ARG,
                "limit": _LIST_LIMIT_ARG,
            }
        ),
        handler=_network_list_port_usages,
    ),
    Tool(
        name="network_list_probes",
        title="List connection probes",
        description=(
            "List the latest measured connection attempts between hosts (one per "
            "source, destination, port and protocol), newest first, each with what "
            "the records expect (expected: open, blocked or unknown). mismatch=true "
            "keeps only probes that contradict the records: "
            "recorded_open_but_blocked (an opening is recorded but the probe timed "
            "out: it may have expired) or passes_without_opening (the records say "
            "blocked but the probe got through: an opening nobody recorded)."
        ),
        group="network",
        input_schema=_object_schema(
            {
                "host": {**_HOST_REF_ARG, "description": "Source or destination host: name, IP or id"},
                "mismatch": {
                    "type": "boolean",
                    "description": "Only probes that contradict the recorded openings",
                },
                "result": _PROBE_RESULT_ARG,
                "limit": _LIST_LIMIT_ARG,
            }
        ),
        handler=_network_list_probes,
    ),
    Tool(
        name="network_probe_targets",
        title="Get probe targets",
        description=(
            "What a host should test to check the recorded openings: every opening "
            "whose source covers that host (the host itself or a CIDR containing its "
            "IP), as destination host, IP, port and protocol with what the records "
            "expect. Measure each from that host itself (a TCP connect with a short "
            "timeout) and send the results with network_report_probes."
        ),
        group="network",
        input_schema=_object_schema(
            {
                "src": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The host the tests run on: its registered name or IP",
                },
                "limit": _LIST_LIMIT_ARG,
            },
            ("src",),
        ),
        handler=_network_probe_targets,
    ),
)

NETWORK_WRITE_TOOLS = (
    Tool(
        name="network_register_host",
        title="Register network host",
        description=(
            "Register a host (server) on the network map. Without region or region_id "
            "it is placed by the regions' CIDRs." + _AGENT_RECORD_NOTE
        ),
        group="network",
        input_schema=_object_schema(_HOST_FIELDS, ("name", "ip")),
        handler=_network_register_host,
        read_only=False,
    ),
    Tool(
        name="network_update_host",
        title="Update network host",
        description=(
            "Change a registered host's name, IP, region, description or owner; fields "
            "left out stay as they are. The host becomes unverified again." + _AGENT_RECORD_NOTE
        ),
        group="network",
        input_schema=_object_schema({"host": _HOST_REF_ARG, **_HOST_FIELDS}, ("host",)),
        handler=_network_update_host,
        read_only=False,
    ),
    Tool(
        name="network_record_opening",
        title="Record firewall opening",
        description=(
            "Record a firewall opening that exists (it does not open anything): from "
            "src_host or src_cidr to dst_host on port_from[-port_to]. Openings inside "
            "one region are refused, as every port is open there already."
            + _AGENT_RECORD_NOTE
        ),
        group="network",
        input_schema=_object_schema(
            {
                **_OPENING_FIELDS,
                "create_missing_destination": {
                    "type": "boolean",
                    "description": (
                        "When dst_host is an IP no host has, register it as an external "
                        "host named dst-<ip> instead of failing"
                    ),
                },
            },
            ("dst_host", "port_from"),
        ),
        handler=_network_record_opening,
        read_only=False,
    ),
    Tool(
        name="network_update_opening",
        title="Update firewall opening",
        description=(
            "Change a recorded firewall opening (e.g. mark it closed, fix the ports or "
            "ticket); fields left out stay as they are. The opening becomes unverified "
            "again." + _AGENT_RECORD_NOTE
        ),
        group="network",
        input_schema=_object_schema(
            {"opening_id": _OPENING_ID_ARG, **_OPENING_FIELDS}, ("opening_id",)
        ),
        handler=_network_update_opening,
        read_only=False,
    ),
    Tool(
        name="network_delete_opening",
        title="Delete firewall opening",
        description=(
            "Delete a recorded firewall opening (a record removed, not a firewall rule). "
            "To keep its history, mark it closed with network_update_opening instead."
        ),
        group="network",
        input_schema=_object_schema({"opening_id": _OPENING_ID_ARG}, ("opening_id",)),
        handler=_network_delete_opening,
        read_only=False,
    ),
    Tool(
        name="network_record_port_usage",
        title="Record port usage",
        description=(
            "Record that a service uses a port on a registered host." + _AGENT_RECORD_NOTE
        ),
        group="network",
        input_schema=_object_schema(
            {
                "host": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Registered host's name or IP",
                },
                "port": _PORT_ARG,
                "protocol": _PROTOCOL_ARG,
                "service_name": {"type": "string", "minLength": 1, "description": "Service name"},
                "description": {"type": "string"},
                "owner": {"type": "string", "description": "Owner or team"},
            },
            ("host", "port", "service_name"),
        ),
        handler=_network_record_port_usage,
        read_only=False,
    ),
    Tool(
        name="network_delete_port_usage",
        title="Delete port usage",
        description=(
            "Delete a recorded port usage by id (from network_list_port_usages). "
            "Discovered (auto) usages have no id and cannot be deleted."
        ),
        group="network",
        input_schema=_object_schema(
            {"usage_id": {"type": "integer", "minimum": 1, "description": "Port usage id"}},
            ("usage_id",),
        ),
        handler=_network_delete_port_usage,
        read_only=False,
    ),
    Tool(
        name="network_report_probes",
        title="Report connection probes",
        description=(
            "Report connection attempts measured FROM src_host itself: run them on "
            "that server (e.g. a TCP connect with a few seconds' timeout to each of "
            "network_probe_targets), never from where the agent happens to run, or "
            "the result says nothing about that server's firewall path. result per "
            "attempt: open = connected; refused = reached the host but nothing "
            "listens (the firewall passes, the service is down or elsewhere); "
            "timeout = no answer, most likely a firewall dropping it; error = "
            "anything else (put the message in error). Each report replaces the "
            "previous one on its path unless that one is newer. The answer counts "
            "what was stored, lists items refused by index under errors, and lists "
            "mismatches where a probe contradicts the recorded openings. A probe "
            "is a measurement, not a record: it changes no opening."
        ),
        group="network",
        input_schema=_object_schema(
            {
                "src_host": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The registered host (name or IP) the attempts were made from",
                },
                "results": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": _PROBE_BATCH_MAX,
                    "items": {
                        "type": "object",
                        "properties": {
                            "dst_host": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Destination: a registered host's name or IP",
                            },
                            "port": _PORT_ARG,
                            "protocol": _PROTOCOL_ARG,
                            "result": _PROBE_RESULT_ARG,
                            "latency_ms": {
                                "type": "number",
                                "minimum": 0,
                                "description": "How long the connect took (open or refused)",
                            },
                            "error": {
                                "type": "string",
                                "maxLength": 255,
                                "description": "The error message, for result=error",
                            },
                            "observed_at": {
                                "type": "string",
                                "description": "When it was measured, ISO 8601 UTC; default now",
                            },
                        },
                        "required": ["dst_host", "port", "result"],
                        "additionalProperties": False,
                    },
                },
            },
            ("src_host", "results"),
        ),
        handler=_network_report_probes,
        read_only=False,
    ),
)


# ── Gateway APIs ────────────────────────────────────────────────────────────

# call_api sends nothing else, whatever the route allows.
API_METHODS = ("GET", "POST")
_API_MAX_TIMEOUT_SECONDS = 600
# Content types call_api returns as text; anything else is refused as binary.
_TEXT_TYPE_MARKERS = ("json", "xml", "javascript", "yaml", "csv", "x-www-form-urlencoded")
# Conditions besides uri and method that decide whether APISIX picks a route.
# Whether an MCP call meets them is not ours to judge: the caller sets the query
# args ``vars`` may test, and calls come from inside the network that
# ``remote_addrs`` may allow. So a route carrying one is never offered, and in
# the conflict check it counts as a route that might answer.
_CONDITION_FIELDS = ("host", "hosts", "vars", "remote_addrs", "remote_addr", "filter_func")


def _route_paths(route: dict[str, Any]) -> list[str]:
    uri = route.get("uri")
    if isinstance(uri, str) and uri:
        return [uri]
    uris = route.get("uris")
    return [u for u in uris if isinstance(u, str) and u] if isinstance(uris, list) else []


def _route_api_methods(route: dict[str, Any]) -> list[str]:
    methods = route.get("methods")
    if not isinstance(methods, list) or not methods:
        return list(API_METHODS)  # APISIX: no list means any method
    return [method for method in API_METHODS if method in methods]


def _route_enabled(route: dict[str, Any]) -> bool:
    return route.get("status", 1) != 0


def _key_auth_runs(route: dict[str, Any]) -> bool:
    plugins = route.get("plugins")
    conf = plugins.get("key-auth") if isinstance(plugins, dict) else None
    if not isinstance(conf, dict):
        return False
    meta = conf.get("_meta")
    return not (isinstance(meta, dict) and meta.get("disable"))


def offerable_route(route: dict[str, Any]) -> bool:
    """Whether an administrator may offer ``route`` over MCP at all."""
    return (
        isinstance(route.get("id"), str)
        and not is_system_route(route["id"])
        and _route_enabled(route)
        and bool(_route_paths(route))
        and bool(_route_api_methods(route))
        and not any(route.get(field) for field in _CONDITION_FIELDS)
        # Plugins bound elsewhere: whether key-auth runs, and so whether the
        # caller's key may be sent, cannot be read off the route.
        and not route.get("service_id")
        and not route.get("plugin_config_id")
    )


def route_info(route: dict[str, Any], description: str = "") -> McpRouteInfo:
    desc = route.get("desc")
    return McpRouteInfo(
        route_id=route["id"],
        name=route.get("name") or route["id"],
        paths=_route_paths(route),
        methods=_route_api_methods(route),
        key_auth=_key_auth_runs(route),
        description=description or (desc if isinstance(desc, str) else ""),
    )


async def gateway_routes() -> list[dict[str, Any]]:
    try:
        listing = await apisix_client.list_resources("routes")
    except Exception as exc:
        logger.warning("MCP could not list the APISIX routes: %s", exc)
        raise ToolError("The gateway routes could not be read") from exc
    return [item for item in listing.get("items", []) if isinstance(item, dict)]


def offered_route_ids(ctx: ToolContext) -> dict[str, str]:
    """Exposed route id → its MCP description, for the routes this key holds."""
    return {
        route.route_id: route.description
        for route in ctx.settings.exposed_routes
        if has_route(ctx.user, route.route_id)
    }


async def exposed_apis(
    ctx: ToolContext, routes: list[dict[str, Any]] | None = None
) -> list[McpRouteInfo]:
    """APIs this key may call over MCP: exposed by an administrator AND granted."""
    offered = offered_route_ids(ctx)
    if not offered:
        return []
    if routes is None:
        routes = await gateway_routes()
    apis = [
        route_info(route, offered[route["id"]])
        for route in routes
        if offerable_route(route) and route["id"] in offered
    ]
    return sorted(apis, key=lambda api: (api.name.casefold(), api.route_id))


async def _search_apis(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    terms = (args.get("query") or "").casefold().split()
    apis = [
        api
        for api in await exposed_apis(ctx)
        if all(
            term in f"{api.route_id}\n{api.name}\n{api.description}\n{' '.join(api.paths)}".casefold()
            for term in terms
        )
    ]
    return {
        "apis": [
            {
                "api": api.route_id,
                "name": api.name,
                "methods": api.methods,
                "paths": api.paths,
                "description": api.description,
            }
            for api in apis
        ]
    }


def _find_api(apis: list[McpRouteInfo], wanted: str) -> McpRouteInfo:
    for api in apis:
        if api.route_id == wanted:
            return api
    named = [api for api in apis if api.name.casefold() == wanted.strip().casefold()]
    if len(named) == 1:
        return named[0]
    if named:
        ids = ", ".join(api.route_id for api in named)
        raise ToolError(f"Several APIs are named '{wanted}'; pass one of: {ids}")
    raise ToolError(f"No API '{wanted}' is available to this key; call search_apis")


def _path_match(pattern: str, path: str) -> int | None:
    """APISIX-style specificity of ``pattern`` for ``path``, or None if it misses.

    An exact uri beats every prefix; among prefixes the longest wins.
    """
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return len(prefix) if path.startswith(prefix) else None
    return 1_000_000 + len(pattern) if path == pattern else None


def _match_rank(route: dict[str, Any], method: str, path: str) -> tuple[int, int] | None:
    if not _route_enabled(route):
        return None
    methods = route.get("methods")
    if isinstance(methods, list) and methods and method not in methods:
        return None
    scores = [score for uri in _route_paths(route) if (score := _path_match(uri, path)) is not None]
    if not scores:
        return None
    priority = route.get("priority")
    return max(scores), priority if isinstance(priority, int) else 0


def _surely_served_by(
    routes: list[dict[str, Any]], route: dict[str, Any], method: str, path: str
) -> bool:
    """Whether APISIX picks ``route`` for ``method path``, whatever else holds.

    ``path`` is the matched form (see _matched_path). Any other route that ranks
    as high spoils it: on a tie which one APISIX picks is not ours to rely on,
    and a route with conditions (_CONDITION_FIELDS) counts, as the call may
    meet them. A host-bound route spoils it at any rank: the default router
    (radixtree_host_uri) tries host routes before all others.
    """
    own_rank = _match_rank(route, method, path)
    if own_rank is None:
        return False
    for other in routes:
        if other is route:
            continue
        rank = _match_rank(other, method, path)
        if rank is None:
            continue
        if rank >= own_rank or other.get("host") or other.get("hosts"):
            return False
    return True


def _matched_path(path: str) -> str:
    """Check ``path`` and return it as APISIX matches it.

    APISIX matches nginx's ``$uri``: percent-decoded once, with ``//`` merged and
    dot segments resolved. Matching the raw text instead would let
    ``/api/x/%61dmin`` or ``/api/x//admin`` reach a route nobody offered, and
    httpx reads a leading ``//`` as an authority and drops it. So encoded slashes,
    ``//`` and dot segments are refused and the rest is decoded the same way.
    """
    if not path.startswith("/") or any(ch in path for ch in "?#\\") or any(
        ord(ch) < 33 for ch in path
    ):
        raise ToolError(
            "path must be an absolute request path without spaces, '?', '#' or '\\' "
            "(put URL parameters in query)"
        )
    lowered = path.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise ToolError("path must not contain encoded slashes")
    decoded = unquote(path)
    if "//" in decoded:
        raise ToolError("path must not contain '//'")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in decoded):
        raise ToolError("path must not contain control characters")
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise ToolError("path must not contain '.' or '..' segments")
    return decoded


def _check_query(query: dict[str, Any]) -> None:
    scalar = (str, int, float, bool)
    for name, value in query.items():
        values = value if isinstance(value, list) else [value]
        if not all(isinstance(item, scalar) for item in values):
            raise ToolError(f"query parameter '{name}' must be a string, number, boolean or a list of them")


def _is_text(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return not media or media.startswith("text/") or any(m in media for m in _TEXT_TYPE_MARKERS)


def _route_timeout(route: dict[str, Any]) -> float:
    timeout = route.get("timeout")
    read = timeout.get("read") if isinstance(timeout, dict) else None
    seconds = read if isinstance(read, (int, float)) and read > 0 else settings_manager.gateway_route_timeout
    return min(float(seconds), _API_MAX_TIMEOUT_SECONDS) + _GATEWAY_TIMEOUT_MARGIN_SECONDS


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > max_bytes:
            return bytes(data[:max_bytes]), True
    return bytes(data), False


def _query_value(value: Any) -> Any:
    # httpx renders True as "true"; keep that, and lists as repeated parameters.
    if isinstance(value, list):
        return [_query_value(item) for item in value]
    return "true" if value is True else "false" if value is False else value


@dataclass(frozen=True)
class _ApiCall:
    """A request to an exposed gateway API, checked and ready to send."""

    api: McpRouteInfo
    method: str
    path: str
    query: dict[str, Any]
    # The caller's key, for a key-auth route only.
    headers: dict[str, str]
    deadline: float

    @property
    def params(self) -> dict[str, Any]:
        return {name: _query_value(value) for name, value in self.query.items()}


async def _resolve_api_call(
    ctx: ToolContext, wanted: str, method: str, path: str | None, query: dict[str, Any]
) -> _ApiCall:
    """Check that ``method path`` is a call ``ctx`` may make on the API ``wanted``."""
    if not ctx.api_key:
        raise ToolError("Tool calls need the API key in the apikey header (call /api/mcp)")
    routes = await gateway_routes()
    api = _find_api(await exposed_apis(ctx, routes), wanted)
    method = method.upper()
    if method not in api.methods:
        raise ToolError(f"{api.name} accepts {', '.join(api.methods)} over MCP, not {method}")
    if path is None:
        exact = [p for p in api.paths if not p.endswith("*")]
        if len(api.paths) != 1 or not exact:
            raise ToolError(f"Pass path: a request path under {', '.join(api.paths)}")
        path = exact[0]
    matched = _matched_path(path)
    if not any(_path_match(pattern, matched) is not None for pattern in api.paths):
        raise ToolError(f"path must fall under {', '.join(api.paths)}")
    route = next(route for route in routes if route.get("id") == api.route_id)
    if not _surely_served_by(routes, route, method, matched):
        # A route as or more specific might answer: one nobody offered.
        raise ToolError(f"{method} {path} may be served by another gateway route, not {api.name}")
    _check_query(query)
    # A key-auth route's upstream gets the key as on a direct call (APISIX
    # removes it only with hide_credentials, which routes leave off); an open
    # route's upstream, which no key unlocks, never sees it.
    headers = {"apikey": ctx.api_key} if api.key_auth else {}
    return _ApiCall(api, method, path, query, headers, _route_timeout(route))


async def _send_api_call(
    ctx: ToolContext,
    call: _ApiCall,
    *,
    json_body: Any = None,
    form: dict[str, Any] | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    no_answer: str = "",
    binary_is_error: bool = True,
) -> TextFile:
    """Send ``call`` and return the answer as text, cut at the result cap.

    A binary answer is never returned. To call_api that is a failed read
    (``binary_is_error``); where the request itself was the point, the status
    alone says whether it worked.
    """
    await ctx.db.rollback()  # see _gateway: free the meta-store connection first
    max_bytes = ctx.settings.max_result_bytes
    try:
        # The read timeout is per chunk; the deadline bounds a trickling answer.
        async with asyncio.timeout(call.deadline), _gateway_client().stream(
            call.method,
            call.path,
            params=call.params,
            json=json_body,
            data=form,
            files=files,
            headers=call.headers,
            timeout=httpx.Timeout(call.deadline, connect=10),
        ) as response:
            content_type = response.headers.get("content-type", "")
            meta: dict[str, Any] = {
                "api": call.api.route_id,
                "method": call.method,
                "path": call.path,
                "status": response.status_code,
                "content_type": content_type or None,
            }
            if not _is_text(content_type):
                meta["truncated"] = False
                return TextFile(
                    meta=meta,
                    text=f"[binary response not returned: {content_type}]",
                    is_error=binary_is_error or response.status_code >= 400,
                )
            data, truncated = await _read_capped(response, max_bytes)
            encoding = response.charset_encoding or "utf-8"
    except TimeoutError as exc:
        raise ToolError(
            f"{call.api.name} did not answer within {call.deadline:.0f}s{no_answer}"
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("MCP call_api %s %s failed: %s", call.method, call.path, exc)
        raise ToolError(f"The UniBridge gateway did not answer{no_answer}") from exc
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    except LookupError:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    meta["truncated"] = truncated
    return TextFile(
        meta=meta,
        text=decoder.decode(data, final=not truncated),
        is_error=meta["status"] >= 400,
    )


async def _call_api(ctx: ToolContext, args: dict[str, Any]) -> TextFile:
    call = await _resolve_api_call(
        ctx, args["api"], args["method"], args.get("path"), args.get("query") or {}
    )
    body = args.get("body")
    if body is not None and call.method != "POST":
        raise ToolError("body is sent with POST only")
    return await _send_api_call(ctx, call, json_body=body)


API_TOOLS = (
    Tool(
        name="search_apis",
        title="Search gateway APIs",
        description=(
            "List the gateway APIs the administrator offers over MCP that this API "
            "key may call: id, name, methods (GET/POST), paths (a trailing * means "
            "any sub-path) and the administrator's notes. Every word of query must "
            "appear in the id, name, paths or notes. There are no request schemas: "
            "follow the notes, or read the API's own docs with call_api (often "
            "GET <path prefix>/docs or /openapi.json)."
        ),
        group="apis",
        input_schema=_object_schema({"query": {"type": "string", "description": "Words to look for"}}),
        handler=_search_apis,
    ),
    Tool(
        name="call_api",
        title="Call gateway API",
        description=(
            "Call an API from search_apis (by its id or exact name) with GET or POST. "
            "path is the full request path and must fall under one of the API's "
            "paths; query holds URL parameters; body is sent as JSON with POST. "
            "Returns the HTTP status, content type and response text, cut at the "
            "result size cap (truncated=true). Binary responses are not returned "
            "(download_artifact stages a GET's binary answer as a file). "
            "POST can change data on the other system."
        ),
        group="apis",
        input_schema=_object_schema(
            {
                "api": {"type": "string", "minLength": 1, "description": "API id or exact name"},
                "method": {"type": "string", "enum": list(API_METHODS), "description": "GET or POST"},
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Request path, e.g. /api/orders/v1/items (optional for a single exact path)",
                },
                "query": {"type": "object", "description": "URL query parameters"},
                "body": {"description": "JSON request body (POST only)"},
            },
            ("api", "method"),
        ),
        handler=_call_api,
        read_only=False,
        open_world=True,
    ),
)


# ── Artifacts ───────────────────────────────────────────────────────────────

# Where download_artifact reads from, and what each source is named by.
ARTIFACT_SOURCES = ("s3", "nas", "api")
_SOURCE_ARGS: dict[str, tuple[str, ...]] = {
    "s3": ("connection", "bucket", "key"),
    "nas": ("connection", "path"),
    "api": ("api",),
}
_SOURCE_OPTIONAL_ARGS: dict[str, tuple[str, ...]] = {"api": ("path", "query")}
# How download_artifact answers: a reference to the staged copy, or the file
# itself as an embedded resource.
RETURN_MODES = ("reference", "resource")
# A host names the saved file after the last segment of the resource URI and
# may refuse a long one (pi-gateway: over 200 UTF-8 bytes becomes artifact.bin,
# losing the extension), so the name in the URI is kept within this.
_RESOURCE_NAME_MAX_BYTES = 200
_RESOURCE_EXTENSION_MAX_BYTES = 32
_SOURCE_HELP = {
    "s3": "s3 = an S3 object, named by connection, bucket and key",
    "nas": "nas = a file on a NAS share, named by connection and path",
    "api": (
        "api = the answer to a GET on a gateway API from search_apis, named by api "
        "and path (plus query)"
    ),
}
# How much of a failed fetch's body is read for the error text.
_ERROR_BODY_BYTES = 2_000
# What one upload_staged_artifacts call may carry.
_UPLOAD_MAX_ARTIFACTS = 10
_UPLOAD_MAX_FIELDS = 64
_UPLOAD_MAX_FIELD_BYTES = 256 * 1024
_FORM_NAME_MAX_CHARS = 200
_DEFAULT_FILE_FIELD = "file"
_UNKNOWN_OUTCOME = "; whether it processed the upload is not known"


def holds_an_artifact_source(ctx: ToolContext) -> bool:
    """Whether the key holds a grant download_artifact could read through."""
    return (
        has_route(ctx.user, GROUP_ROUTES["s3"])
        or has_route(ctx.user, GROUP_ROUTES["nas"])
        or bool(offered_route_ids(ctx))
    )


def _artifact_sources(ctx: ToolContext, offers_api: bool) -> tuple[str, ...]:
    """The sources this key can read: each follows its own group and grant."""
    groups = ctx.settings.tool_groups
    available = {
        "s3": groups.s3 and has_route(ctx.user, GROUP_ROUTES["s3"]),
        "nas": groups.nas and has_route(ctx.user, GROUP_ROUTES["nas"]),
        "api": offers_api,
    }
    return tuple(source for source in ARTIFACT_SOURCES if available[source])


def _error_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    try:
        return _json_error_detail(json.loads(text))
    except ValueError:
        return text[:500] or "no detail"


def _declared_length(headers: httpx.Headers) -> int | None:
    """The body length the sender announced, if it says one about these bytes."""
    if headers.get("content-encoding"):
        return None  # the length of the encoded form, not of what we read
    value = headers.get("content-length", "")
    return int(value) if value.isdigit() else None


async def _fetch_file(
    ctx: ToolContext,
    path: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str],
    deadline: float,
    max_bytes: int,
    too_large: str,
) -> tuple[bytes, httpx.Headers]:
    """GET ``path`` through the gateway and return the whole body with its headers.

    Whole or not at all: any answer but 200, a body over ``max_bytes`` (refused
    with ``too_large``), one shorter than announced, a broken connection and a transfer past
    ``deadline`` all raise, so nothing partial is ever staged. A redirect is
    not a file and is not followed: its target is a URL nobody offered.
    """
    await ctx.db.rollback()  # see _gateway: free the meta-store connection first
    try:
        async with asyncio.timeout(deadline), _gateway_client().stream(
            "GET",
            path,
            params=params,
            # Byte counts are checked below: ask for the bytes as they are.
            headers={**headers, "accept-encoding": "identity"},
            timeout=httpx.Timeout(deadline, connect=10),
        ) as response:
            if response.status_code >= 400:
                detail, _ = await _read_capped(response, _ERROR_BODY_BYTES)
                raise ToolError(f"{response.status_code}: {_error_text(detail)}")
            if 300 <= response.status_code < 400:
                raise ToolError(
                    f"{response.status_code}: the answer is a redirect, which is not "
                    "followed; nothing was kept"
                )
            if response.status_code != 200:
                raise ToolError(
                    f"{response.status_code}: the answer carries no whole file; nothing was kept"
                )
            declared = _declared_length(response.headers)
            if declared is not None and declared > max_bytes:
                raise ToolError(too_large)
            data = await mcp_artifacts.collect(response.aiter_bytes(), max_bytes)
            if data is None:
                raise ToolError(too_large)
            if declared is not None and declared != len(data):
                raise ToolError(
                    f"The transfer was cut short ({len(data)} of {declared} bytes); nothing was kept"
                )
            return data, response.headers
    except TimeoutError as exc:
        raise ToolError(
            f"The file did not arrive within {deadline:.0f}s; nothing was kept"
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("MCP download_artifact GET %s failed: %s", path, exc)
        raise ToolError("The transfer was interrupted; nothing was kept") from exc


_DISPOSITION_EXTENDED = re.compile(r"filename\*\s*=\s*utf-8''([^;]+)", re.IGNORECASE)
_DISPOSITION_QUOTED = re.compile(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', re.IGNORECASE)
_DISPOSITION_TOKEN = re.compile(r"filename\s*=\s*([^;\s\"]+)", re.IGNORECASE)


def _response_filename(headers: httpx.Headers, fallback: str) -> str:
    """The file name a response gives itself, else the last segment of ``fallback``."""
    disposition = headers.get("content-disposition", "")
    candidates: list[str] = []
    if match := _DISPOSITION_EXTENDED.search(disposition):
        try:
            candidates.append(unquote(match.group(1).strip(), errors="strict"))
        except UnicodeDecodeError:
            pass
    if match := _DISPOSITION_QUOTED.search(disposition):
        candidates.append(re.sub(r"\\(.)", r"\1", match.group(1)))
    elif match := _DISPOSITION_TOKEN.search(disposition):
        candidates.append(match.group(1))
    candidates.append(fallback)
    for candidate in candidates:
        if name := mcp_artifacts.clean_filename(candidate):
            return name
    return mcp_artifacts.FALLBACK_FILENAME


def _resource_name(filename: str) -> str:
    """``filename`` as the resource URI carries it: shortened to fit, extension kept."""
    if _utf8_len(filename) <= _RESOURCE_NAME_MAX_BYTES:
        return filename
    dot = filename.rfind(".")
    extension = filename[dot:] if dot > 0 else ""
    if _utf8_len(extension) > _RESOURCE_EXTENSION_MAX_BYTES:
        extension = ""
    stem = filename[: len(filename) - len(extension)]
    # Cut on a character boundary; a name must not end in a space or a dot.
    stem = _cut_utf8(stem, _RESOURCE_NAME_MAX_BYTES - _utf8_len(extension)).rstrip(" .")
    return (stem or mcp_artifacts.FALLBACK_FILENAME) + extension


async def _download_artifact(
    ctx: ToolContext, args: dict[str, Any], sources: tuple[str, ...]
) -> Any:
    if not ctx.api_key:
        raise ToolError("Tool calls need the API key in the apikey header (call /api/mcp)")
    source = args["source"]
    if source not in sources:
        raise ToolError(f"source must be one of: {', '.join(sources)}")
    mode = args.get("return_mode") or RETURN_MODES[0]
    if mode not in RETURN_MODES:
        raise ToolError(f"return_mode must be one of: {', '.join(RETURN_MODES)}")
    given = {name for name, value in args.items() if value is not None} - {"source", "return_mode"}
    missing = [name for name in _SOURCE_ARGS[source] if name not in given]
    if missing:
        raise ToolError(f"source={source} needs: {', '.join(missing)}")
    extra = sorted(given - {*_SOURCE_ARGS[source], *_SOURCE_OPTIONAL_ARGS.get(source, ())})
    if extra:
        raise ToolError(f"source={source} does not take: {', '.join(extra)}")

    # Each source is read with the REST request a direct caller would send, so
    # the route's own checks decide: the key's connections, the bucket
    # allow-list, the share root (paths are resolved there), the route grant.
    own_key = {"apikey": ctx.api_key}
    deadline = float(settings_manager.query_route_timeout + _GATEWAY_TIMEOUT_MARGIN_SECONDS)
    if source == "s3":
        _require_s3(ctx.user, args["connection"])
        path = f"/api/s3/{_segment(args['connection'])}/objects/download"
        params: dict[str, Any] = {"bucket": args["bucket"], "key": args["key"]}
        headers, name_hint = own_key, args["key"]
    elif source == "nas":
        _require_nas(ctx.user, args["connection"])
        path = f"/api/nas/{_segment(args['connection'])}/download"
        params = {"path": args["path"]}
        headers, name_hint = own_key, args["path"]
    else:
        call = await _resolve_api_call(ctx, args["api"], "GET", args.get("path"), args.get("query") or {})
        path, params, headers, deadline = call.path, call.params, call.headers, call.deadline
        name_hint = call.path

    limits = mcp_artifacts.Limits.of(ctx.settings)
    consumer = ctx.user.consumer_name
    inline = mode == "resource"
    if inline:
        if not limits.inline_bytes:
            raise ToolError(
                "return_mode=resource is switched off by the administrator; "
                "call again with return_mode=reference"
            )
        # One HTTP request (a batch of calls) returns one allowance in all.
        max_bytes = limits.inline_bytes - ctx.inline_bytes_sent
        if max_bytes <= 0:
            raise ToolError(
                "This request already returns as much inline file data as one request "
                f"may ({limits.inline_bytes // mcp_artifacts.MIB} MiB); call again on its own "
                "or with return_mode=reference"
            )
        too_large = (
            "Larger than a file returned inline may be "
            f"({limits.inline_bytes // mcp_artifacts.MIB} MiB per request); "
            "call again with return_mode=reference"
        )
    else:
        max_bytes, too_large = limits.max_bytes, mcp_artifacts.too_large(limits).detail
    try:
        with mcp_artifacts.transfer_slot(consumer, limits):
            data, response_headers = await _fetch_file(
                ctx,
                path,
                params=params,
                headers=headers,
                deadline=deadline,
                max_bytes=max_bytes,
                too_large=too_large,
            )
            filename = _response_filename(response_headers, name_hint)
            content_type = mcp_artifacts.clean_content_type(response_headers.get("content-type"))
            if inline:
                # Not staged: the bytes leave in this answer. Size and SHA-256
                # are those of the bytes encoded, for the host to check.
                size, digest = len(data), await mcp_artifacts.sha256_hex(data)
                blob = await mcp_artifacts.base64_text(data)
            else:
                artifact, cached = await mcp_artifacts.stage(
                    ctx.db,
                    consumer=consumer,
                    source=source,
                    filename=filename,
                    content_type=content_type,
                    content=data,
                    limits=limits,
                )
    except mcp_artifacts.ArtifactError as exc:
        raise ToolError(exc.detail) from exc
    if not inline:
        return {**artifact.describe(), "cached": cached}
    ctx.inline_bytes_sent += size
    logger.info(
        "MCP artifact returned inline: consumer=%s source=%s size=%s sha256=%s",
        consumer,
        source,
        size,
        digest,
    )
    return ResourceFile(
        meta={"filename": filename, "size": size, "sha256": digest, "mime": content_type, "return_mode": mode},
        # The last segment is the file name a host saves under; the content's
        # own checksum stands in for an id, as nothing is staged.
        uri=f"artifact://{digest}/{quote(_resource_name(filename), safe='')}",
        content_type=content_type,
        blob=blob,
        size=size,
        sha256=digest,
    )


def _download_artifact_tool(sources: tuple[str, ...], inline_bytes: int) -> Tool:
    """download_artifact for the sources this key can read.

    ``inline_bytes`` is the largest file return_mode=resource carries; with 0
    the tool offers references only.
    """

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> Any:
        return await _download_artifact(ctx, args, sources)

    inline_mib = inline_bytes // mcp_artifacts.MIB

    properties: dict[str, Any] = {
        "source": {"type": "string", "enum": list(sources), "description": "Where the file is"}
    }
    if "s3" in sources or "nas" in sources:
        properties["connection"] = _CONNECTION_ARG
    if "s3" in sources:
        properties["bucket"] = _BUCKET_ARG
        properties["key"] = _KEY_ARG
    if "nas" in sources or "api" in sources:
        places = [
            text
            for source, text in (
                ("nas", "nas: path relative to the share root"),
                ("api", "api: request path, optional for a single exact path"),
            )
            if source in sources
        ]
        properties["path"] = {"type": "string", "minLength": 1, "description": "; ".join(places)}
    if "api" in sources:
        properties["api"] = {"type": "string", "minLength": 1, "description": "API id or exact name"}
        properties["query"] = {"type": "object", "description": "URL query parameters (api)"}
    # Listed even when only one mode is on, so a client that asks for the
    # other is told why instead of "unknown argument".
    properties["return_mode"] = {
        "type": "string",
        "enum": list(RETURN_MODES) if inline_bytes else [RETURN_MODES[0]],
        "description": (
            "reference (default): stage the file and return its id. resource: return "
            f"the file itself as an embedded resource, up to {inline_mib} MiB"
            if inline_bytes
            else "reference: stage the file and return its id (the only mode switched on)"
        ),
    }
    inline_help = (
        f" With return_mode=resource a file of up to {inline_mib} MiB comes back in the "
        "result itself, as a standard MCP embedded resource (base64 blob, with its size "
        "and sha256 in _meta). Use it when your MCP host saves embedded resources into "
        "your workspace (it then reports the saved path, with no further request); on a "
        "host that does not, the base64 only fills this result."
        if inline_bytes
        else ""
    )
    return Tool(
        name="download_artifact",
        title="Download file as artifact",
        description=(
            "Fetch a whole file, binary or text: "
            + "; ".join(_SOURCE_HELP[source] for source in sources)
            + "."
            + inline_help
            + " By reference (the default) the file is staged and the result holds no "
            "bytes: it gives artifact_id, filename, size, sha256, mime and expires_at. "
            "The bytes are then downloaded with GET /api/mcp/artifacts/<artifact_id> and "
            "the same API key and checked against size and sha256; that staged copy is "
            "also what upload_staged_artifacts sends on. A file over the size limit is "
            "refused, never cut. Staging the same content again returns the same "
            "artifact (cached=true)."
        ),
        group="artifacts",
        input_schema=_object_schema(properties, ("source",)),
        handler=handler,
    )


def _form_name(name: Any, what: str) -> str:
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= _FORM_NAME_MAX_CHARS
        or not mcp_artifacts.upload_relay.storable_text(name)
    ):
        raise ToolError(
            f"{what} must be a form field name: 1 to {_FORM_NAME_MAX_CHARS} characters, "
            "no control characters"
        )
    return name


def _form_fields(fields: dict[str, Any]) -> dict[str, str | list[str]]:
    """The text parts of the upload, each value rendered as a form sends it."""
    if len(fields) > _UPLOAD_MAX_FIELDS:
        raise ToolError(f"fields holds at most {_UPLOAD_MAX_FIELDS} entries")
    form: dict[str, str | list[str]] = {}
    used = 0
    for name, value in fields.items():
        _form_name(name, f"fields key {name!r}")
        values = value if isinstance(value, list) else [value]
        if not values or not all(isinstance(item, (str, int, float)) for item in values):
            raise ToolError(
                f"fields.{name} must be a string, number, boolean or a non-empty list of them"
            )
        texts = [
            "true" if item is True else "false" if item is False else str(item) for item in values
        ]
        try:
            used += sum(len(name.encode()) + len(item.encode()) for item in texts)
        except UnicodeEncodeError as exc:
            raise ToolError(f"fields.{name} is not valid Unicode text") from exc
        form[name] = texts if isinstance(value, list) else texts[0]
    if used > _UPLOAD_MAX_FIELD_BYTES:
        raise ToolError(f"fields holds at most {_UPLOAD_MAX_FIELD_BYTES} bytes in all")
    return form


def _artifact_parts(items: list[Any]) -> list[tuple[str, str, str | None]]:
    """(form field, artifact id, file name override) for each file part."""
    if not 1 <= len(items) <= _UPLOAD_MAX_ARTIFACTS:
        raise ToolError(f"artifacts holds 1 to {_UPLOAD_MAX_ARTIFACTS} entries")
    parts: list[tuple[str, str, str | None]] = []
    for index, item in enumerate(items):
        where = f"artifacts[{index}]"
        if not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str):
            raise ToolError(f"{where} must be an object with an artifact_id")
        unknown = sorted(set(item) - {"artifact_id", "field", "filename"})
        if unknown:
            raise ToolError(f"{where} does not take: {', '.join(unknown)}")
        field_name = item.get("field")
        field_name = (
            _DEFAULT_FILE_FIELD if field_name is None else _form_name(field_name, f"{where}.field")
        )
        filename = item.get("filename")
        if filename is not None:
            filename = mcp_artifacts.clean_filename(filename)
            if filename is None:
                raise ToolError(f"{where}.filename is not a usable file name")
        parts.append((field_name, item["artifact_id"], filename))
    return parts


async def _upload_staged_artifacts(ctx: ToolContext, args: dict[str, Any]) -> TextFile:
    destination = args["destination"]
    unknown = sorted(set(destination) - {"api", "path", "query"})
    if unknown:
        raise ToolError(f"destination does not take: {', '.join(unknown)}")
    api, path, query = destination.get("api"), destination.get("path"), destination.get("query")
    if not isinstance(api, str) or not api:
        raise ToolError("destination.api must name an API from search_apis")
    if path is not None and (not isinstance(path, str) or not path):
        raise ToolError("destination.path must be a request path")
    if query is not None and not isinstance(query, dict):
        raise ToolError("destination.query must be an object")
    # The destination is a path on an API the administrator offered and this
    # key holds: never a URL, never a route nobody offered.
    call = await _resolve_api_call(ctx, api, "POST", path, query or {})
    form = _form_fields(args.get("fields") or {})
    parts = _artifact_parts(args["artifacts"])

    limits = mcp_artifacts.Limits.of(ctx.settings)
    consumer = ctx.user.consumer_name
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    sent: list[dict[str, Any]] = []
    try:
        with mcp_artifacts.transfer_slot(consumer, limits):
            # Every artifact is read back and checked before the first byte
            # leaves: one that is missing, expired or damaged sends nothing.
            total = 0
            for index, (field_name, artifact_id, filename) in enumerate(parts):
                try:
                    artifact, content = await mcp_artifacts.read(ctx.db, consumer, artifact_id)
                except mcp_artifacts.ArtifactError as exc:
                    raise ToolError(f"artifacts[{index}]: {exc.detail}; nothing was sent") from exc
                total += artifact.size
                if total > limits.max_bytes:
                    raise ToolError(
                        "One upload carries at most "
                        f"{limits.max_bytes // mcp_artifacts.MIB} MiB of artifacts; nothing was sent"
                    )
                name = filename or artifact.filename
                files.append((field_name, (name, content, artifact.content_type)))
                sent.append(
                    {
                        "field": field_name,
                        "artifact_id": artifact.id,
                        "filename": name,
                        "size": artifact.size,
                        "sha256": artifact.sha256,
                    }
                )
            # The upload happened whatever the answer looks like: a binary
            # answer to a 2xx is left out, not reported as a failure the model
            # would answer by sending everything again.
            result = await _send_api_call(
                ctx, call, form=form, files=files, no_answer=_UNKNOWN_OUTCOME, binary_is_error=False
            )
    except mcp_artifacts.ArtifactError as exc:
        raise ToolError(exc.detail) from exc
    result.meta["artifacts"] = sent
    logger.info(
        "MCP artifacts uploaded: consumer=%s api=%s status=%s artifacts=%s",
        consumer,
        call.api.route_id,
        result.meta["status"],
        ",".join(item["artifact_id"] for item in sent),
    )
    return result


UPLOAD_STAGED_ARTIFACTS_TOOL = Tool(
    name="upload_staged_artifacts",
    title="Upload staged artifacts",
    description=(
        "Send staged artifacts to a gateway API from search_apis as one "
        "multipart/form-data POST. Each entry of artifacts goes out as a file part: "
        "artifact_id (from download_artifact, or from the MCP host staging a "
        "workspace file with POST /api/mcp/artifacts), the form field it is sent in "
        '(default "file") and optionally another filename. fields are the text '
        "parts. Only artifacts this API key staged can be sent, and each is checked "
        "against its recorded size and SHA-256 first: if one is missing, expired or "
        "damaged, nothing is sent. Returns the HTTP status, content type and "
        "response text like call_api (a binary answer is left out; the status says "
        "whether the upload worked), plus the artifacts sent. Artifacts stay staged "
        "until they expire, so a refused upload can be sent again; after a timeout "
        "the API may still have processed it, so do not simply send it again: check "
        "first, or repeat it with the same idempotency key where the API takes one "
        "as a field or query parameter. The POST can change data on the other system."
    ),
    group="artifacts",
    input_schema=_object_schema(
        {
            "destination": {
                "type": "object",
                "description": "Where to post",
                "properties": {
                    "api": {"type": "string", "description": "API id or exact name"},
                    "path": {
                        "type": "string",
                        "description": "Request path under the API's paths (optional for a single exact path)",
                    },
                    "query": {"type": "object", "description": "URL query parameters"},
                },
                "required": ["api"],
                "additionalProperties": False,
            },
            "artifacts": {
                "type": "array",
                "description": f"File parts, 1 to {_UPLOAD_MAX_ARTIFACTS}",
                "minItems": 1,
                "maxItems": _UPLOAD_MAX_ARTIFACTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "string", "description": "Staged artifact id"},
                        "field": {"type": "string", "description": 'Form field name (default "file")'},
                        "filename": {"type": "string", "description": "File name to send instead of the staged one"},
                    },
                    "required": ["artifact_id"],
                    "additionalProperties": False,
                },
            },
            "fields": {
                "type": "object",
                "description": "Text form fields: name → string, number, boolean or a list of them",
            },
        },
        ("destination", "artifacts"),
    ),
    handler=_upload_staged_artifacts,
    read_only=False,
    open_world=True,
)


# ── External MCP servers ────────────────────────────────────────────────────


def _external_tool(server: mcp_remote.ServerRef, upstream: dict[str, Any]) -> Tool:
    tool_name = upstream["name"]

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        if not ctx.api_key:
            raise ToolError("Tool calls need the API key in the apikey header (call /api/mcp)")
        user_token = None
        if server.user_token_header:
            header = mcp_remote.client_token_header(server.name)
            user_token = ctx.user_tokens.get(server.name)
            if user_token is None:
                raise ToolError(
                    f"{server.name}: this server acts as the end user; "
                    f"send their token in the {header} header of the /api/mcp request"
                )
            if not mcp_remote.usable_user_token(user_token):
                raise ToolError(f"{server.name}: the {header} header does not hold one usable token")
        secret = await mcp_remote.relay_secret(ctx.db)
        await ctx.db.rollback()  # see _gateway: free the meta-store connection first
        try:
            return await mcp_remote.call_tool(
                _gateway_client(),
                server,
                api_key=ctx.api_key,
                consumer=ctx.user.consumer_name,
                secret=secret,
                tool=tool_name,
                arguments=args,
                # Room for base64 media and JSON escaping; the result is cut
                # to max_result_bytes afterwards.
                max_bytes=ctx.settings.max_result_bytes * 4 + 65_536,
                user_token=user_token,
            )
        except mcp_remote.RemoteError as exc:
            raise ToolError(f"{server.name}: {exc}") from exc

    annotations = upstream.get("annotations") or {}
    title = upstream.get("title") or annotations.get("title") or tool_name
    return Tool(
        name=mcp_remote.exposed_name(server.name, tool_name),
        title=f"{server.name}: {title}",
        description=upstream.get("description") or "",
        group="external",
        input_schema=upstream["inputSchema"],
        handler=handler,
        read_only=annotations.get("readOnlyHint") is True,
        # MCP's default for a tool that does not say.
        open_world=annotations.get("openWorldHint") is not False,
        passthrough=True,
        server=server.name,
    )


def external_servers_granted(
    ctx: ToolContext, servers: list[McpRemoteServer]
) -> list[McpRemoteServer]:
    return [s for s in servers if has_route(ctx.user, mcp_remote.route_id(s.name))]


async def external_tools(ctx: ToolContext) -> list[Tool]:
    """Approved tools of the enabled servers whose relay route this key holds."""
    servers = await mcp_remote.load_servers(ctx.db, enabled_only=True)
    tools: list[Tool] = []
    for server in external_servers_granted(ctx, servers):
        ref = mcp_remote.ServerRef.of(server)
        tools.extend(_external_tool(ref, upstream) for upstream in mcp_remote.offered_tools(server))
    return tools


# ── Catalog ─────────────────────────────────────────────────────────────────


async def _offers_an_api(ctx: ToolContext) -> bool:
    """Whether a route this key may call is offered and still exists as one.

    Read from the gateway's short-TTL route listing, so a deleted or changed
    route drops the tools within seconds. If the listing cannot be read the
    tools stay: call_api checks a fresh listing anyway, and a blip should not
    take them away for the rest of a client's session.
    """
    offered = offered_route_ids(ctx)
    if not offered:
        return False
    try:
        routes = await gateway_router._list_routes_cached()
    except Exception as exc:
        logger.warning("MCP could not list the APISIX routes for tools/list: %s", exc)
        return True
    return any(
        isinstance(route, dict) and offerable_route(route) and route["id"] in offered
        for route in routes
    )


async def forget_exposed_route(db: AsyncSession, route_id: str) -> bool:
    """Drop a deleted route from ``exposed_routes``; True if it was there."""
    current = await load_settings(db)
    kept = [route for route in current.exposed_routes if route.route_id != route_id]
    if len(kept) == len(current.exposed_routes):
        return False
    await save_settings(db, current.model_copy(update={"exposed_routes": kept}))
    return True


async def build_tools(ctx: ToolContext) -> list[Tool]:
    """The tools this key sees: enabled groups whose route grant it holds."""
    groups = ctx.settings.tool_groups
    user = ctx.user
    tools: list[Tool] = []
    if groups.db_explore and has_route(user, GROUP_ROUTES["db_explore"]):
        tools.extend(DB_EXPLORE_TOOLS)
    if groups.run_query and has_route(user, GROUP_ROUTES["run_query"]):
        tools.append(RUN_QUERY_TOOL)
    if groups.s3 and has_route(user, GROUP_ROUTES["s3"]):
        tools.extend(S3_TOOLS)
    if groups.nas and has_route(user, GROUP_ROUTES["nas"]):
        tools.extend(NAS_TOOLS)
    if groups.templates and has_route(user, GROUP_ROUTES["templates"]):
        tools.extend(TEMPLATE_TOOLS)
    if groups.network and has_route(user, GROUP_ROUTES["network"]):
        tools.extend(NETWORK_READ_TOOLS)
        if has_route(user, NETWORK_WRITE_ROUTE_ID):
            tools.extend(NETWORK_WRITE_TOOLS)
    offers_api = groups.apis and await _offers_an_api(ctx)
    if offers_api:
        tools.extend(API_TOOLS)
    if groups.artifacts:
        # No grant of their own: each follows the source or destination it uses.
        sources = _artifact_sources(ctx, offers_api)
        if sources:
            inline_bytes = mcp_artifacts.Limits.of(ctx.settings).inline_bytes
            tools.append(_download_artifact_tool(sources, inline_bytes))
        if offers_api:
            tools.append(UPLOAD_STAGED_ARTIFACTS_TOOL)
    if groups.external:
        tools.extend(await external_tools(ctx))
    return tools
