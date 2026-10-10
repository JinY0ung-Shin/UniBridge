"""The /api/llm gateway routes, on LiteLLM or on Bifrost (LLM_GATEWAY).

Both gateways get the same route ids, so API-key grants (the consumer-restriction
whitelists), the grants an ``llm-proxy`` grant implies and the monitoring
selectors keyed on route ids all carry over a switch in either direction. Only
the upstreams and the injected headers change:

- ``llm-proxy`` is the raw OpenAI-compatible surface. On LiteLLM it takes all of
  ``/api/llm/*``. On Bifrost it takes only the three inference paths, because
  Bifrost answers any other extension-less path with its UI's index.html (200)
  and serves its management API under ``/api/*``; ``llm-not-found`` answers the
  rest with a 404 that lists what is served.
- ``llm-messages``, ``llm-responses`` and ``llm-models`` go to the per-color
  llm-converter on both, and the converter reads LLM_GATEWAY for its own
  upstream. Their credentials do not depend on the switch: they carry the one
  of each gateway that is configured, and the converter forwards only its own.
  A deploy rewrites the routes when the new color boots but moves the converter
  upstream only at promotion, so in between the old color's converter must still
  find its credential, whichever way the switch went.
- ``llm-metrics`` is the gateway's own Prometheus exposition.

Boot provisioning (app/main.py ``_provision_llm_routes``) does the writes.
"""
from __future__ import annotations

from typing import Any

from app.services import bifrost_routes
from app.services.apisix_system_resources import LLM_NOT_FOUND_ROUTE_ID
from app.services.consumer_restrictions import DENY_ALL_CONSUMER

PATH_PREFIX = "/api/llm"

LITELLM_UPSTREAM: dict[str, Any] = {
    "name": "litellm",
    "type": "roundrobin",
    "scheme": "https",
    "nodes": {"litellm:4000": 1},
}

# LLM responses can stay silent past APISIX's default 60s read timeout (long
# TTFT, reasoning, large non-stream completions); allow long reads so the
# gateway doesn't drop the socket.
TIMEOUT = dict(bifrost_routes.TIMEOUT)

_REWRITE = {"regex_uri": [f"^{PATH_PREFIX}(.*)", "$1"], "use_real_request_uri_unsafe": True}
# Routes to Bifrost rewrite the normalized path instead. APISIX picks the route on
# the normalized path, so with the raw one `/api/llm/%2e%2e/llm/metrics` matched
# llm-metrics and reached Bifrost as `/%2e%2e/llm/metrics`, which Bifrost
# resolved to its UI. Their paths are fixed, so the raw form is never needed.
_REWRITE_NORMALIZED = {"regex_uri": [f"^{PATH_PREFIX}(.*)", "$1"], "use_real_request_uri_unsafe": False}

# The raw inference paths llm-proxy serves on Bifrost.
BIFROST_PROXY_PATHS = (
    f"{PATH_PREFIX}/v1/chat/completions",
    f"{PATH_PREFIX}/v1/completions",
    f"{PATH_PREFIX}/v1/embeddings",
)

METRICS_PATH = f"{PATH_PREFIX}/metrics"

# route id -> (exact paths, methods, long timeout). Higher priority than the
# llm-proxy catch-all on LiteLLM; on Bifrost the exact paths win anyway.
CONVERTER_ROUTES: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]] = {
    "llm-messages": ((f"{PATH_PREFIX}/v1/messages",), ("POST", "OPTIONS"), True),
    "llm-responses": ((f"{PATH_PREFIX}/v1/responses",), ("POST", "OPTIONS"), True),
    # A listing returns immediately, unlike a completion: no timeout override.
    "llm-models": ((f"{PATH_PREFIX}/v1/models",), ("GET",), False),
}
CONVERTER_ROUTE_IDS = tuple(CONVERTER_ROUTES)
# On Bifrost, llm-messages also takes count_tokens, which the converter answers
# with its own estimate: Bifrost has no working one for self-hosted backends. On
# LiteLLM it stays on llm-proxy's catch-all, so LiteLLM keeps answering it.
COUNT_TOKENS_PATH = f"{PATH_PREFIX}/v1/messages/count_tokens"


