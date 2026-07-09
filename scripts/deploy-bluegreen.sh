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
EDGE_TEMPLATE="$ROOT_DIR/deploy/edge/default.conf.template"
EDGE_CONFIG="$ROOT_DIR/deploy/edge/generated/default.conf"

INFRA_PROJECT="${UNIBRIDGE_INFRA_PROJECT:-unibridge-infra}"
EDGE_PROJECT="${UNIBRIDGE_EDGE_PROJECT:-unibridge-edge}"
NETWORK_NAME="${UNIBRIDGE_NETWORK_NAME:-unibridge-net}"
STOP_OLD_AFTER_PROMOTE="${STOP_OLD_AFTER_PROMOTE:-false}"
DRAIN_SECONDS="${DRAIN_SECONDS:-15}"
APISIX_INTERNAL_PROXY_HEADER_NAME="X-UniBridge-Internal-Proxy"

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

apisix_get() {
  local path="$1"
  local base_url
  base_url="$(admin_url)"
  curl -fsS \
    -H "X-API-KEY: $APISIX_ADMIN_KEY" \
    "$base_url/apisix/admin/$path" 2>/dev/null
}

json_contains_pair() {
  local json="$1"
  local key="$2"
  local value="$3"
  local compact
  compact="${json//[[:space:]]/}"
  [[ "$compact" == *"\"$key\":\"$value\""* ]]
}

route_has_internal_proxy_header() {
  local json="$1"
  local compact
  compact="${json//[[:space:]]/}"
  [[ "$compact" == *"\"proxy-rewrite\""* ]] || return 1
  [[ "$compact" == *"\"headers\":{\"set\":"* ]] || return 1
  [[ "$compact" == *"\"$APISIX_INTERNAL_PROXY_HEADER_NAME\":"* ]]
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

# True only when the core routes already exist in etcd and still match the
# built-in auth/header shape plus LiteLLM topology. A 404 or stale route shape
# (for example, missing the internal proxy trust header, or llm-proxy still
# pointing at an older gateway upstream) returns non-zero so the caller can force
# re-provisioning.
apisix_has_core_routes() {
  local query_route s3_route nas_route usages_route llm_proxy_route llm_admin_route messages_route responses_route litellm_upstream
  query_route="$(apisix_get "routes/query-api")" || return 1
  [[ -n "$query_route" ]] || return 1
  route_has_internal_proxy_header "$query_route" || return 1

  s3_route="$(apisix_get "routes/s3-api")" || return 1
  route_has_internal_proxy_header "$s3_route" || return 1

  nas_route="$(apisix_get "routes/nas-api")" || return 1
  route_has_internal_proxy_header "$nas_route" || return 1

  usages_route="$(apisix_get "routes/usages-api")" || return 1
  route_has_internal_proxy_header "$usages_route" || return 1

  llm_proxy_route="$(apisix_get "routes/llm-proxy")" || return 1
  json_contains_pair "$llm_proxy_route" "upstream_id" "litellm" || return 1

  llm_admin_route="$(apisix_get "routes/llm-admin")" || return 1
  json_contains_pair "$llm_admin_route" "upstream_id" "litellm" || return 1

  messages_route="$(apisix_get "routes/llm-messages")" || return 1
  json_contains_pair "$messages_route" "upstream_id" "llm-converter" || return 1

  responses_route="$(apisix_get "routes/llm-responses")" || return 1
  json_contains_pair "$responses_route" "upstream_id" "llm-converter" || return 1

  litellm_upstream="$(apisix_get "upstreams/litellm")" || return 1
  json_contains_pair "$litellm_upstream" "scheme" "https" || return 1
  [[ "${litellm_upstream//[[:space:]]/}" == *"\"litellm:4000\""* ]] || return 1
}

# Switch both APISIX upstreams (unibridge-service + llm-converter) to $color as a
# unit. The two PUTs are not transactional in APISIX, so if the second fails we
# roll the first back to $prev_color (when known) — otherwise a single transient
# admin error would leave service on the new color and the converter on the old
# (half-switched routing). Returns non-zero on failure so the caller aborts.
promote_apisix() {
  local color="$1"
  local prev_color="${2:-}"
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
        echo "WARNING: failed to roll unibridge-service back to $prev_color; APISIX upstreams may be half-switched. Re-run 'deploy-bluegreen.sh promote $prev_color' to restore." >&2
      fi
    else
      echo "WARNING: unibridge-service now points at $color but llm-converter does not, and no previous color is known to revert to — APISIX upstreams are half-switched." >&2
    fi
    return 1
  fi
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
  printf '%s\n' "$color" > "$STATE_FILE"
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

  if [[ -n "$old" && "$old" != "$target" && "$STOP_OLD_AFTER_PROMOTE" == "true" ]]; then
    echo "Waiting ${DRAIN_SECONDS}s before stopping old $old stack..."
    sleep "$DRAIN_SECONDS"
    compose_app "$old" "$(color_port "$old")" "false" stop
  fi

  echo "Active color: $target"
}

promote_color() {
  local target="$1"
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
  promote_apisix "$target" "$old"
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

  promote_color "$target"
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
