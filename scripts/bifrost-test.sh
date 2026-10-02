#!/usr/bin/env bash
set -euo pipefail

# Adds / removes the APISIX upstreams and routes for the Bifrost side-by-side
# test: the /api/llm-bi inference paths are served by Bifrost while /api/llm
# stays on LiteLLM. See README "Bifrost side-by-side test (/api/llm-bi)".
#
# SECURITY: like scripts/deploy-bluegreen.sh this sources $ENV_FILE with
# `set -a`, exporting secrets (APISIX_ADMIN_KEY, BIFROST_TEST_VK, …) into the
# environment. Do NOT run it with `bash -x` in shared logs. The admin key reaches
# APISIX only as a request header from python3 — never on a command line, and
# never through an HTTP(S)_PROXY the same .env may set for image builds.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/bifrost-test.sh up       Create/refresh the bifrost + llm-converter-bi
                                   upstreams and the four llm-bi-* routes.
  scripts/bifrost-test.sh down     Delete exactly those routes and upstreams.
  scripts/bifrost-test.sh status   Show what is installed.

Routes (all key-auth, deny-all until a key is granted them):
  llm-bi-proxy      /api/llm-bi/v1/chat/completions,
                    /api/llm-bi/v1/completions,
                    /api/llm-bi/v1/embeddings  -> bifrost          /v1/...
  llm-bi-messages   /api/llm-bi/v1/messages    -> llm-converter-bi /v1/messages
  llm-bi-responses  /api/llm-bi/v1/responses   -> llm-converter-bi /v1/responses
  llm-bi-models     /api/llm-bi/v1/models      -> llm-converter-bi /v1/models

Every route injects the gateway virtual key (x-bf-vk) Bifrost requires, so
`up` needs BIFROST_TEST_VK — the same value the bifrost container runs with.

Grant all four route ids to each non-master test key on the API Keys page; an
llm-proxy grant does NOT imply them. Master keys (`*`) are whitelisted on them
automatically by the consumer-restriction reconciler. Re-running `up` keeps the
grants. Do not edit these routes in the Gateway UI (its strip-prefix toggle
rewrites the path regex) — change this script and re-run `up` instead.

Run `up` only once both containers are healthy, and on teardown run `down`
BEFORE stopping them: the alert checker probes every APISIX upstream, so an
installed upstream whose container is down mails upstream_health alerts.

Environment:
  ENV_FILE=.env                      Environment file to load.
  APISIX_ADMIN_HOST_URL=http://127.0.0.1:${APISIX_ADMIN_PORT:-9180}
  APISIX_ADMIN_KEY                   Required.
  BIFROST_TEST_VK                    Required by `up` (sk-bf-…).
  BIFROST_TEST_NODE=bifrost:8080
  BIFROST_TEST_CONVERTER_NODE=llm-converter-bi:4001
USAGE
}

case "${1:-}" in
  up|down|status) ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if [[ -f "$ENV_FILE" ]]; then
  set -a
  _had_xtrace=0
  case $- in *x*) _had_xtrace=1; set +x ;; esac
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  [[ "$_had_xtrace" == 1 ]] && set -x
  unset _had_xtrace
  set +a
fi

if [[ -z "${APISIX_ADMIN_KEY:-}" ]]; then
  echo "APISIX_ADMIN_KEY is required (set it in $ENV_FILE or the environment)" >&2
  exit 1
fi
if [[ "$1" == "up" ]]; then
  # Same rule bifrost/entrypoint.sh enforces: Bifrost replaces a config.json
  # virtual key that lacks the sk-bf- prefix, so a mismatch would 401 everything.
  if [[ ! "${BIFROST_TEST_VK:-}" =~ ^sk-bf-.{16,}$ ]]; then
    echo "BIFROST_TEST_VK must be set to sk-bf- followed by at least 16 characters" \
      "(the value the bifrost container runs with)" >&2
    exit 1
  fi
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

default_admin_url="http://127.0.0.1:${APISIX_ADMIN_PORT:-9180}"
export BIFROST_TEST_ADMIN_URL="${APISIX_ADMIN_HOST_URL:-$default_admin_url}"
export BIFROST_TEST_NODE="${BIFROST_TEST_NODE:-bifrost:8080}"
export BIFROST_TEST_CONVERTER_NODE="${BIFROST_TEST_CONVERTER_NODE:-llm-converter-bi:4001}"
export APISIX_ADMIN_KEY
export BIFROST_TEST_VK="${BIFROST_TEST_VK:-}"

exec python3 - "$1" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

