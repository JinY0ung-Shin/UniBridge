#!/usr/bin/env bash
set -euo pipefail

# SECURITY: this script sources $ENV_FILE with `set -a`, exporting secrets
# (APISIX_ADMIN_KEY, DB passwords, master keys, …) into the environment and
# passing the admin key to curl. Do NOT run it with `bash -x` / xtrace in
# shared or persisted CI logs — that would echo those secret values. As a
# safeguard we disable xtrace while sourcing the env file below.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

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

STATE_DIR="${BLUEGREEN_STATE_DIR:-$ROOT_DIR/.deploy}"
STATE_FILE="${BLUEGREEN_STATE_FILE:-$STATE_DIR/bluegreen-active}"
LOCK_FILE="${BLUEGREEN_LOCK_FILE:-$STATE_DIR/bluegreen.lock}"
LLM_GATEWAY_ROLLBACK_FILE="${BLUEGREEN_LLM_GATEWAY_ROLLBACK_FILE:-$STATE_DIR/llm-gateway-rollback}"
EDGE_TEMPLATE="$ROOT_DIR/deploy/edge/default.conf.template"
EDGE_CONFIG="$ROOT_DIR/deploy/edge/generated/default.conf"

INFRA_PROJECT="${UNIBRIDGE_INFRA_PROJECT:-unibridge-infra}"
EDGE_PROJECT="${UNIBRIDGE_EDGE_PROJECT:-unibridge-edge}"
NETWORK_NAME="${UNIBRIDGE_NETWORK_NAME:-unibridge-net}"
STOP_OLD_AFTER_PROMOTE="${STOP_OLD_AFTER_PROMOTE:-false}"
DRAIN_SECONDS="${DRAIN_SECONDS:-15}"

# LLM gateway engine reconcile during promote. Blue/green promote only rotates
# the color-specific upstreams (unibridge-service, llm-converter). The LLM
# gateway engine is a singleton infra upstream that the new-color app provisions
# only when APISIX_PROVISION_ON_START=true — which is false for a new color over
# an existing one. Without this, a LiteLLM->Bifrost cutover would promote the app
# tier but leave the llm-proxy / llm-admin routes pointed at the old engine
# upstream (so /api/llm/v1/chat/completions and /v1/models keep hitting LiteLLM).
# When enabled, promote ensures the gateway upstream exists and repoints those
# routes at it via PATCH (which preserves each route's consumer-restriction and
# other fields). Set PROMOTE_LLM_GATEWAY=false to leave the routes untouched
# (e.g. rolling back to an older engine, where you'd run the old code's script).
PROMOTE_LLM_GATEWAY="${PROMOTE_LLM_GATEWAY:-true}"
APISIX_LLM_GATEWAY_UPSTREAM="${APISIX_LLM_GATEWAY_UPSTREAM:-bifrost}"
APISIX_BIFROST_NODE="${APISIX_BIFROST_NODE:-bifrost:8080}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/deploy-bluegreen.sh deploy [blue|green]
  scripts/deploy-bluegreen.sh promote <blue|green>
  scripts/deploy-bluegreen.sh rollback
  scripts/deploy-bluegreen.sh status
  scripts/deploy-bluegreen.sh stop <blue|green>

Environment:
  ENV_FILE=.env                         Environment file to load.
  BLUEGREEN_BLUE_UI_PORT=3001           Local verification port for blue UI.
  BLUEGREEN_GREEN_UI_PORT=3002          Local verification port for green UI.
  UNIBRIDGE_EDGE_PORT=3000              Public HTTPS port owned by edge proxy.
  APISIX_ADMIN_HOST_URL=http://127.0.0.1:${APISIX_ADMIN_PORT:-9180}
  STOP_OLD_AFTER_PROMOTE=false          Stop old color after promotion.
  DRAIN_SECONDS=15                      Delay before stopping old color.
  PROMOTE_LLM_GATEWAY=true              On promote, repoint llm-proxy/llm-admin
                                        at the LLM gateway upstream below.
  APISIX_LLM_GATEWAY_UPSTREAM=bifrost   Gateway upstream id to ensure/point to.
  APISIX_BIFROST_NODE=bifrost:8080      host:port for that upstream's node.
  BLUEGREEN_LLM_GATEWAY_ROLLBACK_FILE   Saved previous llm-proxy/llm-admin
                                        upstreams, used by rollback.
