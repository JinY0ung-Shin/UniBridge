"""System-managed APISIX resource identifiers, and the fixed service routes."""

from __future__ import annotations

from dataclasses import dataclass

QUERY_API_ROUTE_ID = "query-api"
QUERY_TEMPLATE_WRITE_ROUTE_ID = "query-template-write-api"
# MCP (Streamable HTTP) endpoint over the query/S3/NAS surfaces (app/routers/mcp.py).
MCP_API_ROUTE_ID = "mcp-api"
# Network map (app/routers/network.py) for API keys: reads, and writes as a
# separate grant, like query-api / query-template-write-api.
NETWORK_API_ROUTE_ID = "network-api"
NETWORK_WRITE_ROUTE_ID = "network-write-api"

# The removed /api/llm-bi routes of the Bifrost side-by-side test and the
# second converter they used. They stay protected, so no user resource takes
# one of their ids, while their old series are still in Prometheus (the LLM
# route metric exclusions in app/routers/gateway.py name them too).
RETIRED_LLM_BI_ROUTE_IDS = ("llm-bi-proxy", "llm-bi-messages", "llm-bi-responses", "llm-bi-models")
RETIRED_LLM_BI_UPSTREAM_ID = "llm-converter-bi"
# What LiteLLM had in APISIX: its admin UI route and its upstream. Gone, and
# protected for now like the llm-bi ids.
RETIRED_LITELLM_ROUTE_ID = "llm-admin"
RETIRED_LITELLM_UPSTREAM_ID = "litellm"
# Answers all of /api/llm-bi with a 404 that points to /api/llm. It proxies
# nothing, so it publishes no API and takes no key grants.
LLM_BI_NOT_FOUND_ROUTE_ID = "llm-bi-not-found"
# The same kind of route for /api/llm, where llm-proxy serves exact paths
# instead of the whole prefix (app/services/llm_routes.py).
LLM_NOT_FOUND_ROUTE_ID = "llm-not-found"


@dataclass(frozen=True)
class ServiceRoute:
    """A fixed key-auth route that boot provisioning writes (app/main.py).

    The route bodies are built from these fields in app/main.py
    (``_service_route``); the deploy script matches parts of what it writes
    (scripts/deploy-bluegreen.sh ``apisix_has_core_routes``).
    """

    id: str
    # One path is written as ``uri``, several as ``uris``.
    paths: tuple[str, ...]
    methods: tuple[str, ...]
    # proxy-rewrite ``regex_uri``: the gateway path to the upstream's.
    rewrite: tuple[str, str]
    desc: str | None = None
    priority: int | None = None
    upstream_id: str = "unibridge-service"
    # read/send follow the query route timeout (settings_manager), which the
    # settings UI can change at runtime.
    query_timeout: bool = False
    # Ships consumer-restriction deny-all, so the route is never callable by an
    # arbitrary key between its PUT and the consumer-restriction replay that
    # installs the stored grants; on later boots the replayed whitelist is
    # preserved (main._preserve_consumer_restriction).
    deny_all: bool = True
    # Sets X-UniBridge-Internal-Proxy, which app/auth.py authenticates
    # gateway-proxied requests by.
    internal_proxy: bool = True
    # proxy-rewrite ``use_real_request_uri_unsafe``.
    raw_request_uri: bool = True
    # key-auth ``hide_credentials: false``: the key reaches the upstream.
    forward_key: bool = False
    # Logged once the route is written.
    log: str | None = None


# The routes in front of this app's own APIs, in provisioning order.
APP_SERVICE_ROUTES: tuple[ServiceRoute, ...] = (
    ServiceRoute(
        QUERY_API_ROUTE_ID,
        ("/api/query/*",),
        ("POST", "GET"),
        ("^/api/query(.*)", "/query$1"),
        # Without the timeout, APISIX's default 60s read timeout cuts long
        # queries before the app's own timeout fires. The app allows
        # req.timeout up to 300s (schemas.py), so keep the gateway read above
        # that — the app then wins the race and returns a clean 408 + audit
        # log. Seeded from APISIX_QUERY_ROUTE_TIMEOUT (env) but
        # runtime-overridable via the settings UI, which live-patches this
        # route; boot provisioning reads settings_manager after it was loaded
        # from the DB, so it reflects the latest.
        query_timeout=True,
        deny_all=False,
        log="APISIX query route provisioned successfully",
    ),
    # Editing is a separate key grant from query read/execute.
    ServiceRoute(
        QUERY_TEMPLATE_WRITE_ROUTE_ID,
        ("/api/query/templates/*",),
        ("PUT", "PATCH", "DELETE"),
        ("^/api/query(.*)", "/query$1"),
        desc="Create, edit, and delete query templates",
        priority=20,
        log="APISIX query-template write route provisioned successfully",
    ),
    ServiceRoute(
        "s3-api",
        ("/api/s3/*",),
        ("GET",),
        ("^/api/s3(.*)", "/s3$1"),
        deny_all=False,
        log="APISIX S3 route provisioned successfully",
    ),
    ServiceRoute(
        "nas-api",
        ("/api/nas/*",),
        ("GET", "POST"),
        ("^/api/nas(.*)", "/nas$1"),
        log="APISIX NAS route provisioned successfully",
    ),
    # Self-scoped usage metrics (each consumer only ever sees its own
    # traffic), but access still follows the per-key route-grant model.
    ServiceRoute(
        "usages-api",
        ("/api/usages",),
        ("GET",),
        ("^/api/usages(.*)", "/usages$1"),
        log="APISIX usages route provisioned successfully",
    ),
    # The MCP server over the query/S3/NAS surfaces (app/routers/mcp.py). Its
    # tool calls wait on query-api, so the read timeout follows query-api's.
    # /api/mcp/artifacts carries the bytes of staged artifacts
    # (app/services/mcp_artifacts.py) on the same grant; the deploy script
    # re-provisions while the route lacks those paths. The app resends the key
    # with each tool call (mcp_tools._gateway), so it must reach it.
    ServiceRoute(
        MCP_API_ROUTE_ID,
        ("/api/mcp", "/api/mcp/artifacts", "/api/mcp/artifacts/*"),
        ("GET", "POST", "DELETE"),
        ("^/api/mcp(.*)", "/mcp$1"),
        desc="MCP server (Streamable HTTP): query templates, databases, S3, NAS",
        query_timeout=True,
        raw_request_uri=False,
        forward_key=True,
        log="APISIX MCP route provisioned successfully",
    ),
    # Network map (app/routers/network.py): reads on network-api, writes on
    # network-write-api, a separate grant like query-template-write-api. Same
    # uri, split by method.
    ServiceRoute(
        NETWORK_API_ROUTE_ID,
        ("/api/network/*",),
        ("GET",),
        ("^/api/network(.*)", "/network$1"),
        desc="Network map: regions, hosts, firewall openings, port usage, access checks",
    ),
    ServiceRoute(
        NETWORK_WRITE_ROUTE_ID,
        ("/api/network/*",),
        ("POST", "PUT", "DELETE"),
        ("^/api/network(.*)", "/network$1"),
        desc="Network map: record regions, hosts, firewall openings and port usage",
        log="APISIX network routes provisioned successfully",
    ),
)

