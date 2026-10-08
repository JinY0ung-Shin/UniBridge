#!/usr/bin/env bash
set -euo pipefail

# Live check for the unibridge-node-path-prefix APISIX plugin
# (apisix/plugins/unibridge-node-path-prefix.lua).
#
# Starts a throwaway etcd, an APISIX built from this repo's apisix/config.yaml,
# entrypoint and plugin (images taken from docker-compose.infra.yml, so it is
# the version production runs) and two echo backends on a private docker
# network. It then sends requests through APISIX and checks the path each
# backend received. Everything it starts is removed on exit. Needs docker and
# python3; touches no running UniBridge stack.
#
# Run it after changing the plugin and after every APISIX upgrade: the plugin
# leans on APISIX internals (see the comment at the top of the plugin).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN=unibridge-node-path-prefix
TAG="npp-check-$$"
ADMIN_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
WORK_DIR=""

cleanup() {
  docker rm -f "$TAG-apisix" "$TAG-etcd" "$TAG-echo-a" "$TAG-echo-b" "$TAG-echo-m1" "$TAG-echo-m2" >/dev/null 2>&1 || true
  docker network rm "$TAG" >/dev/null 2>&1 || true
  if [[ -n "$WORK_DIR" ]]; then
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

image_of() {
  sed -n "s|^ *image: *\\($1[^ ]*\\).*|\\1|p" "$ROOT_DIR/docker-compose.infra.yml" | head -n 1
}
APISIX_IMAGE="$(image_of apache/apisix)"
ETCD_IMAGE="$(image_of quay.io/coreos/etcd)"
ECHO_IMAGE=nginx:alpine
[[ -n "$APISIX_IMAGE" && -n "$ETCD_IMAGE" ]] || { echo "could not read images from docker-compose.infra.yml" >&2; exit 1; }
WORK_DIR="$(mktemp -d)"

# apisix/config.yaml only lets 127.0.0.1 and 172.16.0.0/12 reach the Admin API,
# and the host reaches a published port from the network's gateway address, so
# the network has to sit inside 172.16.0.0/12.
network_ok=""
for third in 251 252 253 254; do
  if docker network create --subnet "172.31.$third.0/24" "$TAG" >/dev/null 2>&1; then
    network_ok=1
    break
  fi
done
[[ -n "$network_ok" ]] || { echo "no free 172.31.25x.0/24 subnet for the test network" >&2; exit 1; }

for name in echo-a echo-b; do
  cat > "$WORK_DIR/$name.conf" <<CONF
server {
    listen 8080;
    location / {
        default_type application/json;
        return 200 '{"server":"$name","path":"\$request_uri"}';
    }
}
CONF
  docker run -d --name "$TAG-$name" --network "$TAG" --network-alias "$name" \
    -v "$WORK_DIR/$name.conf:/etc/nginx/conf.d/default.conf:ro" "$ECHO_IMAGE" >/dev/null
done

for name in echo-m1 echo-m2; do
  cat > "$WORK_DIR/$name.conf" <<CONF
server {
    listen 8080;
    location / {
        default_type application/json;
        return 200 '{"server":"$name","path":"\$request_uri"}';
    }
}
CONF
  docker run -d --name "$TAG-$name" --network "$TAG" --network-alias echo-multi \
    -v "$WORK_DIR/$name.conf:/etc/nginx/conf.d/default.conf:ro" "$ECHO_IMAGE" >/dev/null
done

docker run -d --name "$TAG-etcd" --network "$TAG" --network-alias etcd \
  -e ETCD_NAME=etcd -e ETCD_DATA_DIR=/tmp/etcd \
  -e ETCD_ADVERTISE_CLIENT_URLS=http://etcd:2379 -e ETCD_LISTEN_CLIENT_URLS=http://0.0.0.0:2379 \
  -e ETCD_INITIAL_CLUSTER=etcd=http://etcd:2380 -e ETCD_INITIAL_ADVERTISE_PEER_URLS=http://etcd:2380 \
  -e ETCD_LISTEN_PEER_URLS=http://0.0.0.0:2380 -e ETCD_INITIAL_CLUSTER_STATE=new \
  "$ETCD_IMAGE" >/dev/null

docker run -d --name "$TAG-apisix" --network "$TAG" \
  -p 127.0.0.1::9080 -p 127.0.0.1::9180 \
  -e APISIX_ADMIN_KEY="$ADMIN_KEY" -e ETCD_USERNAME=root -e ETCD_PASSWORD= \
  --entrypoint /opt/apisix-config/docker-entrypoint.sh \
  -v "$ROOT_DIR/apisix/config.yaml:/opt/apisix-config/config.yaml:ro" \
  -v "$ROOT_DIR/apisix/docker-entrypoint.sh:/opt/apisix-config/docker-entrypoint.sh:ro" \
  -v "$ROOT_DIR/apisix/plugins/$PLUGIN.lua:/usr/local/apisix/apisix/plugins/$PLUGIN.lua:ro" \
  "$APISIX_IMAGE" >/dev/null

GATEWAY_PORT="$(docker port "$TAG-apisix" 9080/tcp | head -n 1 | sed 's/.*://')"
ADMIN_PORT="$(docker port "$TAG-apisix" 9180/tcp | head -n 1 | sed 's/.*://')"

echo "APISIX $APISIX_IMAGE on 127.0.0.1:$GATEWAY_PORT (admin $ADMIN_PORT); waiting for it..."
GATEWAY_PORT="$GATEWAY_PORT" ADMIN_PORT="$ADMIN_PORT" ADMIN_KEY="$ADMIN_KEY" PLUGIN="$PLUGIN" \
  python3 - <<'PY'
import collections
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

GATEWAY_PORT = int(os.environ["GATEWAY_PORT"])
ADMIN = f"http://127.0.0.1:{os.environ['ADMIN_PORT']}/apisix/admin"
KEY = os.environ["ADMIN_KEY"]
PLUGIN = os.environ["PLUGIN"]
failures = []


def admin(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{ADMIN}/{path}", data=data, method=method,
        headers={"X-API-KEY": KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except OSError as exc:
        return 0, str(exc)


def get(target):
    """GET with the request target sent exactly as written."""
    connection = http.client.HTTPConnection("127.0.0.1", GATEWAY_PORT, timeout=15)
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        body = response.read().decode()
    finally:
        connection.close()
    if response.status != 200:
        return response.status, None, None
    payload = json.loads(body)
    return 200, payload["server"], payload["path"]


def sample(target, count=20):
    return collections.Counter(get(target) for _ in range(count))


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n      -> {detail}"))
    if not ok:
        failures.append(name)


def strip(prefix):
    return {"proxy-rewrite": {"regex_uri": [f"^{prefix}(.*)", "$1"], "use_real_request_uri_unsafe": True}}


def setup(name, nodes, uri, plugins=None):
    status, body = admin("PUT", f"upstreams/{name}", {"type": "roundrobin", "pass_host": "pass", "nodes": nodes})
    assert status in (200, 201), (name, status, body)
    route = {"uri": uri, "upstream_id": name}
    if plugins:
        route["plugins"] = plugins
    status, body = admin("PUT", f"routes/{name}", route)
    assert status in (200, 201), (name, status, body)
    time.sleep(1.5)  # let APISIX pick the change up from etcd


for _ in range(120):
    if admin("GET", "routes")[0] == 200:
        break
    time.sleep(1)
else:
    sys.exit("APISIX admin API never came up")

status, body = admin("PUT", f"global_rules/{PLUGIN}", {"plugins": {PLUGIN: {}}})
check("APISIX loads the plugin (global rule accepted)", status in (200, 201), f"{status} {body}")

A = {"host": "echo-a", "port": 8080, "weight": 1}
B_API = {"host": "echo-b", "port": 8080, "weight": 1, "metadata": {"path_prefix": "/api"}}
DEAD_B_API = {"host": "echo-b", "port": 9999, "weight": 1, "metadata": {"path_prefix": "/api"}}
DEAD_A = {"host": "echo-a", "port": 9999, "weight": 1}
REFUSED = (502, None, None)

setup("lb", [A, B_API], "/api/lb/*", strip("/api/lb"))
seen = sample("/api/lb/v1/models?x=1&y=%2F")
check("load balancing: each node gets its own path, query kept",
      set(seen) == {(200, "echo-a", "/v1/models?x=1&y=%2F"), (200, "echo-b", "/api/v1/models?x=1&y=%2F")}, seen)
seen = sample("/api/lb/v1/a%2Fb%20c", 10)
check("encoded path bytes kept",
      set(seen) == {(200, "echo-a", "/v1/a%2Fb%20c"), (200, "echo-b", "/api/v1/a%2Fb%20c")}, seen)

setup("raw", [A, B_API], "/api/raw/*")
seen = sample("/api/raw/v1/models?q=1")
check("route without proxy-rewrite: prefix goes in front of the request target",
      set(seen) == {(200, "echo-a", "/api/raw/v1/models?q=1"), (200, "echo-b", "/api/api/raw/v1/models?q=1")}, seen)

setup("dict", {"echo-a:8080": 1}, "/api/dict/*", strip("/api/dict"))
check("dict-form nodes unchanged", set(sample("/api/dict/v1/models", 5)) == {(200, "echo-a", "/v1/models")})

setup("retry1", [A, DEAD_B_API], "/api/retry1/*", strip("/api/retry1"))
seen = sample("/api/retry1/v1/models")
check("a failed /api node is not retried on a no-prefix node (502, never /api at echo-a)",
      set(seen) <= {(200, "echo-a", "/v1/models"), REFUSED} and REFUSED in seen, seen)

setup("retry2", [DEAD_A, B_API], "/api/retry2/*", strip("/api/retry2"))
seen = sample("/api/retry2/v1/models")
check("a failed no-prefix node is not retried on an /api node (502, never bare path at echo-b)",
      set(seen) <= {(200, "echo-b", "/api/v1/models"), REFUSED} and REFUSED in seen, seen)

setup("retry3", [DEAD_B_API, B_API], "/api/retry3/*", strip("/api/retry3"))
check("nodes sharing a prefix still fail over",
      set(sample("/api/retry3/v1/models")) == {(200, "echo-b", "/api/v1/models")})

setup("bad", [A, {**B_API, "metadata": {"path_prefix": "/a\r\nX-Injected: 1"}}], "/api/bad/*", strip("/api/bad"))
seen = sample("/api/bad/v1/models")
check("an invalid prefix written straight to the Admin API takes its node out instead of reaching the request line",
      set(seen) <= {(200, "echo-a", "/v1/models"), REFUSED} and REFUSED in seen, seen)

setup("multi", [{"host": "echo-multi", "port": 8080, "weight": 1, "metadata": {"path_prefix": "/api"}}],
      "/api/multi/*", strip("/api/multi"))
seen = sample("/api/multi/v1/models", 40)
check("a node whose name has several addresses keeps its prefix on each of them",
      {path for _, _, path in seen} == {"/api/v1/models"}, seen)

setup("lb", [A, {**B_API, "metadata": {"path_prefix": "/v2"}}], "/api/lb/*", strip("/api/lb"))
check("an updated prefix applies without a restart",
      set(sample("/api/lb/v1/models")) == {(200, "echo-a", "/v1/models"), (200, "echo-b", "/v2/v1/models")})

print()
print(f"{len(failures)} check(s) failed" if failures else "all checks passed")
sys.exit(1 if failures else 0)
PY
