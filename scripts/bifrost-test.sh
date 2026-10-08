#!/usr/bin/env bash
set -euo pipefail

# Shows the APISIX upstreams and routes of the Bifrost side-by-side test: the
# /api/llm-bi inference paths are served by Bifrost while /api/llm stays on
# LiteLLM. See README "Bifrost side-by-side test (/api/llm-bi)".
#
# unibridge-service installs and removes them itself, following
# BIFROST_GATEWAY_ROUTES (unibridge-service/app/services/bifrost_routes.py);
# this script only reads them and says whether they match the switch.
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
  scripts/bifrost-test.sh status   Show what is installed, and whether it matches
                                   BIFROST_GATEWAY_ROUTES and BIFROST_TEST_VK.

unibridge-service installs the bifrost + llm-converter-bi upstreams, the four
llm-bi-* routes and llm-bi-not-found at boot while BIFROST_GATEWAY_ROUTES is on
(the default), and removes all but llm-bi-not-found once it is off. Change it in
.env, then deploy (blue-green) or run `docker compose up -d unibridge-service`
(single stack; a plain restart keeps the old environment).

Environment:
  ENV_FILE=.env                      Environment file to load.
  APISIX_ADMIN_HOST_URL=http://127.0.0.1:${APISIX_ADMIN_PORT:-9180}
  APISIX_ADMIN_KEY                   Required.
USAGE
}

case "${1:-}" in
  status) ;;
  up|down)
    if [[ "$1" == "up" ]]; then switch=true; else switch=false; fi
    echo "'scripts/bifrost-test.sh $1' is gone: unibridge-service installs and removes" \
      "the /api/llm-bi routes itself. Set BIFROST_GATEWAY_ROUTES=$switch in .env, then" \
      "deploy (blue-green) or run 'docker compose up -d unibridge-service' (single stack)." >&2
    exit 2
    ;;
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
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

default_admin_url="http://127.0.0.1:${APISIX_ADMIN_PORT:-9180}"
export BIFROST_TEST_ADMIN_URL="${APISIX_ADMIN_HOST_URL:-$default_admin_url}"
export APISIX_ADMIN_KEY
export BIFROST_GATEWAY_ROUTES="${BIFROST_GATEWAY_ROUTES:-true}"
export BIFROST_TEST_VK="${BIFROST_TEST_VK:-}"

exec python3 - <<'PY'
import json
import os
import re
import sys
import urllib.error
import urllib.request

ADMIN = os.environ["BIFROST_TEST_ADMIN_URL"].rstrip("/") + "/apisix/admin"
ADMIN_KEY = os.environ["APISIX_ADMIN_KEY"]
SWITCH = os.environ["BIFROST_GATEWAY_ROUTES"]
GATEWAY_VK = os.environ["BIFROST_TEST_VK"]
# Never route admin calls through HTTP(S)_PROXY: .env may set one for image
# builds, `set -a` exports it, and urllib would hand it the admin key.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Sentinel consumer that keeps a whitelist meaning "nobody"
# (unibridge-service app/services/consumer_restrictions.py DENY_ALL_CONSUMER).
DENY_ALL = "__deny_all__"
# unibridge-service app/services/apisix_system_resources.py.
UPSTREAM_IDS = ("bifrost", "llm-converter-bi")
ROUTE_IDS = ("llm-bi-proxy", "llm-bi-messages", "llm-bi-responses", "llm-bi-models")
NOT_FOUND_ROUTE_ID = "llm-bi-not-found"