def gateway_state(gateway: str | None, virtual_key: str | None) -> str:
    """What provisioning makes of LLM_GATEWAY: ``"litellm"``, ``"bifrost"`` or ``"skip"``.

    ``"skip"`` is Bifrost without a usable BIFROST_TEST_VK: every request would
    401, so the routes are left as they are (scripts/deploy-bluegreen.sh
    ``llm_gateway_state`` applies the same rule).
    """
    if (gateway or "litellm").strip().lower() != "bifrost":
        return "litellm"
    return "bifrost" if bifrost_routes.usable_virtual_key(virtual_key or "") else "skip"


def _with_paths(body: dict[str, Any], paths: tuple[str, ...]) -> dict[str, Any]:
    if len(paths) == 1:
        body["uri"] = paths[0]
    else:
        body["uris"] = list(paths)
    return body


def litellm_proxy_route(master_key: str) -> dict[str, Any]:
    """``llm-proxy`` on LiteLLM: all of /api/llm/*, with the master key injected."""
    return {
        "name": "llm-proxy",
        "uri": f"{PATH_PREFIX}/*",
        "methods": ["POST", "GET", "PUT", "DELETE", "OPTIONS"],
        "upstream_id": "litellm",
        "timeout": dict(TIMEOUT),
        "plugins": {
            "key-auth": {},
            "proxy-rewrite": {
                **_REWRITE,
                "headers": {
                    "set": {
                        "Authorization": f"Bearer {master_key}",
                        "x-litellm-end-user-id": "$consumer_name",
                    },
                },
            },
        },
        "status": 1,
    }


def bifrost_proxy_route(virtual_key: str) -> dict[str, Any]:
    """``llm-proxy`` on Bifrost: the three inference paths, with the virtual key.

    Non-OpenAI fields such as chat_template_kwargs pass through to the backend
    here (x-bf-passthrough-extra-params), as they did through LiteLLM.
    """
    headers_set = {
        **bifrost_routes.virtual_key_headers(virtual_key),
        "x-bf-passthrough-extra-params": "true",
    }
    return _with_paths(
        {
            "name": "llm-proxy",
            "desc": "OpenAI-compatible inference on Bifrost (LLM_GATEWAY=bifrost)",
            "methods": ["POST", "OPTIONS"],
            "upstream_id": "bifrost",
            "timeout": dict(TIMEOUT),
            "plugins": {
                "key-auth": {},
                "consumer-restriction": {"whitelist": [DENY_ALL_CONSUMER]},
                "proxy-rewrite": {
                    **_REWRITE_NORMALIZED,
                    "headers": {
                        "set": headers_set,
                        "remove": list(bifrost_routes.REMOVED_HEADERS),
                    },
                },
            },
            "status": 1,
        },
        BIFROST_PROXY_PATHS,
    )


def llm_admin_route() -> dict[str, Any]:
    """``/api/llm-admin/*`` → LiteLLM's admin UI and API, same-origin via the gateway."""
    return {
        "name": "llm-admin",
        "uri": "/api/llm-admin/*",
        "methods": ["POST", "GET", "PUT", "DELETE", "OPTIONS"],
        "upstream_id": "litellm",
        "plugins": {
            "key-auth": {},
            "proxy-rewrite": {
                "regex_uri": ["^/api/llm-admin(.*)", "$1"],
                "use_real_request_uri_unsafe": True,
            },
        },
        "status": 1,
    }


def litellm_metrics_route(master_key: str) -> dict[str, Any]:
    """``/api/llm/metrics`` → LiteLLM's own Prometheus exposition.

    It carves the metrics endpoint out of the llm-proxy grant, so a scraper can
    be granted monitoring access without any LLM invocation rights. The master
    key is injected even though LiteLLM serves /metrics unauthenticated here
    (litellm/config.yaml), so a release that gates it doesn't turn every scrape
    into a 401. No x-litellm-end-user-id: a scrape has no consumer semantics.
    """
    return {
        "name": "llm-metrics",
        "desc": "LiteLLM's own Prometheus /metrics exposition via the gateway",
        "uri": METRICS_PATH,
        "methods": ["GET"],
        "priority": 10,
        "upstream_id": "litellm",
        "plugins": {
            "key-auth": {},
            "consumer-restriction": {"whitelist": [DENY_ALL_CONSUMER]},
            "proxy-rewrite": {
                **_REWRITE,
                "headers": {"set": {"Authorization": f"Bearer {master_key}"}},
            },
        },
        "status": 1,
    }