# External PromQL access rides the gateway instead of a raw host port:
# Prometheus has no auth of its own, so a published :9090 is an open read of
# every metric. Behind key-auth the same surface is read-only — the container
# runs without --web.enable-admin-api / --web.enable-lifecycle, so there is
# nothing to write or reload through it. POST carries form-encoded
# /api/v1/query bodies, the only way to send a query too long for a URL.
PROMETHEUS_API_ROUTE = ServiceRoute(
    "prometheus-api",
    ("/api/prometheus/*",),
    ("GET", "POST"),
    ("^/api/prometheus(.*)", "$1"),
    desc="Prometheus HTTP API (PromQL queries, read-only) via the gateway",
    upstream_id="prometheus",
    internal_proxy=False,
    log="APISIX Prometheus route provisioned successfully",
)

SERVICE_ROUTES: tuple[ServiceRoute, ...] = (*APP_SERVICE_ROUTES, PROMETHEUS_API_ROUTE)
SERVICE_ROUTE_IDS = tuple(route.id for route in SERVICE_ROUTES)
# The routes whose requests reach this app through APISIX and are
# authenticated by the internal-proxy header (main._reconcile_internal_proxy_header).
INTERNAL_PROXY_ROUTE_IDS = tuple(route.id for route in SERVICE_ROUTES if route.internal_proxy)

PROTECTED_ROUTE_IDS = {
    *SERVICE_ROUTE_IDS,
    "llm-proxy",
    RETIRED_LITELLM_ROUTE_ID,
    "llm-messages",
    "llm-responses",
    "llm-metrics",
    "llm-models",
    *RETIRED_LLM_BI_ROUTE_IDS,
    LLM_BI_NOT_FOUND_ROUTE_ID,
    LLM_NOT_FOUND_ROUTE_ID,
}
PROTECTED_UPSTREAM_IDS = {
    "unibridge-service",
    RETIRED_LITELLM_UPSTREAM_ID,
    "llm-converter",
    "prometheus",
    "bifrost",
    RETIRED_LLM_BI_UPSTREAM_ID,
}

# External MCP servers (app/services/mcp_remote.py): one route and one upstream
# each, ``mcp-ext-<name>``, written from the MCP admin page. They are system
# resources to the gateway editor and the config transfer, like the ids above.
MCP_REMOTE_ID_PREFIX = "mcp-ext-"
# The path each relay route serves, under this prefix (mcp_remote.relay_path).
MCP_REMOTE_RELAY_PATH_PREFIX = "/_mcp-ext/"

# Every path the system routes serve, for the gateway's shadowing guard
# (routers/gateway.py, the config import): held here so the guard still stands
# when the live APISIX listing is unavailable. tests/test_main.py checks every
# provisioned path is covered.
SYSTEM_ROUTE_URIS: tuple[str, ...] = (
    *dict.fromkeys(path for route in SERVICE_ROUTES for path in route.paths),
    # The /api/llm routes (app/services/llm_routes.py).
    "/api/llm/*",
    "/api/llm/v1/messages",
    "/api/llm/v1/responses",
    # The whole prefix: llm-bi-not-found answers all of it since the Bifrost
    # side-by-side test was removed (llm_routes.retired_llm_bi_route).
    "/api/llm-bi/*",
    # The external MCP servers' relay routes (mcp_remote.relay_path), which
    # carry the relay secret and the callers' tokens.
    f"{MCP_REMOTE_RELAY_PATH_PREFIX}*",
)


def is_system_route(route_id: object) -> bool:
    """Whether the gateway editor must leave ``route_id`` alone."""
    return isinstance(route_id, str) and (
        route_id in PROTECTED_ROUTE_IDS or route_id.startswith(MCP_REMOTE_ID_PREFIX)
    )


def is_system_upstream(upstream_id: object) -> bool:
    return isinstance(upstream_id, str) and (
        upstream_id in PROTECTED_UPSTREAM_IDS or upstream_id.startswith(MCP_REMOTE_ID_PREFIX)
    )


def is_mcp_remote_resource(resource_id: object) -> bool:
    return isinstance(resource_id, str) and resource_id.startswith(MCP_REMOTE_ID_PREFIX)
