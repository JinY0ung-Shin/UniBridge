"""The /api/llm gateway routes, on Bifrost.

The route ids are the ones /api/llm had on LiteLLM, so API-key grants (the
consumer-restriction whitelists), the grants an ``llm-proxy`` grant implies and
the monitoring selectors keyed on route ids carried over when it left:

- ``llm-proxy`` is the raw OpenAI-compatible surface: only the three inference
  paths, because Bifrost answers any other extension-less path with its UI's
  index.html (200) and serves its management API under ``/api/*``;
  ``llm-not-found`` answers the rest with a 404 that lists what is served.
- ``llm-messages``, ``llm-responses`` and ``llm-models`` go to the per-color
  llm-converter, which forwards to Bifrost with the virtual key they inject.
- ``llm-metrics`` is Bifrost's own Prometheus exposition.

Boot provisioning (app/main.py ``_provision_llm_routes``) does the writes. It
also retires what LiteLLM left in APISIX (``RETIRED_LITELLM_ROUTE_ID``,
``RETIRED_LITELLM_UPSTREAM_ID``) and the /api/llm-bi routes of the Bifrost
side-by-side test (``retired_llm_bi_route``).
"""
from __future__ import annotations

import re
from typing import Any

from app.services.apisix_system_resources import LLM_BI_NOT_FOUND_ROUTE_ID, LLM_NOT_FOUND_ROUTE_ID
from app.services.consumer_restrictions import DENY_ALL_CONSUMER

PATH_PREFIX = "/api/llm"

# Colorless shared infra: one Bifrost for both blue/green colors.
BIFROST_UPSTREAM: dict[str, Any] = {
    "name": "bifrost",
    "desc": "Bifrost LLM gateway (/api/llm)",
    "type": "roundrobin",
    "scheme": "http",
    "nodes": {"bifrost:8080": 1},
}

# LLM responses can stay silent past APISIX's default 60s read timeout (long
# TTFT, reasoning, large non-stream completions); allow long reads so the
# gateway doesn't drop the socket.
TIMEOUT = {"connect": 60, "send": 600, "read": 600}

# Client headers Bifrost would read as a credential or a key selector: a
# virtual key (Authorization: Bearer / x-api-key / api-key / x-goog-api-key with
# an sk-bf- value) or a pinned stored provider key (x-bf-api-key /
# x-bf-api-key-id). APISIX has already authenticated the caller; key-auth runs
# before proxy-rewrite, so it still reads the client's own key first.
REMOVED_HEADERS = (
    "Authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "x-bf-api-key",
    "x-bf-api-key-id",
)

# Bifrost swaps a config.json virtual key that lacks the sk-bf- prefix for a
# random one, so routes injecting it would 401 every request (bifrost/entrypoint.sh
# refuses to start Bifrost without it). The URL-safe alphabet, which the README's
# generator produces, is ours: scripts/deploy-bluegreen.sh finds the key in the
# whitespace-stripped JSON of a route, where a space, quote or backslash would
# never match and every deploy would re-provision. It applies the same rule.
_VIRTUAL_KEY_RE = re.compile(r"sk-bf-[A-Za-z0-9_-]{16,}")
VIRTUAL_KEY_RULE = "sk-bf- followed by at least 16 URL-safe characters (A-Z, a-z, 0-9, - and _)"

_REWRITE = {"regex_uri": [f"^{PATH_PREFIX}(.*)", "$1"], "use_real_request_uri_unsafe": True}
# Routes straight to Bifrost rewrite the normalized path instead. APISIX picks the route on
# the normalized path, so with the raw one `/api/llm/%2e%2e/llm/metrics` matched
# llm-metrics and reached Bifrost as `/%2e%2e/llm/metrics`, which Bifrost
# resolved to its UI. Their paths are fixed, so the raw form is never needed.
_REWRITE_NORMALIZED = {"regex_uri": [f"^{PATH_PREFIX}(.*)", "$1"], "use_real_request_uri_unsafe": False}

# The raw inference paths llm-proxy serves.
BIFROST_PROXY_PATHS = (
    f"{PATH_PREFIX}/v1/chat/completions",
    f"{PATH_PREFIX}/v1/completions",
    f"{PATH_PREFIX}/v1/embeddings",
)

METRICS_PATH = f"{PATH_PREFIX}/metrics"

# route id -> (exact paths, methods, long timeout).
CONVERTER_ROUTES: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]] = {
    # count_tokens too, which the converter answers with its own estimate:
    # Bifrost has no working one for self-hosted backends.
    "llm-messages": (
        (f"{PATH_PREFIX}/v1/messages", f"{PATH_PREFIX}/v1/messages/count_tokens"),
        ("POST", "OPTIONS"),
        True,
    ),
    "llm-responses": ((f"{PATH_PREFIX}/v1/responses",), ("POST", "OPTIONS"), True),
    # A listing returns immediately, unlike a completion: no timeout override.
    "llm-models": ((f"{PATH_PREFIX}/v1/models",), ("GET",), False),
}
CONVERTER_ROUTE_IDS = tuple(CONVERTER_ROUTES)

# The prefix of the Bifrost side-by-side test, whose routes provisioning now
# deletes; ``retired_llm_bi_route`` keeps answering it with a pointer here.
LLM_BI_PREFIX = "/api/llm-bi"