def bifrost_metrics_route() -> dict[str, Any]:
    """``/api/llm/metrics`` → Bifrost's own Prometheus exposition.

    Bifrost serves /metrics without a login (client.whitelisted_routes in
    bifrost/config.json), so nothing is injected, and the client's credential
    headers are dropped like on every route to Bifrost.
    """
    return {
        "name": "llm-metrics",
        "desc": "Bifrost's own Prometheus /metrics exposition via the gateway",
        "uri": METRICS_PATH,
        "methods": ["GET"],
        "priority": 10,
        "upstream_id": "bifrost",
        "plugins": {
            "key-auth": {},
            "consumer-restriction": {"whitelist": [DENY_ALL_CONSUMER]},
            "proxy-rewrite": {
                **_REWRITE_NORMALIZED,
                "headers": {"remove": list(bifrost_routes.REMOVED_HEADERS)},
            },
        },
        "status": 1,
    }


def converter_route(
    route_id: str, *, master_key: str, virtual_key: str, on_bifrost: bool
) -> dict[str, Any]:
    """``llm-messages`` / ``llm-responses`` / ``llm-models``, for either gateway.

    Each credential that is configured goes in: the LiteLLM master key with the
    end-user attribution, and the Bifrost virtual key with its attribution. The
    converter keeps the one its LLM_GATEWAY needs and drops the other, so the
    credentials need not change at the moment the switch flips; only
    llm-messages differs, taking count_tokens on Bifrost (``COUNT_TOKENS_PATH``).
    Ships deny-all, so the route is never callable by an arbitrary key between
    this PUT and the consumer-restriction replay.
    """
    paths, methods, long_timeout = CONVERTER_ROUTES[route_id]
    if on_bifrost and route_id == "llm-messages":
        paths = (*paths, COUNT_TOKENS_PATH)
    headers_set: dict[str, str] = {}
    remove = list(bifrost_routes.REMOVED_HEADERS)
    if master_key:
        headers_set["Authorization"] = f"Bearer {master_key}"
        headers_set["x-litellm-end-user-id"] = "$consumer_name"
        remove.remove("Authorization")
    if bifrost_routes.usable_virtual_key(virtual_key):
        headers_set.update(bifrost_routes.virtual_key_headers(virtual_key))
    body: dict[str, Any] = {"name": route_id}
    if route_id == "llm-models":
        body["desc"] = "Model listing with claude/-prefixed aliases via the converter"
    body["methods"] = list(methods)
    body["priority"] = 10
    body["upstream_id"] = "llm-converter"
    if long_timeout:
        body["timeout"] = dict(TIMEOUT)
    body["plugins"] = {
        "key-auth": {},
        "consumer-restriction": {"whitelist": [DENY_ALL_CONSUMER]},
        "proxy-rewrite": {**_REWRITE, "headers": {"set": headers_set, "remove": remove}},
    }
    body["status"] = 1
    return _with_paths(body, paths)


def served_endpoints() -> list[str]:
    """``METHOD path`` for every request /api/llm forwards on Bifrost, preflights aside."""
    endpoints = [f"POST {path}" for path in BIFROST_PROXY_PATHS]
    endpoints += [
        f"{method} {path}"
        for paths, methods, _long_timeout in CONVERTER_ROUTES.values()
        for path in paths
        for method in methods
        if method != "OPTIONS"
    ]
    endpoints.insert(endpoints.index(f"POST {PATH_PREFIX}/v1/messages") + 1, f"POST {COUNT_TOKENS_PATH}")
    endpoints.append(f"GET {METRICS_PATH}")
    return endpoints


def not_found_route() -> dict[str, Any]:
    """The ``/api/llm/*`` route that answers 404 while /api/llm runs on Bifrost.

    On LiteLLM llm-proxy itself takes the whole prefix, so this route exists
    only on Bifrost. Same mechanism as ``llm-bi-not-found``
    (``bifrost_routes.explaining_404_route``).
    """
    return bifrost_routes.explaining_404_route(
        LLM_NOT_FOUND_ROUTE_ID,
        PATH_PREFIX,
        f"No {PATH_PREFIX} endpoint takes this method and path. It serves only "
        + ", ".join(served_endpoints())
        + ".",
        # Below any other /api/llm/* route: should one come back without this
        # route going (a release from before LLM_GATEWAY rolled back on top),
        # the catch-all must still win instead of APISIX picking either.
        priority=-10,
    )