USAGE
}

compose_env_args() {
  if [[ -f "$ENV_FILE" ]]; then
    printf '%s\n' "--env-file" "$ENV_FILE"
  fi
}

compose_infra() {
  mapfile -t env_args < <(compose_env_args)
  docker compose "${env_args[@]}" -p "$INFRA_PROJECT" -f "$ROOT_DIR/docker-compose.infra.yml" "$@"
}

compose_edge() {
  mapfile -t env_args < <(compose_env_args)
  UNIBRIDGE_NETWORK_NAME="$NETWORK_NAME" \
    docker compose "${env_args[@]}" -p "$EDGE_PROJECT" -f "$ROOT_DIR/docker-compose.edge.yml" "$@"
}

compose_app() {
  local color="$1"
  local port="$2"
  local provision_on_start="$3"
  shift 3
  mapfile -t env_args < <(compose_env_args)
  APP_COLOR="$color" \
    UNIBRIDGE_UI_PORT="$port" \
    APISIX_PROVISION_ON_START="$provision_on_start" \
    UNIBRIDGE_NETWORK_NAME="$NETWORK_NAME" \
    docker compose "${env_args[@]}" -p "unibridge-$color" -f "$ROOT_DIR/docker-compose.app.yml" "$@"
}

validate_color() {
  case "${1:-}" in
    blue|green) ;;
    *)
      echo "color must be 'blue' or 'green'" >&2
      exit 2
      ;;
  esac
}

other_color() {
  case "$1" in
    blue) printf 'green' ;;
    green) printf 'blue' ;;
  esac
}

color_port() {
  case "$1" in
    blue) printf '%s' "${BLUEGREEN_BLUE_UI_PORT:-3001}" ;;
    green) printf '%s' "${BLUEGREEN_GREEN_UI_PORT:-3002}" ;;
  esac
}

active_color() {
  if [[ -f "$STATE_FILE" ]]; then
    tr -d '[:space:]' < "$STATE_FILE"
  fi
}

# Serialize mutating operations. Two concurrent deploys can otherwise pick the
# same target color and race on the same compose project / container names.
acquire_lock() {
  mkdir -p "$STATE_DIR"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "Another deploy-bluegreen.sh run holds the lock ($LOCK_FILE); aborting." >&2
    exit 1
  fi
}

# Both app colors mount the SAME meta volume, so they share one database file.
# SQLite is not safe for concurrent writers across containers (both colors run
# the alert checker, and a freshly-booted color runs `alembic upgrade head` on
# the shared file while the old color is still live). Refuse SQLite for
# blue/green unless the operator explicitly opts in.
require_shared_db_safe() {
  local url="${META_DB_URL:-}"
  # An unset META_DB_URL falls back to the bundled networked 'unibridge-db'
  # Postgres service (see docker-compose.app.yml/.infra.yml), which is safe for
  # blue/green — but only if its password is set, since the compose default
  # interpolates UNIBRIDGE_DB_PASSWORD into the connection URL.
  if [[ -z "$url" ]]; then
    if [[ -z "${UNIBRIDGE_DB_PASSWORD:-}" ]]; then
      echo "ERROR: META_DB_URL is unset and UNIBRIDGE_DB_PASSWORD is empty." >&2
      echo "  With no META_DB_URL the stack uses the bundled 'unibridge-db' Postgres" >&2
      echo "  service, which needs UNIBRIDGE_DB_PASSWORD. Set it, or point META_DB_URL" >&2
      echo "  at your own networked database." >&2
      exit 1
    fi
    return 0
  fi
  if [[ "$url" == sqlite* ]]; then
    if [[ "${ALLOW_SQLITE_BLUEGREEN:-false}" != "true" ]]; then
      echo "ERROR: META_DB_URL is SQLite." >&2
      echo "  Blue/green runs blue and green app stacks against the same shared meta" >&2
      echo "  volume. SQLite cannot be safely written by two containers at once, and a" >&2
      echo "  new color runs 'alembic upgrade head' on the shared file at boot." >&2
      echo "  Use a networked database (e.g. Postgres) via META_DB_URL, or set" >&2
      echo "  ALLOW_SQLITE_BLUEGREEN=true to override at your own risk." >&2
      exit 1
    fi
    echo "WARNING: ALLOW_SQLITE_BLUEGREEN=true — proceeding with a shared SQLite meta" >&2
    echo "         store. Concurrent writes from both colors may corrupt it." >&2
  fi
}

