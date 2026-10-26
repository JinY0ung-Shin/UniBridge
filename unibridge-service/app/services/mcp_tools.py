"""MCP tools over the query, S3 and NAS surfaces, scoped to one API key.

Every tool that reaches a database, S3 or NAS goes out as the matching REST
request through APISIX (``APISIX_GATEWAY_URL``) with the caller's own key. So
each call is authorized, rate-limited, audited and counted on that REST route
(``query-api``, ``s3-api``, ``nas-api``) exactly like a direct call, and the
``mcp-api`` route itself is left out of usage numbers. A tool group is offered
only when the key holds the route grant its REST twin needs (``GROUP_ROUTES``).
Gateway APIs (``search_apis``/``call_api``) are the routes an administrator
lists in ``exposed_routes``, each offered only to keys holding its grant;
``call_api`` sends GET or POST only, on the route itself. External MCP
servers' approved tools come in as ``<server>__<tool>``, offered to keys holding
the server's relay route and relayed by :mod:`app.services.mcp_remote`.
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import logging
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
from app.services import apisix_client, mcp_remote
from app.services.apisix_system_resources import QUERY_API_ROUTE_ID, is_system_route
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
    "shares this API key may use. Look for a saved query with search_templates "
    "and run it with run_template when one answers the question; otherwise call "
    "list_databases, list_tables and describe_table "
    "before writing SQL for run_query, which runs one SELECT. Gateway APIs the "
    "administrator offers are found with search_apis and called with call_api. "
    "Tools named <server>__<tool> belong to external MCP servers the "
    "administrator connected. "
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


@dataclass
class TextFile:
    """A file read: metadata as JSON plus the raw text as its own content item."""

    meta: dict[str, Any]
    text: str
    # An answer worth showing that still failed (call_api's HTTP errors).
    is_error: bool = False


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


def _template_summary(template: QueryTemplateResponse) -> dict[str, Any]:
    sql = template.sql
    if len(sql) > _TEMPLATE_SQL_PREVIEW:
        sql = sql[:_TEMPLATE_SQL_PREVIEW] + " …"
    return {
        "template": template.path,
        "name": template.name,
        "description": template.description,
        "database": template.database,
        "params": extract_template_params(template.sql),
        "sql": sql,
    }


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
    expected = extract_template_params(template.sql)
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
            "run_template, its database, its params and its SQL."
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
                    "description": "Values for the template's :name params",
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


async def _call_api(ctx: ToolContext, args: dict[str, Any]) -> TextFile:
    if not ctx.api_key:
        raise ToolError("Tool calls need the API key in the apikey header (call /api/mcp)")
    routes = await gateway_routes()
    api = _find_api(await exposed_apis(ctx, routes), args["api"])
    method = args["method"].upper()
    if method not in api.methods:
        raise ToolError(f"{api.name} accepts {', '.join(api.methods)} over MCP, not {method}")
    path = args.get("path")
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
    query = args.get("query") or {}
    _check_query(query)
    body = args.get("body")
    if body is not None and method != "POST":
        raise ToolError("body is sent with POST only")

    # A key-auth route's upstream gets the key as on a direct call (APISIX
    # removes it only with hide_credentials, which routes leave off); an open
    # route's upstream, which no key unlocks, never sees it.
    headers = {"apikey": ctx.api_key} if api.key_auth else {}
    await ctx.db.rollback()  # see _gateway: free the meta-store connection first
    max_bytes = ctx.settings.max_result_bytes
    deadline = _route_timeout(route)
    try:
        # The read timeout is per chunk; the deadline bounds a trickling answer.
        async with asyncio.timeout(deadline), _gateway_client().stream(
            method,
            path,
            params={name: _query_value(value) for name, value in query.items()},
            json=body if method == "POST" and body is not None else None,
            headers=headers,
            timeout=httpx.Timeout(deadline, connect=10),
        ) as response:
            content_type = response.headers.get("content-type", "")
            meta: dict[str, Any] = {
                "api": api.route_id,
                "method": method,
                "path": path,
                "status": response.status_code,
                "content_type": content_type or None,
            }
            if not _is_text(content_type):
                meta["truncated"] = False
                return TextFile(
                    meta=meta,
                    text=f"[binary response not returned: {content_type}]",
                    is_error=True,
                )
            data, truncated = await _read_capped(response, max_bytes)
            encoding = response.charset_encoding or "utf-8"
    except TimeoutError as exc:
        raise ToolError(f"{api.name} did not answer within {deadline:.0f}s") from exc
    except httpx.HTTPError as exc:
        logger.warning("MCP call_api %s %s failed: %s", method, path, exc)
        raise ToolError("The UniBridge gateway did not answer") from exc
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
            "result size cap (truncated=true). Binary responses are not returned. "
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


# ── External MCP servers ────────────────────────────────────────────────────


def _external_tool(server: mcp_remote.ServerRef, upstream: dict[str, Any]) -> Tool:
    tool_name = upstream["name"]

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        if not ctx.api_key:
            raise ToolError("Tool calls need the API key in the apikey header (call /api/mcp)")
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
    if groups.apis and await _offers_an_api(ctx):
        tools.extend(API_TOOLS)
    if groups.external:
        tools.extend(await external_tools(ctx))
    return tools