def die(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def get(path):
    request = urllib.request.Request(f"{ADMIN}/{path}", headers={"X-API-KEY": ADMIN_KEY})
    try:
        with OPENER.open(request, timeout=15) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        die(f"GET {path} failed: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as exc:
        die(f"APISIX admin API unreachable at {ADMIN}: {exc.reason}")
    return payload.get("value", payload)


def whitelist_of(route):
    restriction = ((route or {}).get("plugins") or {}).get("consumer-restriction") or {}
    return [name for name in restriction.get("whitelist") or [] if isinstance(name, str)]


def injected_vk(route):
    rewrite = (route.get("plugins") or {}).get("proxy-rewrite") or {}
    return ((rewrite.get("headers") or {}).get("set") or {}).get("x-bf-vk")


for upstream_id in UPSTREAM_IDS:
    upstream = get(f"upstreams/{upstream_id}")
    if upstream is None:
        print(f"upstream {upstream_id}: absent")
    else:
        print(f"upstream {upstream_id}: {', '.join(upstream.get('nodes') or {})}")

installed = []
stale = []
for route_id in ROUTE_IDS:
    route = get(f"routes/{route_id}")
    if route is None:
        print(f"route {route_id}: absent")
        continue
    installed.append(route_id)
    expected_upstream = "bifrost" if route_id == "llm-bi-proxy" else "llm-converter-bi"
    if injected_vk(route) != GATEWAY_VK or route.get("upstream_id") != expected_upstream:
        stale.append(route_id)
    granted = [n for n in whitelist_of(route) if n != DENY_ALL]
    read_timeout = (route.get("timeout") or {}).get("read")
    paths = route.get("uris") or [route.get("uri")]
    print(
        f"route {route_id}: {', '.join(paths)} -> {route.get('upstream_id')}, "
        f"{len(granted)} consumer(s) granted, read timeout {read_timeout}s"
    )
not_found = get(f"routes/{NOT_FOUND_ROUTE_ID}")
if not_found is None:
    print(f"route {NOT_FOUND_ROUTE_ID}: absent")
else:
    restriction = (not_found.get("plugins") or {}).get("consumer-restriction") or {}
    print(f"route {NOT_FOUND_ROUTE_ID}: {not_found.get('uri')} answers 404: {restriction.get('rejected_msg')}")

# Same reading as unibridge-service (app/main.py _provision_bifrost_routes) and
# scripts/deploy-bluegreen.sh bifrost_routes_state.
print()
apply_hint = "the next deploy (blue-green) or `docker compose up -d unibridge-service` (single stack)"
if SWITCH.lower() in {"0", "f", "false", "n", "no", "off"}:
    if installed:
        print(f"BIFROST_GATEWAY_ROUTES={SWITCH}, but {len(installed)} route(s) are still installed: {apply_hint} removes them.")
    elif not_found is None:
        print(f"BIFROST_GATEWAY_ROUTES={SWITCH}, but {NOT_FOUND_ROUTE_ID} is missing: {apply_hint} installs it.")
    else:
        print(f"In step with BIFROST_GATEWAY_ROUTES={SWITCH}: /api/llm-bi answers 404, switched off.")
elif not re.fullmatch(r"sk-bf-[A-Za-z0-9_-]{16,}", GATEWAY_VK):
    explained = (
        f"{NOT_FOUND_ROUTE_ID} says why"
        if not_found is not None
        else f"{apply_hint} installs {NOT_FOUND_ROUTE_ID} to say why"
    )
    print(
        "BIFROST_TEST_VK is not set or not sk-bf- followed by at least 16 URL-safe characters "
        f"(A-Z, a-z, 0-9, - and _): unibridge-service leaves the routes as they are, and {explained}."
    )
elif len(installed) < len(ROUTE_IDS) or not_found is None:
    print(f"BIFROST_GATEWAY_ROUTES={SWITCH}, but routes are missing: {apply_hint} installs them.")
elif stale:
    print(
        f"{', '.join(stale)} inject a virtual key other than BIFROST_TEST_VK or point at another "
        f"upstream: {apply_hint} updates them. Bifrost accepts only the key its container was "
        "created with, so recreate that too after changing it."
    )
else:
    print(f"In step with BIFROST_GATEWAY_ROUTES={SWITCH}. Grant the four llm-bi-* routes to each non-master test key on the API Keys page.")
PY
