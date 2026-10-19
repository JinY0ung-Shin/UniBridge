"""System-managed APISIX resource identifiers."""

QUERY_API_ROUTE_ID = "query-api"
QUERY_TEMPLATE_WRITE_ROUTE_ID = "query-template-write-api"

# The /api/llm-bi routes of the Bifrost side-by-side test and the second
# converter they used. Boot provisioning deletes them (app/main.py
# _retire_llm_bi_routes); they stay protected so no user resource takes an id
# provisioning would delete.
RETIRED_LLM_BI_ROUTE_IDS = ("llm-bi-proxy", "llm-bi-messages", "llm-bi-responses", "llm-bi-models")
RETIRED_LLM_BI_UPSTREAM_ID = "llm-converter-bi"
# Answers all of /api/llm-bi with a 404 that points to /api/llm. It proxies
# nothing, so it publishes no API and takes no key grants.
LLM_BI_NOT_FOUND_ROUTE_ID = "llm-bi-not-found"
# The same kind of route for /api/llm while LLM_GATEWAY=bifrost, where llm-proxy
# serves exact paths instead of the whole prefix (app/services/llm_routes.py).
LLM_NOT_FOUND_ROUTE_ID = "llm-not-found"

PROTECTED_ROUTE_IDS = {
    QUERY_API_ROUTE_ID,
    QUERY_TEMPLATE_WRITE_ROUTE_ID,
    "llm-proxy",
    "llm-admin",
    "s3-api",
    "llm-messages",
    "llm-responses",
    "nas-api",
    "usages-api",
    "prometheus-api",
    "llm-metrics",
    "llm-models",
    *RETIRED_LLM_BI_ROUTE_IDS,
    LLM_BI_NOT_FOUND_ROUTE_ID,
    LLM_NOT_FOUND_ROUTE_ID,
}
PROTECTED_UPSTREAM_IDS = {
    "unibridge-service",
    "litellm",
    "llm-converter",
    "prometheus",
    "bifrost",
    RETIRED_LLM_BI_UPSTREAM_ID,
}
