"""MCP tools over the query, S3 and NAS surfaces, scoped to one API key.

Every tool that reaches a database, S3 or NAS goes out as the matching REST
request through APISIX (``APISIX_GATEWAY_URL``) with the caller's own key. So
each call is authorized, rate-limited, audited and counted on that REST route
(``query-api``, ``s3-api``, ``nas-api``) exactly like a direct call, and the
``mcp-api`` route itself is left out of usage numbers. A tool group is offered
only when the key holds the route grant its REST twin needs (``GROUP_ROUTES``).
Discovery — tools/list, ``list_databases``, ``*_list_connections`` — touches
no backend and is answered here.

Settings (on/off, tool groups, result caps) live in ``system_config`` under
``MCP_SETTINGS_KEY`` and are read per request, so both blue/green colors see an
admin's change at once.
"""
from __future__ import annotations

import codecs
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ApiKeyUser
from app.config import settings as app_settings
from app.models import SystemConfig
from app.routers import query as query_router
from app.schemas import McpSettings, McpToolGroup, QueryTemplateResponse
from app.services.apisix_system_resources import QUERY_API_ROUTE_ID
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
}

SERVER_INSTRUCTIONS = (
    "UniBridge exposes the databases, saved query templates, S3 storage and NAS "
    "shares this API key may use. Prefer a tpl_* tool when one answers the "
    "question; otherwise call list_databases, list_tables and describe_table "
    "before writing SQL for run_query, which runs one SELECT. Results are capped: "
    "truncated=true means rows or bytes were cut, so narrow the query."
)

# Tool names: MCP clients accept [A-Za-z0-9_-], and several cap them at 64.
MAX_TOOL_NAME = 64
TEMPLATE_TOOL_PREFIX = "tpl_"
_TOOL_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
TEMPLATE_LIMIT_ARG = "row_limit"
_TEMPLATE_SQL_IN_DESCRIPTION = 1500
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


@dataclass
class TextFile:
    """A file read: metadata as JSON plus the raw text as its own content item."""

    meta: dict[str, Any]
    text: str


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    group: McpToolGroup
    input_schema: dict[str, Any]
    handler: Handler

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
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
        _validate_args(tool.input_schema, arguments)
        result = await tool.handler(ctx, arguments)
    except ToolError as exc:
        return _error_result(str(exc))
    except Exception:
        logger.exception("MCP tool %s failed", tool.name)
        return _error_result("Tool failed; see the UniBridge server logs")

    max_bytes = ctx.settings.max_result_bytes
    if isinstance(result, TextFile):
        meta = _dumps(result.meta)
        content = [
            {"type": "text", "text": meta},
            {"type": "text", "text": _capped_text(result.text, max_bytes - _utf8_len(meta))},
        ]
    else:
        content = [{"type": "text", "text": _capped_text(_dumps(result), max_bytes)}]
    return {"content": content, "isError": False}


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


def template_tool_name(path: str, taken: set[str]) -> str:
    name = TEMPLATE_TOOL_PREFIX + _TOOL_NAME_UNSAFE.sub("_", path)
    if len(name) > MAX_TOOL_NAME or name in taken:
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: MAX_TOOL_NAME - len(digest) - 1]}_{digest}"
    return name


def _template_tool(template: QueryTemplateResponse, name: str, max_rows: int) -> Tool:
    params = extract_template_params(template.sql)
    properties: dict[str, Any] = {
        param: {
            "type": ["string", "number", "integer", "boolean", "null"],
            "description": f"Value for :{param}",
        }
        for param in params
    }
    if TEMPLATE_LIMIT_ARG not in properties:
        properties[TEMPLATE_LIMIT_ARG] = {
            "type": "integer",
            "minimum": 1,
            "maximum": max_rows,
            "description": "Maximum rows to return",
        }
    sql = template.sql
    if len(sql) > _TEMPLATE_SQL_IN_DESCRIPTION:
        sql = sql[:_TEMPLATE_SQL_IN_DESCRIPTION] + " …"
    summary = template.name + (f": {template.description}" if template.description else "")
    path = template.path
    database = template.database
    default_limit = template.default_limit

    async def run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        bound = {param: args[param] for param in params if param in args}
        requested = args.get(TEMPLATE_LIMIT_ARG) if TEMPLATE_LIMIT_ARG not in params else None
        limit = min(requested or default_limit or ctx.settings.max_rows, ctx.settings.max_rows)
        response = await _gateway(
            ctx,
            "POST",
            "/api/query/templates/" + "/".join(_segment(part) for part in path.split("/")),
            json_body={"params": bound or None, "limit": limit},
        )
        return _query_payload(database, response.json(), ctx.settings)

    return Tool(
        name=name,
        title=template.name,
        description=f"{summary}\nSaved query on database '{database}':\n{sql}",
        group="templates",
        input_schema=_object_schema(properties, tuple(params)),
        handler=run,
    )


async def _template_tools(ctx: ToolContext) -> list[Tool]:
    templates = await query_router.list_accessible_query_templates(user=ctx.user, db=ctx.db)
    taken: set[str] = set()
    tools = []
    for template in templates:
        name = template_tool_name(template.path, taken)
        taken.add(name)
        tools.append(_template_tool(template, name, ctx.settings.max_rows))
    return tools


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


# ── Catalog ─────────────────────────────────────────────────────────────────


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
        tools.extend(await _template_tools(ctx))
    return tools
