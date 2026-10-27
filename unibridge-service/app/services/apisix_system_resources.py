"""System-managed APISIX resource identifiers."""

QUERY_API_ROUTE_ID = "query-api"
QUERY_TEMPLATE_WRITE_ROUTE_ID = "query-template-write-api"
# MCP (Streamable HTTP) endpoint over the query/S3/NAS surfaces (app/routers/mcp.py).
MCP_API_ROUTE_ID = "mcp-api"

# The /api/llm-bi routes of the Bifrost side-by-side test and the second
# converter they used. Boot provisioning deletes them (app/main.py
# _retire_llm_bi_routes); they stay protected so no user resource takes an id
# provisioning would delete.
RETIRED_LLM_BI_ROUTE_IDS = ("llm-bi-proxy", "llm-bi-messages", "llm-bi-responses", "llm-bi-models")
RETIRED_LLM_BI_UPSTREAM_ID = "llm-converter-bi"
# What LiteLLM had in APISIX: its admin UI route and its upstream. Boot
# provisioning deletes them (app/main.py _retire_litellm_routing) and they stay
# protected for the same reason.
RETIRED_LITELLM_ROUTE_ID = "llm-admin"
RETIRED_LITELLM_UPSTREAM_ID = "litellm"
# Answers all of /api/llm-bi with a 404 that points to /api/llm. It proxies
# nothing, so it publishes no API and takes no key grants.
LLM_BI_NOT_FOUND_ROUTE_ID = "llm-bi-not-found"
# The same kind of route for /api/llm, where llm-proxy serves exact paths
# instead of the whole prefix (app/services/llm_routes.py).
LLM_NOT_FOUND_ROUTE_ID = "llm-not-found"

PROTECTED_ROUTE_IDS = {
    QUERY_API_ROUTE_ID,
    QUERY_TEMPLATE_WRITE_ROUTE_ID,
    "llm-proxy",
    RETIRED_LITELLM_ROUTE_ID,
    "s3-api",
    "llm-messages",
    "llm-responses",
    "nas-api",
    "usages-api",
    MCP_API_ROUTE_ID,
    "prometheus-api",
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
