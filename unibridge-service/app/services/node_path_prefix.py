"""Per-node path prefixes for gateway upstreams.

An APISIX upstream balances requests across its nodes, but a node is only a
host and a port: the route rewrites the path once, before the balancer picks a
node, so every node receives the same path. Backends that serve one API under
different base paths (10.0.0.1 answers /v1/models, 10.0.0.2 answers
/api/v1/models) could therefore not share an upstream.

A node can carry ``metadata.path_prefix`` instead. The custom APISIX plugin
``unibridge-node-path-prefix`` (apisix/plugins/) puts it in front of the
forwarded path once the balancer has picked that node, and it runs from a
global rule that this module provisions. Only array-form nodes
(``[{"host": ..., "port": ..., "weight": ..., "metadata": {...}}]``) can hold
metadata; the ``{"host:port": weight}`` form never carries a prefix.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any
from urllib.parse import unquote

from fastapi import HTTPException, status
from httpx import HTTPStatusError

from app.services import apisix_client

PLUGIN_NAME = "unibridge-node-path-prefix"
GLOBAL_RULE_ID = "unibridge-node-path-prefix"
PATH_PREFIX_KEY = "path_prefix"
MAX_PATH_PREFIX_LENGTH = 256
# config.yaml alone is not enough: the plugin file is a bind mount, which only a
# new container picks up (see CLAUDE.md for the command).
RECREATE_APISIX_ADVICE = (
    "Recreate the APISIX container from the current compose files, which mount "
    "the plugin and list it in apisix/config.yaml"
)

# One or more "/segment" parts made of RFC 3986 path characters: unreserved,
# %XX escapes, sub-delims, ":" and "@". That leaves out "?", "#", whitespace and
# control characters, which would end or corrupt the forwarded path.
_PATH_PREFIX_RE = re.compile(
    r"(?:/(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})+)+"
)

# A tuple, not a set: ``scheme`` comes from the request body and may be any
# JSON value, and an unhashable one must fail validation, not raise TypeError.
_HTTP_SCHEMES = ("http", "https")


def _node_address(node: dict[str, Any]) -> str | None:
    """``host:port`` spelled the way ``apisix_client.upstream_node_addresses`` does."""
    host = node.get("host")
    if host is None:
        return None
    port = node.get("port")
    return f"{host}:{port}" if port is not None else str(host)


def _node_identity(node: dict[str, Any], scheme: Any) -> str | None:
    """The address APISIX's balancer and the plugin tell nodes apart by.

    Host case and IPv6 brackets do not matter there, and a node without a port
    gets the scheme's default, so ``x`` and ``x:80`` are the same node.
    """
    host = node.get("host")
    if not isinstance(host, str) or not host:
        return None
    port = node.get("port")
    if port is None:
        port = 443 if scheme == "https" else 80
    return f"{host.strip('[]').lower()}:{port}"


def _path_prefix_problem(value: Any) -> str | None:
    """Why ``value`` cannot be a path prefix, or None when it can."""
    if not isinstance(value, str):
        return "must be a string"
    if not value:
        return "must not be empty (leave path_prefix out for no prefix)"
    if len(value) > MAX_PATH_PREFIX_LENGTH:
        return f"must be at most {MAX_PATH_PREFIX_LENGTH} characters"
    if not value.startswith("/"):
        return 'must start with "/"'
    if value.endswith("/"):
        return 'must not end with "/"'
    segments = value[1:].split("/")
    if "" in segments:
        return 'must not contain "//"'
    if not _PATH_PREFIX_RE.fullmatch(value):
        return (
            "may only contain letters, digits, the characters "
            "- . _ ~ ! $ & ' ( ) * + , ; = : @ and %XX escapes"
        )
    # After the character check, so every escape is known to be well formed: a
    # backend that decodes "%2e" would treat that segment as "." or "..".
    if any(unquote(segment) in (".", "..") for segment in segments):
        return 'must not contain "." or ".." segments'
    return None


def node_path_prefixes(nodes: Any) -> dict[str, str]:
    """Map ``host:port`` to path prefix for the nodes that carry one."""
    prefixes: dict[str, str] = {}
    if not isinstance(nodes, list):
        return prefixes
    for node in nodes:
        if not isinstance(node, dict):
            continue
        metadata = node.get("metadata")
        address = _node_address(node)
        if not isinstance(metadata, dict) or address is None:
            continue
        prefix = metadata.get(PATH_PREFIX_KEY)
        if isinstance(prefix, str) and prefix:
            prefixes[address] = prefix
    return prefixes


def uses_node_path_prefixes(upstream: dict[str, Any]) -> bool:
    """Whether any node of ``upstream`` asks for a path prefix."""
    nodes = upstream.get("nodes")
    if not isinstance(nodes, list):
        return False
    return any(
        isinstance(node, dict)
        and isinstance(node.get("metadata"), dict)
        and PATH_PREFIX_KEY in node["metadata"]
        for node in nodes
    )


def _nodes_per_path(upstream: dict[str, Any]) -> Counter[str]:
    """How many nodes serve each path ("" is "no prefix").

    Weight-0 nodes count too: the ewma balancer ignores weights and still sends
    them traffic.
    """
    nodes = upstream.get("nodes")
    if not isinstance(nodes, list):
        return Counter()
    return Counter(
        (node["metadata"].get(PATH_PREFIX_KEY) if isinstance(node.get("metadata"), dict) else None)
        or ""
        for node in nodes
        if isinstance(node, dict)
    )


# TCP probes every second; a node is out after 2 refused connects or 3 timed-out
# ones and back after 2 good ones. The intervals must be explicit: APISIX stores
# `checks` as sent, and the health-check library's own default interval is 0,
# which never probes.
MIXED_PREFIX_CHECKS: dict[str, Any] = {
    "active": {
        "type": "tcp",
        "timeout": 1,
        "healthy": {"interval": 1, "successes": 2},
        "unhealthy": {"interval": 1, "tcp_failures": 2, "timeouts": 3},
    }
}


def apply_mixed_prefix_defaults(upstream: dict[str, Any]) -> None:
    """Fill in failover settings for an upstream whose nodes use different paths.

    A retry can only reach a node with the prefix the request was built with
    (nginx fixes the path before the first attempt); the plugin ends any other
    retry with 502, and that refused retry still uses up the next node's
    balancer turn. Measured on a live APISIX with one node down:

    - Two nodes, one per path: retries on failed 53-60 of 60 requests, retries
      off only the dead node's share (about 30), and ewma kept picking the dead
      node either way.
    - A dead node whose path a live node also serves: retries on failed 6-8 of
      60, retries off 19-21, since its retries reach that sibling.

    So TCP health checks always take a dead node out of rotation within a few
    seconds, and retries go off only when no two nodes share a path, where every
    retry would be refused anyway. ``retries`` and ``checks`` the body sets
    itself are kept. That includes values filled in here by an earlier save: a
    client that reads an upstream and sends it back keeps them even after its
    nodes stop mixing paths, while the UI, which never sends either field, gets
    them worked out again on every save. Mind that an http(s) active check
    probes every node at the same ``http_path``, without the node's prefix, so a
    node that only serves under its prefix fails it and gets no traffic; only
    an import or a raw API call can set one.
    """
    per_path = _nodes_per_path(upstream)
    if len(per_path) < 2:
        return
    if upstream.get("checks") is None:
        upstream["checks"] = copy.deepcopy(MIXED_PREFIX_CHECKS)
    if upstream.get("retries") is None and max(per_path.values()) == 1:
        upstream["retries"] = 0


def validate_upstream_node_prefixes(upstream: dict[str, Any]) -> None:
    """Raise ValueError, worded for the admin, when a node's path prefix is unusable.

    Only node ``metadata`` and the rules a prefix brings along are checked; an
    upstream without prefixes passes whatever its node form, and the rest of
    the body is left to APISIX's own schema check. ``metadata: null`` is left
    to APISIX too, since it cannot hold a prefix.
    """
    nodes = upstream.get("nodes")
    if not isinstance(nodes, list):
        return
    scheme = upstream.get("scheme")
    # identity -> the addresses as written, to name them in the error. Every
    # listed node counts, weight 0 included: APISIX and the plugin index those
    # too, so a weight-0 twin would still lend its prefix to the live node.
    spellings: dict[str, list[str]] = {}
    has_prefix = False
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        address = _node_address(node)
        identity = _node_identity(node, scheme)
        if identity is not None and address is not None:
            spellings.setdefault(identity, []).append(address)
        metadata = node.get("metadata")
        if metadata is None:
            continue
        label = address or f"#{index + 1}"
        if not isinstance(metadata, dict):
            raise ValueError(f"Node {label}: metadata must be an object.")
        if PATH_PREFIX_KEY not in metadata:
            continue
        problem = _path_prefix_problem(metadata[PATH_PREFIX_KEY])
        if problem:
            raise ValueError(f"Node {label}: path prefix {problem}.")
        has_prefix = True
    if not has_prefix:
        return

    if scheme is not None and scheme not in _HTTP_SCHEMES:
        raise ValueError(
            "Path prefixes only work on http and https upstreams; "
            f'this one uses "{scheme}".'
        )
    duplicates = sorted(names for names in spellings.values() if len(names) > 1)
    if duplicates:
        names = duplicates[0]
        if len(set(names)) == 1:
            which = f"Node {names[0]} is listed more than once."
        else:
            which = f"Nodes {' and '.join(sorted(set(names)))} are the same host and port."
        raise ValueError(
            f"{which} With path prefixes every node needs its own address: the "
            "gateway tells nodes apart by IP and port, so host names that resolve "
            "to the same IP and port count as one node as well."
        )


async def prefixed_resource_ids() -> list[str]:
    """Upstreams (and routes with an inline upstream) whose nodes set a prefix.

    Best effort, for a boot-time log line: an empty list when APISIX cannot be
    listed.
    """
    try:
        upstreams = await apisix_client.list_resources("upstreams")
        routes = await apisix_client.list_resources("routes")
    except Exception:
        return []
    ids = [
        f"upstream {item.get('id')}"
        for item in upstreams.get("items", [])
        if isinstance(item, dict) and uses_node_path_prefixes(item)
    ]
    ids += [
        f"route {item.get('id')}"
        for item in routes.get("items", [])
        if isinstance(item, dict)
        and isinstance(item.get("upstream"), dict)
        and uses_node_path_prefixes(item["upstream"])
    ]
    return sorted(ids)


async def ensure_global_rule() -> None:
    """Create or refresh the global rule that runs the plugin on every route.

    Idempotent. Errors propagate: an APISIX that does not load the plugin
    answers HTTP 400 "unknown plugin [unibridge-node-path-prefix]".
    """
    await apisix_client.put_resource(
        "global_rules", GLOBAL_RULE_ID, {"plugins": {PLUGIN_NAME: {}}}
    )


def describe_apisix_error(exc: Exception) -> str:
    """APISIX's own reason for a failed admin call, short enough for a message."""
    if isinstance(exc, HTTPStatusError):
        try:
            reason = exc.response.json().get("error_msg")
        except Exception:
            reason = None
        if isinstance(reason, str) and reason:
            return reason
        text = exc.response.text.strip()
        return text[:200] if text else f"HTTP {exc.response.status_code}"
    return str(exc) or exc.__class__.__name__