wait_url() {
  local url="$1"
  local label="$2"
  local attempts="${3:-60}"
  local delay="${4:-2}"

  for ((i = 1; i <= attempts; i++)); do
    if curl -kfsS "$url" >/dev/null; then
      return 0
    fi
    sleep "$delay"
  done

  echo "Timed out waiting for $label at $url" >&2
  return 1
}

render_edge_config() {
  local color="$1"
  validate_color "$color"
  mkdir -p "$(dirname "$EDGE_CONFIG")"
  # Guard the bind-mount directory trap: if the edge stack was ever started
  # before this config was rendered (e.g. a direct `docker compose -f
  # docker-compose.edge.yml up` instead of going through this script), Docker
  # creates a *directory* at the bind-mount path. A later `sed > "$EDGE_CONFIG"`
  # then fails with "Is a directory" and `set -e` aborts every subsequent deploy
  # until it is removed. Fail loudly with the fix instead of a cryptic error.
  if [[ -d "$EDGE_CONFIG" ]]; then
    echo "ERROR: $EDGE_CONFIG is a directory, not a file." >&2
    echo "       This usually means the edge stack was started before the config was" >&2
    echo "       rendered. Remove it and re-run the deploy:" >&2
    echo "         docker compose -p \"$EDGE_PROJECT\" -f \"$ROOT_DIR/docker-compose.edge.yml\" down" >&2
    echo "         rm -rf \"$EDGE_CONFIG\"" >&2
    exit 1
  fi
  sed "s/__ACTIVE_COLOR__/$color/g" "$EDGE_TEMPLATE" > "$EDGE_CONFIG"
}

admin_url() {
  local default_url="http://127.0.0.1:${APISIX_ADMIN_PORT:-9180}"
  local url="${APISIX_ADMIN_HOST_URL:-$default_url}"
  printf '%s' "${url%/}"
}

# PUT an APISIX admin resource with retry, so a transient admin 503/timeout in
# the middle of a multi-resource promotion does not leave colors half-switched.
apisix_put() {
  local path="$1"
  local body="$2"
  local base_url
  base_url="$(admin_url)"
  local attempts=5 delay=2 i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS -X PUT "$base_url/apisix/admin/$path" \
      -H "X-API-KEY: $APISIX_ADMIN_KEY" \
      -H "Content-Type: application/json" \
      --data "$body" >/dev/null; then
      return 0
    fi
    echo "APISIX admin PUT $path failed (attempt $i/$attempts)" >&2
    [[ $i -lt $attempts ]] && sleep "$delay"
  done
  echo "APISIX admin PUT $path failed after $attempts attempts" >&2
  return 1
}

