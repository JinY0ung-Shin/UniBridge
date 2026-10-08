"""The /api/llm-bi gateway routes: Bifrost, side by side with LiteLLM.

``/api/llm`` stays on LiteLLM. These routes send six exact inference paths to
Bifrost instead, three of them through a second converter (``llm-converter-bi``)
so Claude Code and Codex get the same translation on both prefixes.

Boot provisioning installs them while BIFROST_GATEWAY_ROUTES is on (the
default, since the Bifrost containers run in the default stack) and removes
them once it is off; see ``_provision_bifrost_routes`` in app/main.py, which
does the writes. In every case the ``llm-bi-not-found`` route answers the rest
of ``/api/llm-bi/`` with a 404 that says which case applies, instead of
APISIX's bare "404 Route Not Found".
"""
from __future__ import annotations

import re
from typing import Any

from app.services.apisix_system_resources import BIFROST_NOT_FOUND_ROUTE_ID
from app.services.consumer_restrictions import DENY_ALL_CONSUMER

PATH_PREFIX = "/api/llm-bi"

# Colorless shared infra (one Bifrost for both blue/green colors), like litellm.
UPSTREAMS: dict[str, dict[str, Any]] = {
    "bifrost": {
        "name": "bifrost",
        "desc": "Bifrost LLM gateway (/api/llm-bi)",
        "type": "roundrobin",
        "scheme": "http",
        "nodes": {"bifrost:8080": 1},
    },
    "llm-converter-bi": {
        "name": "llm-converter-bi",
        "desc": "llm-converter in front of Bifrost (/api/llm-bi)",
        "type": "roundrobin",
        "scheme": "http",
        "nodes": {"llm-converter-bi:4001": 1},
    },
}

# route id -> (exact paths, methods, upstream id, extra-params passthrough)
#
# Exact paths only. Bifrost serves its UI at / (and as a 200 fallback for any
# unknown extension-less path, /v1/* included), its management API under
# /api/*, and MCP under /v1/mcp/*; none of it may be reachable through the
# gateway. x-bf-passthrough-extra-params lets non-OpenAI fields such as
# chat_template_kwargs through to the backend on the raw proxy only: the
# converter routes send none, and enabling it there would forward the
# LiteLLM-only allowed_openai_params the converter attaches.
_ROUTES: dict[str, tuple[tuple[str, ...], tuple[str, ...], str, bool]] = {
    "llm-bi-proxy": (
        (
            f"{PATH_PREFIX}/v1/chat/completions",
            f"{PATH_PREFIX}/v1/completions",
            f"{PATH_PREFIX}/v1/embeddings",
        ),
        ("POST", "OPTIONS"),
        "bifrost",
        True,
    ),
    "llm-bi-messages": ((f"{PATH_PREFIX}/v1/messages",), ("POST", "OPTIONS"), "llm-converter-bi", False),
    "llm-bi-responses": ((f"{PATH_PREFIX}/v1/responses",), ("POST", "OPTIONS"), "llm-converter-bi", False),
    "llm-bi-models": ((f"{PATH_PREFIX}/v1/models",), ("GET",), "llm-converter-bi", False),
}

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

# Same budget as the /api/llm routes: LLM responses can stay silent far past
# APISIX's default 60s read timeout.
TIMEOUT = {"connect": 60, "send": 600, "read": 600}

# Bifrost swaps a config.json virtual key that lacks the sk-bf- prefix for a
# random one, so routes injecting it would 401 every request (bifrost/entrypoint.sh
# refuses to start Bifrost without it). The URL-safe alphabet, which the README's
# generator produces, is ours: scripts/deploy-bluegreen.sh finds the key in the
# whitespace-stripped JSON of a route, where a space, quote or backslash would
# never match and every deploy would re-provision. It applies the same rule.
_VIRTUAL_KEY_RE = re.compile(r"sk-bf-[A-Za-z0-9_-]{16,}")
VIRTUAL_KEY_RULE = "sk-bf- followed by at least 16 URL-safe characters (A-Z, a-z, 0-9, - and _)"


