"""System-managed APISIX resource identifiers."""

QUERY_API_ROUTE_ID = "query-api"
QUERY_TEMPLATE_WRITE_ROUTE_ID = "query-template-write-api"

# The /api/llm-bi routes in front of Bifrost (app/services/bifrost_routes.py).
BIFROST_ROUTE_IDS = ("llm-bi-proxy", "llm-bi-messages", "llm-bi-responses", "llm-bi-models")
# Answers every /api/llm-bi request those routes do not serve with a 404 that
# says why. It proxies nothing, so it publishes no API and takes no key grants.
BIFROST_NOT_FOUND_ROUTE_ID = "llm-bi-not-found"
BIFROST_UPSTREAM_IDS = ("bifrost", "llm-converter-bi")

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
    *BIFROST_ROUTE_IDS,
    BIFROST_NOT_FOUND_ROUTE_ID,
}
PROTECTED_UPSTREAM_IDS = {
    "unibridge-service",
    "litellm",
    "llm-converter",
    "prometheus",
    *BIFROST_UPSTREAM_IDS,
}