# PATCH an APISIX admin resource with retry. Unlike PUT, this is a partial
# update: only the fields in $body change and APISIX preserves the rest of the
# resource (notably a route's consumer-restriction whitelist). Used to repoint a
# route's upstream_id without re-sending — and risking clobbering — its plugins.
apisix_patch() {
  local path="$1"
  local body="$2"
  local base_url
  base_url="$(admin_url)"
  local attempts=5 delay=2 i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS -X PATCH "$base_url/apisix/admin/$path" \
      -H "X-API-KEY: $APISIX_ADMIN_KEY" \
      -H "Content-Type: application/json" \
      --data "$body" >/dev/null; then
      return 0
    fi
    echo "APISIX admin PATCH $path failed (attempt $i/$attempts)" >&2
    [[ $i -lt $attempts ]] && sleep "$delay"
  done
  echo "APISIX admin PATCH $path failed after $attempts attempts" >&2
  return 1
}

# GET an APISIX admin resource with retry. Return codes:
#   0: success, body printed to stdout
#   1: resource is absent (HTTP 404)
#   2: admin API/curl/server error after retries
apisix_get() {
  local path="$1"
  local base_url
  base_url="$(admin_url)"
  local attempts=5 delay=2 i
  local tmp http_code curl_rc
  tmp="$(mktemp)"

  for ((i = 1; i <= attempts; i++)); do
    if http_code="$(curl -sS -o "$tmp" -w '%{http_code}' \
      -H "X-API-KEY: $APISIX_ADMIN_KEY" \
      "$base_url/apisix/admin/$path" 2>/dev/null)"; then
      case "$http_code" in
        2*)
          cat "$tmp"
          rm -f "$tmp"
          return 0
          ;;
        404)
          rm -f "$tmp"
          return 1
          ;;
        *)
          echo "APISIX admin GET $path returned HTTP $http_code (attempt $i/$attempts)" >&2
          ;;
      esac
    else
      curl_rc=$?
      echo "APISIX admin GET $path failed (curl rc=$curl_rc, attempt $i/$attempts)" >&2
    fi
    [[ $i -lt $attempts ]] && sleep "$delay"
  done

  rm -f "$tmp"
  echo "APISIX admin GET $path failed after $attempts attempts" >&2
  return 2
}

# Return codes mirror apisix_get: 0 exists, 1 absent, 2 failed to check.
apisix_route_exists() {
  apisix_get "routes/$1" >/dev/null
}

apisix_route_upstream_id() {
  local route="$1"
  local body status
  if body="$(apisix_get "routes/$route")"; then
    if printf '%s' "$body" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
value = data.get("value", data) if isinstance(data, dict) else {}
upstream = value.get("upstream_id") if isinstance(value, dict) else None
if isinstance(upstream, str) and upstream:
    print(upstream)
else:
    sys.exit(3)
'; then
      return 0
    fi
    return 3
  else
    status=$?
    return "$status"
  fi
}

