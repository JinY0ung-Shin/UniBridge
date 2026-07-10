"""System-managed APISIX resource identifiers."""

QUERY_API_ROUTE_ID = "query-api"
QUERY_TEMPLATE_WRITE_ROUTE_ID = "query-template-write-api"

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
}
PROTECTED_UPSTREAM_IDS = {"unibridge-service", "litellm", "llm-converter"}