def usable_virtual_key(value: str) -> bool:
    """True for a BIFROST_TEST_VK value the routes can carry."""
    return bool(_VIRTUAL_KEY_RE.fullmatch(value or ""))


def route(route_id: str, virtual_key: str) -> dict[str, Any]:
    """Body of one of the routes, deny-all until the restriction replay grants it."""
    paths, methods, upstream_id, passthrough = _ROUTES[route_id]
    headers_set = {
        # The only credential Bifrost accepts on inference
        # (enforce_auth_on_inference); `set` also overwrites any client copy.
        "x-bf-vk": virtual_key,
        # Per-key attribution: the `consumer` Prometheus label
        # (client.prometheus_labels in bifrost/config.json) and the log
        # metadata, where a client-sent x-bf-lh-* value would otherwise win.
        "x-bf-dim-consumer": "$consumer_name",
        "x-bf-lh-consumer": "$consumer_name",
    }
    if passthrough:
        headers_set["x-bf-passthrough-extra-params"] = "true"
    body: dict[str, Any] = {
        "name": route_id,
        "desc": "Bifrost, side by side with LiteLLM",
        "methods": list(methods),
        "upstream_id": upstream_id,
        "timeout": dict(TIMEOUT),
        "plugins": {
            "key-auth": {},
            "consumer-restriction": {"whitelist": [DENY_ALL_CONSUMER]},
            "proxy-rewrite": {
                "regex_uri": [f"^{PATH_PREFIX}(.*)", "$1"],
                "use_real_request_uri_unsafe": True,
                "headers": {"set": headers_set, "remove": list(REMOVED_HEADERS)},
            },
        },
        "status": 1,
    }
    if len(paths) == 1:
        body["uri"] = paths[0]
    else:
        body["uris"] = list(paths)
    return body


def served_endpoints() -> list[str]:
    """``METHOD path`` for every request the routes forward, preflights aside."""
    return [
        f"{method} {path}"
        for paths, methods, _upstream_id, _passthrough in _ROUTES.values()
        for path in paths
        for method in methods
        if method != "OPTIONS"
    ]


def not_found_route(state: str) -> dict[str, Any]:
    """The ``/api/llm-bi/*`` route that answers 404 and says why.

    ``state`` is what provisioning did: ``"on"`` (the routes are installed),
    ``"off"`` (BIFROST_GATEWAY_ROUTES=false) or ``"no-key"`` (BIFROST_TEST_VK is
    unusable, so the routes were left as they are).

    APISIX's router prefers an exact path to a prefix whatever their priority,
    and a request whose method the exact route does not take falls through to
    the prefix. So this route gets what the routes above do not serve, or all
    of /api/llm-bi while they are switched off.

    It uses only plugins APISIX already loads: consumer-restriction keyed on
    the route id, with this route's own id blacklisted, rejects every request
    in the access phase, before anything is proxied. It therefore needs no
    upstream, and no key-auth either, which would also put its whitelist under
    the consumer-restriction reconciler.
    """
    messages = {
        "on": (
            f"No {PATH_PREFIX} endpoint takes this method and path. It serves only "
            + ", ".join(served_endpoints())
            + "."
        ),
        "off": (
            f"{PATH_PREFIX} (Bifrost) is switched off on this UniBridge "
            "(BIFROST_GATEWAY_ROUTES=false); /api/llm serves the same endpoints "
            "through LiteLLM."
        ),
        "no-key": (
            f"No {PATH_PREFIX} endpoint takes this method and path: this UniBridge "
            "cannot set up its Bifrost routes, because BIFROST_TEST_VK is not set or "
            f"is not {VIRTUAL_KEY_RULE}. /api/llm serves the same endpoints through "
            "LiteLLM."
        ),
    }
    message = messages[state]
    return {
        "name": BIFROST_NOT_FOUND_ROUTE_ID,
        "desc": f"Explains a 404 under {PATH_PREFIX}",
        "uri": f"{PATH_PREFIX}/*",
        "plugins": {
            "consumer-restriction": {
                "type": "route_id",
                "blacklist": [BIFROST_NOT_FOUND_ROUTE_ID],
                "rejected_code": 404,
                "rejected_msg": message,
            },
        },
        "status": 1,
    }