# Wait until the APISIX admin API answers (any successful auth'd response),
# tolerating the warmup window right after the infra stack starts.
wait_apisix_admin() {
  local base_url
  base_url="$(admin_url)"
  local i
  for ((i = 1; i <= 60; i++)); do
    if curl -fsS -o /dev/null \
      -H "X-API-KEY: ${APISIX_ADMIN_KEY:-}" \
      "$base_url/apisix/admin/routes" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# True only when the core route already exists in etcd. A 404 (etcd reset /
# fresh APISIX) returns non-zero so the caller can force re-provisioning.
apisix_has_core_routes() {
  local base_url
  base_url="$(admin_url)"
  curl -fsS -o /dev/null \
    -H "X-API-KEY: $APISIX_ADMIN_KEY" \
    "$base_url/apisix/admin/routes/query-api" 2>/dev/null
}

# Switch both APISIX upstreams (unibridge-service + llm-converter) to $color as a
# unit. The two PUTs are not transactional in APISIX, so if the second fails we
# roll the first back to $prev_color (when known) — otherwise a single transient
# admin error would leave service on the new color and the converter on the old
# (half-switched routing). Returns non-zero on failure so the caller aborts.
promote_apisix() {
  local color="$1"
  local prev_color="${2:-}"
  local llm_gateway_mode="${3:-promote}"
  validate_color "$color"

  if [[ -z "${APISIX_ADMIN_KEY:-}" ]]; then
    echo "APISIX_ADMIN_KEY is required for APISIX promotion" >&2
    exit 1
  fi

  local service_body
  local converter_body
  service_body="$(printf '{"name":"unibridge-service","type":"roundrobin","nodes":{"unibridge-service-%s:8000":1}}' "$color")"
  converter_body="$(printf '{"name":"llm-converter","type":"roundrobin","scheme":"http","nodes":{"llm-converter-%s:4001":1}}' "$color")"

  if ! apisix_put "upstreams/unibridge-service" "$service_body"; then
    echo "APISIX promotion failed on unibridge-service upstream; no changes applied." >&2
    return 1
  fi

  if ! apisix_put "upstreams/llm-converter" "$converter_body"; then
    echo "APISIX promotion failed on llm-converter upstream." >&2
    if [[ -n "$prev_color" ]]; then
      echo "Rolling unibridge-service upstream back to $prev_color to avoid half-switched routing..." >&2
      local revert_body
      revert_body="$(printf '{"name":"unibridge-service","type":"roundrobin","nodes":{"unibridge-service-%s:8000":1}}' "$prev_color")"
      if ! apisix_put "upstreams/unibridge-service" "$revert_body"; then
        echo "FATAL: APISIX upstreams are half-switched (unibridge-service=$color, llm-converter=$prev_color) and the rollback PUT also failed. Live LLM/converter routing is inconsistent. Once the APISIX admin API is healthy, run 'deploy-bluegreen.sh promote $prev_color' to restore." >&2
      fi
    else
      echo "WARNING: unibridge-service now points at $color but llm-converter does not, and no previous color is known to revert to — APISIX upstreams are half-switched." >&2
    fi
    return 1
  fi

  # Color rotation done. Now reconcile the singleton LLM gateway engine so the
  # llm-proxy / llm-admin routes follow the engine the deployed code expects
  # (e.g. LiteLLM->Bifrost). Best-effort: a failure here does NOT undo the color
  # switch (which already succeeded), so it must not abort promote — but it is
  # surfaced loudly so the operator can finish the gateway cutover by hand.
  if ! reconcile_llm_gateway "$llm_gateway_mode"; then
    echo "WARNING: LLM gateway reconcile incomplete — /api/llm/v1/chat/completions and /v1/models may still route to the previous engine. Re-run 'scripts/deploy-bluegreen.sh promote $color' once APISIX admin is healthy, or repoint the llm-proxy/llm-admin routes manually." >&2
  fi
}

save_llm_gateway_rollback_state() {
  local gw="$1"
  mkdir -p "$STATE_DIR"

  # Merge semantics, keyed per route: record a route's CURRENT upstream the
  # first time we see it diverge from the gateway target, and NEVER overwrite an
  # already-recorded route. This preserves the original pre-cutover target
  # across re-runs of the same promote — including partial-failure retries where
  # some routes are already repointed (the old "keep the whole existing file"
  # branch dropped routes that only diverged on a later attempt). rollback
  # consumes and deletes the file, so a fresh cutover after a completed rollback
  # starts from an empty map again.
  #
  # Known limit: chaining two DISTINCT gateway cutovers without an intervening
  # rollback (A->B->C) still rolls back to A, not B, because B's routes are
  # already recorded as A. That path is intentionally out of scope — undo always
  # targets the most recent *un-rolled-back* cutover's origin.
  declare -A saved_map=()
  if [[ -s "$LLM_GATEWAY_ROLLBACK_FILE" ]]; then
    local k v
    while IFS='=' read -r k v; do
      [[ -n "$k" && -n "$v" ]] && saved_map["$k"]="$v"
    done < "$LLM_GATEWAY_ROLLBACK_FILE"
  fi

  local rc=0 changed=0 route upstream status
  for route in llm-proxy llm-admin; do
    # Already recorded — keep the original, do not overwrite.
    [[ -n "${saved_map[$route]:-}" ]] && continue
    if upstream="$(apisix_route_upstream_id "$route")"; then
      if [[ "$upstream" != "$gw" ]]; then
        saved_map["$route"]="$upstream"
        changed=1
      fi
    else
      status=$?
      if [[ "$status" == 1 ]]; then
        # Route not provisioned yet; reconcile will skip it too.
        continue
      fi
      echo "Failed to read current upstream_id for route '$route' before LLM gateway reconcile." >&2
      rc=1
    fi
  done

  if [[ "$rc" != 0 ]]; then
    return "$rc"
  fi

  if [[ "$changed" == 1 ]]; then
    local tmp="${LLM_GATEWAY_ROLLBACK_FILE}.tmp"
    : > "$tmp"
    for route in "${!saved_map[@]}"; do
      printf '%s=%s\n' "$route" "${saved_map[$route]}" >> "$tmp"
    done
    mv "$tmp" "$LLM_GATEWAY_ROLLBACK_FILE"
    echo "Saved LLM gateway rollback state to $LLM_GATEWAY_ROLLBACK_FILE." >&2
  fi
}

restore_llm_gateway_rollback_state() {
  if [[ ! -s "$LLM_GATEWAY_ROLLBACK_FILE" ]]; then
    echo "No LLM gateway rollback state found; leaving llm-proxy/llm-admin upstreams unchanged." >&2
    return 0
  fi

  local rc=0 route upstream
  while IFS='=' read -r route upstream; do
    [[ -n "$route" && -n "$upstream" ]] || continue
    if apisix_patch "routes/$route" "$(printf '{"upstream_id":"%s"}' "$upstream")"; then
      echo "Restored route '$route' to previous LLM gateway upstream '$upstream'." >&2
    else
      echo "Failed to restore route '$route' to previous LLM gateway upstream '$upstream'." >&2
      rc=1
    fi
  done < "$LLM_GATEWAY_ROLLBACK_FILE"

  if [[ "$rc" == 0 ]]; then
    rm -f "$LLM_GATEWAY_ROLLBACK_FILE"
  fi
  return "$rc"
}

# Ensure the LLM gateway upstream exists and the llm-proxy / llm-admin routes
# point at it. Idempotent, so it is safe on every promote (same-engine app
# upgrades just re-assert the existing wiring). Skipped when PROMOTE_LLM_GATEWAY
# is not "true". Routes that the app has not provisioned yet are left untouched.
reconcile_llm_gateway() {
  local mode="${1:-promote}"
  [[ "$PROMOTE_LLM_GATEWAY" == "true" ]] || return 0

  if [[ -z "${APISIX_ADMIN_KEY:-}" ]]; then
    echo "APISIX_ADMIN_KEY is required to reconcile the LLM gateway" >&2
    return 1
  fi

  if [[ "$mode" == "rollback" ]]; then
    restore_llm_gateway_rollback_state
    return $?
  fi

  local gw="$APISIX_LLM_GATEWAY_UPSTREAM"
  local up_body
  up_body="$(printf '{"name":"%s","type":"roundrobin","scheme":"http","nodes":{"%s":1}}' "$gw" "$APISIX_BIFROST_NODE")"

  if ! apisix_put "upstreams/$gw" "$up_body"; then
    echo "Failed to ensure LLM gateway upstream '$gw' (node $APISIX_BIFROST_NODE)." >&2
    return 1
  fi

  save_llm_gateway_rollback_state "$gw" || return 1

  local rc=0 route status
  for route in llm-proxy llm-admin; do
    if apisix_route_exists "$route"; then
      if apisix_patch "routes/$route" "$(printf '{"upstream_id":"%s"}' "$gw")"; then
        echo "Repointed route '$route' at LLM gateway upstream '$gw'." >&2
      else
        echo "Failed to repoint route '$route' at LLM gateway upstream '$gw'." >&2
        rc=1
      fi
    else
      status=$?
      if [[ "$status" == 1 ]]; then
        # Not provisioned yet (e.g. first deploy still booting) — nothing to repoint.
        continue
      fi
      echo "Failed to confirm whether route '$route' exists before LLM gateway reconcile." >&2
      rc=1
    fi
  done
  return $rc
}

# Render and validate the edge config for $color WITHOUT switching live traffic.
# Bringing the edge container up (or leaving it up) does not change which color
# it serves — only reload_edge applies a new config — so running `nginx -t` here
# lets a bad edge config abort the promotion BEFORE any APISIX upstream is
# flipped, keeping the two layers from desyncing on a config error.
prepare_edge() {
  local color="$1"
  validate_color "$color"

  render_edge_config "$color"
  compose_edge up -d --wait
  compose_edge exec -T edge nginx -t >/dev/null
}

# Apply the already-rendered, already-validated edge config. Call only after
# prepare_edge and promote_apisix have both succeeded.
reload_edge() {
  compose_edge exec -T edge nginx -s reload
}

write_active_color() {
  local color="$1"
  mkdir -p "$STATE_DIR"
  # Atomic write (temp + rename) so a crash mid-write can't leave a truncated or
  # empty state file that the next deploy/rollback would choke on.
  local tmp="${STATE_FILE}.tmp"
  printf '%s\n' "$color" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

wait_color() {
  local color="$1"
  local port
  port="$(color_port "$color")"

  wait_url "https://127.0.0.1:$port/healthz" "unibridge-ui-$color"
  wait_url "https://127.0.0.1:$port/_api/health" "unibridge-service-$color"
}

up_infra() {
  compose_infra up -d --wait
}

deploy_color() {
  local target="${1:-}"
  local old
  old="$(active_color || true)"
  if [[ -z "$target" ]]; then
    if [[ -n "$old" ]]; then
      target="$(other_color "$old")"
    else
      target="blue"
    fi
  fi
  validate_color "$target"
  require_shared_db_safe

  local port
  port="$(color_port "$target")"

  echo "Starting infra stack..."
  up_infra

  # Decide whether the new color should provision APISIX routes at boot.
  # First-ever deploy (no active color) always provisions. Otherwise routes
  # normally already live in shared etcd, so we skip — UNLESS etcd was reset
  # and the core routes are gone, in which case skipping would leave the system
  # with no routes (silent total outage). Detect that and force provisioning.
  local provision_on_start="false"
  if [[ -z "$old" ]]; then
    provision_on_start="true"
  elif [[ -n "${APISIX_ADMIN_KEY:-}" ]] && wait_apisix_admin && ! apisix_has_core_routes; then
    echo "WARNING: APISIX is reachable but core routes are missing (etcd reset?)." >&2
    echo "         Forcing route re-provisioning on $target." >&2
    provision_on_start="true"
  fi

  echo "Building and starting $target app stack on local port $port (provision=$provision_on_start)..."
  compose_app "$target" "$port" "$provision_on_start" up -d --build --wait
  wait_color "$target"

  # Validate the new edge config before touching APISIX, then flip APISIX
  # upstreams (with rollback on partial failure), and only switch the edge once
  # APISIX is on the new color. The active-color state file is written last, so
  # an abort anywhere above leaves both layers and the recorded state on the old
  # color (rollback stays consistent).
  echo "Validating edge proxy config for $target..."
  prepare_edge "$target"

  echo "Promoting APISIX upstreams to $target..."
  promote_apisix "$target" "$old"

  echo "Switching edge proxy to $target..."
  reload_edge
  write_active_color "$target"

  if [[ -n "$old" && "$old" != "$target" ]]; then
    if [[ "$STOP_OLD_AFTER_PROMOTE" == "true" ]]; then
      echo "Waiting ${DRAIN_SECONDS}s before stopping old $old stack..."
      sleep "$DRAIN_SECONDS"
      # 'down' (not 'stop') so 'restart: unless-stopped' cannot resurrect the
      # retired color on a host/daemon reboot. A revived old color whose baked
      # APISIX_PROVISION_ON_START is still 'true' (e.g. the first-ever color)
      # would re-provision APISIX on boot and repoint the shared
      # unibridge-service / llm-converter upstreams back at itself — silently
      # hijacking live traffic to the old version.
      compose_app "$old" "$(color_port "$old")" "false" down
    else
      # Keep the old color warm for rollback, but recreate it with provisioning
      # DISABLED to close the same reboot-hijack window. Recreates only if its
      # baked APISIX_PROVISION_ON_START actually changes (a no-op once already
      # false), and the old color serves no public traffic (edge already points
      # at $target), so this is invisible to users. Best-effort: a failure here
      # must not fail an otherwise-successful promote.
      echo "Disabling APISIX auto-provision on the retired $old stack..."
      compose_app "$old" "$(color_port "$old")" "false" up -d --no-build --wait \
        || echo "WARNING: could not recreate $old with provisioning disabled. A reboot of $old before its next deploy could re-provision APISIX and hijack traffic. Mitigate with: scripts/deploy-bluegreen.sh stop $old" >&2
    fi
  fi

  echo "Active color: $target"
}

promote_color() {
  local target="$1"
  local llm_gateway_mode="${2:-promote}"
  validate_color "$target"

  local old
  old="$(active_color || true)"

  # Ensure infra (and therefore the external apihub-net the app/edge stacks
  # attach to) is up before touching the edge: a promote can run after infra was
  # stopped or cleaned, and the edge stack would otherwise fail with
  # "network unibridge-net not found".
  echo "Ensuring infra stack is running..."
  up_infra

  wait_color "$target"
  prepare_edge "$target"
  promote_apisix "$target" "$old" "$llm_gateway_mode"
  reload_edge
  write_active_color "$target"
  echo "Active color: $target"
}

rollback() {
  local current
  current="$(active_color || true)"
  if [[ -z "$current" ]]; then
    echo "No active color state found; cannot infer rollback target." >&2
    exit 1
  fi
  # Reject a corrupted state file up front. Without this, a non-empty bad value
  # (e.g. a truncated/hand-edited state file) makes other_color return "" and
  # the compose_app call below runs with an empty APP_COLOR/port — a cryptic
  # docker error instead of the clear "color must be 'blue' or 'green'".
  validate_color "$current"

  local target
  target="$(other_color "$current")"

  # rollback brings a color's app stack back up (compose_app ... up), so it must
  # honour the same shared-SQLite guard as deploy_color — otherwise rollback is a
  # second path that can start a color against an unsafe shared meta store.
  require_shared_db_safe

  # Bring infra up first so the external apihub-net exists; otherwise the
  # compose_app call below fails attaching to a missing network.
  echo "Ensuring infra stack is running..."
  up_infra

  # The old color may have been stopped after a previous promotion
  # (STOP_OLD_AFTER_PROMOTE=true). promote_color waits on its health endpoints,
  # so bring it back up first; otherwise rollback would just time out. No
  # --build here: rollback restores the previously-deployed image as-is.
  echo "Ensuring $target stack is running before rollback..."
  compose_app "$target" "$(color_port "$target")" "false" up -d --wait

  promote_color "$target" "rollback"
}

status() {
  local current
  current="$(active_color || true)"
  echo "Active color: ${current:-none}"
  docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" \
    | grep -E '(^NAMES|unibridge-(edge|ui|service)|llm-converter)' || true
}

stop_color() {
  local color="$1"
  validate_color "$color"
  compose_app "$color" "$(color_port "$color")" "false" stop
}

main() {
  local command="${1:-}"
  shift || true

  case "$command" in
    deploy)
      acquire_lock
      deploy_color "${1:-}"
      ;;
    promote)
      [[ $# -eq 1 ]] || { usage >&2; exit 2; }
      acquire_lock
      promote_color "$1"
      ;;
    rollback)
      acquire_lock
      rollback
      ;;
    status)
      status
      ;;
    stop)
      [[ $# -eq 1 ]] || { usage >&2; exit 2; }
      acquire_lock
      stop_color "$1"
      ;;
    -h|--help|help|"")
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