ADMIN = os.environ["BIFROST_TEST_ADMIN_URL"].rstrip("/") + "/apisix/admin"
ADMIN_KEY = os.environ["APISIX_ADMIN_KEY"]
GATEWAY_VK = os.environ.get("BIFROST_TEST_VK", "")
# Never route admin calls through HTTP(S)_PROXY: .env may set one for image
# builds, `set -a` exports it, and urllib would hand it the admin key.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Sentinel consumer that keeps a whitelist meaning "nobody"
# (unibridge-service app/services/consumer_restrictions.py DENY_ALL_CONSUMER).
DENY_ALL = "__deny_all__"
# Marks a per-route timeout override, so the admin "default route timeout"
# setting does not reset these LLM routes to its value
# (app/routers/gateway.py _TIMEOUT_OVERRIDE_LABEL / sync_default_route_timeout).
TIMEOUT_LABEL = "ub_route_timeout"
# Same budget as the /api/llm routes: LLM responses can stay silent far past
# APISIX's default 60s read timeout.
TIMEOUT = {"connect": 60, "send": 600, "read": 600}

UPSTREAMS = {
    "bifrost": {
        "name": "bifrost",
        "desc": "Bifrost LLM gateway (bifrost-test profile)",
        "type": "roundrobin",
        "scheme": "http",
        "nodes": {os.environ["BIFROST_TEST_NODE"]: 1},
    },
    "llm-converter-bi": {
        "name": "llm-converter-bi",
        "desc": "llm-converter in front of Bifrost (bifrost-test profile)",
        "type": "roundrobin",
        "scheme": "http",
        "nodes": {os.environ["BIFROST_TEST_CONVERTER_NODE"]: 1},
    },
}

# (id, uris, methods, priority, upstream_id, passthrough_extra_params)
#
# Exact paths only. Bifrost serves its UI at / (and as a 200 fallback for any
# unknown extension-less path, /v1/* included), its management API under
# /api/*, and MCP under /v1/mcp/*; none of it may be reachable through the
# gateway. x-bf-passthrough-extra-params lets non-OpenAI fields such as
# chat_template_kwargs through to the backend on the raw proxy only; the
# converter routes don't send any, and enabling it there would forward the
# LiteLLM-only allowed_openai_params the converter attaches.
ROUTES = (
    (
        "llm-bi-proxy",
        [
            "/api/llm-bi/v1/chat/completions",
            "/api/llm-bi/v1/completions",
            "/api/llm-bi/v1/embeddings",
        ],
        ["POST", "OPTIONS"],
        None,
        "bifrost",
        True,
    ),
    ("llm-bi-messages", ["/api/llm-bi/v1/messages"], ["POST", "OPTIONS"], 10, "llm-converter-bi", False),
    ("llm-bi-responses", ["/api/llm-bi/v1/responses"], ["POST", "OPTIONS"], 10, "llm-converter-bi", False),
    ("llm-bi-models", ["/api/llm-bi/v1/models"], ["GET"], 10, "llm-converter-bi", False),
)

# Client headers Bifrost would read as a credential or a key selector: a
# virtual key (Authorization: Bearer / x-api-key / api-key / x-goog-api-key with
# an sk-bf- value) or a pinned stored provider key (x-bf-api-key /
# x-bf-api-key-id). APISIX has already authenticated the caller; key-auth runs
# before proxy-rewrite, so it still reads the client's own key first.
REMOVED_HEADERS = [
    "Authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "x-bf-api-key",
    "x-bf-api-key-id",
]


