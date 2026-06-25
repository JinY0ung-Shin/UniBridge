"""System-managed APISIX resource identifiers."""

PROTECTED_ROUTE_IDS = {
    "query-api",
    "llm-proxy",
    "llm-admin",
    "s3-api",
    "llm-messages",
    "llm-responses",
    "nas-api",
}
PROTECTED_UPSTREAM_IDS = {"unibridge-service", "bifrost", "litellm", "llm-converter"}