def is_unknown_plugin_error(exc: Exception) -> bool:
    """Whether APISIX refused the global rule because it does not load the plugin."""
    return (
        isinstance(exc, HTTPStatusError)
        and exc.response.status_code == 400
        and f"unknown plugin [{PLUGIN_NAME}]" in describe_apisix_error(exc)
    )


def plugin_unavailable_detail(exc: Exception, *, retry: str = "save again") -> str:
    """Error detail for a prefix that cannot take effect because the rule failed."""
    return (
        f"Per-node path prefixes need the {PLUGIN_NAME} plugin in APISIX "
        f"({describe_apisix_error(exc)}). {RECREATE_APISIX_ADVICE}, then {retry}."
    )


def rule_failure_http_error(exc: Exception, *, retry: str = "save again") -> HTTPException:
    """What a failed ``ensure_global_rule()`` means for the request that needed it.

    Only APISIX's "unknown plugin" answer means the plugin is missing and APISIX
    must be redeployed. Anything else is an ordinary gateway failure and is
    reported like other APISIX errors, so an outage never reads as "redeploy".
    """
    if is_unknown_plugin_error(exc):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=plugin_unavailable_detail(exc, retry=retry),
        )
    if isinstance(exc, HTTPStatusError):
        detail = (
            f"APISIX refused the {PLUGIN_NAME} global rule "
            f"({describe_apisix_error(exc)}); nothing was saved."
        )
    else:
        detail = f"Failed to connect to APISIX: {describe_apisix_error(exc)}"
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