def die(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{ADMIN}/{path}",
        data=data,
        method=method,
        headers={"X-API-KEY": ADMIN_KEY, "Content-Type": "application/json"},
    )
    try:
        with OPENER.open(request, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error_msg": raw.decode("utf-8", "replace")[:300]}
        return exc.code, payload
    except urllib.error.URLError as exc:
        die(f"APISIX admin API unreachable at {ADMIN}: {exc.reason}")


def error_text(payload):
    return payload.get("error_msg") or payload.get("message") or json.dumps(payload)


def get(path):
    status, payload = call("GET", path)
    if status == 404:
        return None
    if status != 200:
        die(f"GET {path} failed: HTTP {status} {error_text(payload)}")
    return payload.get("value", payload)


def put(path, body):
    status, payload = call("PUT", path, body)
    if status not in (200, 201):
        die(f"PUT {path} failed: HTTP {status} {error_text(payload)}")


def delete(path):
    status, payload = call("DELETE", path)
    if status in (200, 404):
        return status == 200
    die(f"DELETE {path} failed: HTTP {status} {error_text(payload)}")


def whitelist_of(route):
    plugins = (route or {}).get("plugins") or {}
    restriction = plugins.get("consumer-restriction") or {}
    names = [name for name in restriction.get("whitelist") or [] if isinstance(name, str)]
    return names


def route_paths(route):
    return route.get("uris") or [route.get("uri")]


def route_body(route_id, uris, methods, priority, upstream_id, passthrough, existing):
    # Keep the grants a previous `up` + the API Keys page accumulated; a new
    # route (or one whose restriction was stripped) starts as deny-all, so it is
    # never callable by an arbitrary key before the reconciler grants it.
    whitelist = whitelist_of(existing) or [DENY_ALL]
    headers_set = {
        # The only credential Bifrost accepts on inference
        # (enforce_auth_on_inference); `set` also overwrites any client copy.
        "x-bf-vk": GATEWAY_VK,
        # Per-key attribution: the `consumer` Prometheus label
        # (client.prometheus_labels in bifrost/config.json) and the log
        # metadata, where a client-sent x-bf-lh-* value would otherwise win.
        "x-bf-dim-consumer": "$consumer_name",
        "x-bf-lh-consumer": "$consumer_name",
    }
    if passthrough:
        headers_set["x-bf-passthrough-extra-params"] = "true"
    labels = dict((existing or {}).get("labels") or {})
    labels[TIMEOUT_LABEL] = "1"
    body = {
        "name": route_id,
        "desc": "Bifrost side-by-side test (scripts/bifrost-test.sh)",
        "methods": methods,
        "upstream_id": upstream_id,
        "timeout": TIMEOUT,
        "labels": labels,
        "plugins": {
            "key-auth": {},
            "consumer-restriction": {"whitelist": sorted(whitelist)},
            "proxy-rewrite": {
                "regex_uri": ["^/api/llm-bi(.*)", "$1"],
                "use_real_request_uri_unsafe": True,
                "headers": {"set": headers_set, "remove": REMOVED_HEADERS},
            },
        },
        "status": 1,
    }
    if len(uris) == 1:
        body["uri"] = uris[0]
    else:
        body["uris"] = uris
    if priority is not None:
        body["priority"] = priority
    return body


def up():
    for upstream_id, body in UPSTREAMS.items():
        existing = get(f"upstreams/{upstream_id}")
        put(f"upstreams/{upstream_id}", body)
        print(f"upstream {upstream_id} -> {', '.join(body['nodes'])}")
        if existing is not None and existing.get("desc") != body["desc"]:
            # e.g. the orphaned `bifrost` upstream an earlier (June) Bifrost
            # cutover left in etcd. Say so, since `down` will delete it too.
            print(f"  note: replaced a pre-existing upstream {upstream_id} this script did not create")
    for route_id, uris, methods, priority, upstream_id, passthrough in ROUTES:
        existing = get(f"routes/{route_id}")
        body = route_body(route_id, uris, methods, priority, upstream_id, passthrough, existing)
        put(f"routes/{route_id}", body)
        granted = [n for n in body["plugins"]["consumer-restriction"]["whitelist"] if n != DENY_ALL]
        print(f"route {route_id} {', '.join(uris)} -> {upstream_id} ({len(granted)} consumer(s) granted)")
    print(
        "Grant llm-bi-proxy, llm-bi-messages, llm-bi-responses and llm-bi-models "
        "to each non-master test key on the API Keys page."
    )


def down():
    for route_id, *_ in ROUTES:
        removed = delete(f"routes/{route_id}")
        print(f"route {route_id}: {'deleted' if removed else 'absent'}")
    for upstream_id in UPSTREAMS:
        removed = delete(f"upstreams/{upstream_id}")
        print(f"upstream {upstream_id}: {'deleted' if removed else 'absent'}")


def status():
    for upstream_id in UPSTREAMS:
        upstream = get(f"upstreams/{upstream_id}")
        if upstream is None:
            print(f"upstream {upstream_id}: absent")
        else:
            print(f"upstream {upstream_id}: {', '.join(upstream.get('nodes') or {})}")
    for route_id, *_ in ROUTES:
        route = get(f"routes/{route_id}")
        if route is None:
            print(f"route {route_id}: absent")
            continue
        granted = [n for n in whitelist_of(route) if n != DENY_ALL]
        read_timeout = (route.get("timeout") or {}).get("read")
        print(
            f"route {route_id}: {', '.join(route_paths(route))} -> {route.get('upstream_id')}, "
            f"{len(granted)} consumer(s) granted, read timeout {read_timeout}s"
        )


{"up": up, "down": down, "status": status}[sys.argv[1]]()
PY