def usable_virtual_key(value: str) -> bool:
    """True for a BIFROST_TEST_VK value the routes can carry."""
    return bool(_VIRTUAL_KEY_RE.fullmatch(value or ""))


def virtual_key_headers(virtual_key: str) -> dict[str, str]:
    """What every route in front of Bifrost sets."""
    return {
        # The only credential Bifrost accepts on inference
        # (enforce_auth_on_inference); `set` also overwrites any client copy.
        "x-bf-vk": virtual_key,
        # Per-key attribution: the `consumer` Prometheus label
        # (client.prometheus_labels in bifrost/config.json) and the log
        # metadata, where a client-sent x-bf-lh-* value would otherwise win.
        "x-bf-dim-consumer": "$consumer_name",
        "x-bf-lh-consumer": "$consumer_name",
    }


def _with_paths(body: dict[str, Any], paths: tuple[str, ...]) -> dict[str, Any]:
    if len(paths) == 1:
        body["uri"] = paths[0]
    else:
        body["uris"] = list(paths)
    return body


def bifrost_proxy_route(virtual_key: str) -> dict[str, Any]:
    """``llm-proxy``: the three inference paths, with the virtual key.

    Non-OpenAI fields such as chat_template_kwargs pass through to the backend
    (x-bf-passthrough-extra-params).
    """
    headers_set = {
        **virtual_key_headers(virtual_key),
        "x-bf-passthrough-extra-params": "true",
    }
    return _with_paths(
        {
            "name": "llm-proxy",
            "desc": "OpenAI-compatible inference on Bifrost",
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
                        "remove": list(REMOVED_HEADERS),
                    },
                },
            },
            "status": 1,
        },
        BIFROST_PROXY_PATHS,
    )


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
                "headers": {"remove": list(REMOVED_HEADERS)},
            },
        },
        "status": 1,
    }


def converter_route(route_id: str, virtual_key: str) -> dict[str, Any]:
    """``llm-messages`` / ``llm-responses`` / ``llm-models``, with the virtual key.

    The converter forwards the key and its attribution to Bifrost. Ships
    deny-all, so the route is never callable by an arbitrary key between this
    PUT and the consumer-restriction replay.
    """
    paths, methods, long_timeout = CONVERTER_ROUTES[route_id]
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
        "proxy-rewrite": {
            **_REWRITE,
            "headers": {"set": virtual_key_headers(virtual_key), "remove": list(REMOVED_HEADERS)},
        },
    }
    body["status"] = 1
    return _with_paths(body, paths)


def served_endpoints() -> list[str]:
    """``METHOD path`` for every request /api/llm forwards, preflights aside."""
    endpoints = [f"POST {path}" for path in BIFROST_PROXY_PATHS]
    endpoints += [
        f"{method} {path}"
        for paths, methods, _long_timeout in CONVERTER_ROUTES.values()
        for path in paths
        for method in methods
        if method != "OPTIONS"
    ]
    endpoints.append(f"GET {METRICS_PATH}")
    return endpoints


def not_found_route() -> dict[str, Any]:
    """The ``/api/llm/*`` route that answers 404 for whatever /api/llm does not serve."""
    return explaining_404_route(
        LLM_NOT_FOUND_ROUTE_ID,
        PATH_PREFIX,
        f"No {PATH_PREFIX} endpoint takes this method and path. It serves only "
        + ", ".join(served_endpoints())
        + ".",
        # Below any other /api/llm/* route: should one come back without this
        # route going (a release from before the Bifrost switch rolled back on
        # top), the catch-all must still win instead of APISIX picking either.
        priority=-10,
    )


def retired_llm_bi_route() -> dict[str, Any]:
    """The ``/api/llm-bi/*`` route left behind by the Bifrost side-by-side test.

    Its routes are gone (app/main.py ``_retire_llm_bi_routes`` deletes them on
    every provisioning boot); this 404 tells a client still configured for the
    old prefix where to go instead of APISIX's bare "404 Route Not Found".
    scripts/deploy-bluegreen.sh looks for "has been removed" in it.
    """
    return explaining_404_route(
        LLM_BI_NOT_FOUND_ROUTE_ID,
        LLM_BI_PREFIX,
        f"{LLM_BI_PREFIX} (the Bifrost side-by-side test) has been removed from "
        f"UniBridge; {PATH_PREFIX} serves the same endpoints.",
    )


def explaining_404_route(
    route_id: str, prefix: str, message: str, *, priority: int | None = None
) -> dict[str, Any]:
    """A route that answers every request under ``prefix`` with a 404 and ``message``.

    APISIX's router prefers an exact path to a prefix whatever their priority,
    and a request whose method the exact route does not take falls through to
    the prefix, so this gets whatever the other routes under ``prefix`` do not
    serve. consumer-restriction keyed on the route id, with this route's own id
    blacklisted, rejects in the access phase before anything is proxied, so the
    route needs no upstream and no key-auth (which would also put it under the
    consumer-restriction reconciler). It uses only plugins APISIX already loads.
    """
    body: dict[str, Any] = {
        "name": route_id,
        "desc": f"Explains a 404 under {prefix}",
        "uri": f"{prefix}/*",
        "plugins": {
            "consumer-restriction": {
                "type": "route_id",
                "blacklist": [route_id],
                "rejected_code": 404,
                "rejected_msg": message,
            },
        },
        "status": 1,
    }
    if priority is not None:
        body["priority"] = priority
    return body
