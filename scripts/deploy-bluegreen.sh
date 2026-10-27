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
EDGE_BIFROST_TEMPLATE="$ROOT_DIR/deploy/edge/bifrost-ui.conf.template"
EDGE_CONFIG="$ROOT_DIR/deploy/edge/generated/default.conf"
EDGE_CANDIDATE_CONFIG="$ROOT_DIR/deploy/edge/generated/candidate.conf"
EDGE_PREVIOUS_CONFIG="$ROOT_DIR/deploy/edge/generated/previous.conf"

INFRA_PROJECT="${UNIBRIDGE_INFRA_PROJECT:-unibridge-infra}"
EDGE_PROJECT="${UNIBRIDGE_EDGE_PROJECT:-unibridge-edge}"
NETWORK_NAME="${UNIBRIDGE_NETWORK_NAME:-unibridge-net}"
STOP_OLD_AFTER_PROMOTE="${STOP_OLD_AFTER_PROMOTE:-false}"
DRAIN_SECONDS="${DRAIN_SECONDS:-15}"
RECONCILE_INFRA_ON_DEPLOY="${RECONCILE_INFRA_ON_DEPLOY:-false}"
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
  RECONCILE_INFRA_ON_DEPLOY=false       Allow normal deploys to update/restart infra.
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
  EDGE_CONFIG_PATH="${EDGE_CONFIG_PATH:-$EDGE_CONFIG}" \
    UNIBRIDGE_NETWORK_NAME="$NETWORK_NAME" \
    docker compose "${env_args[@]}" -p "$EDGE_PROJECT" -f "$ROOT_DIR/docker-compose.edge.yml" "$@"
}

compose_app() {
  local color="$1"
  local port="$2"
  local provision_on_start="$3"
  local provision_route_color="$4"
  shift 4
  mapfile -t env_args < <(compose_env_args)
  APP_COLOR="$color" \
    UNIBRIDGE_UI_PORT="$port" \
    APISIX_PROVISION_ON_START="$provision_on_start" \
    APISIX_UNIBRIDGE_SERVICE_NODE="unibridge-service-${provision_route_color}:8000" \
    APISIX_LLM_CONVERTER_NODE="llm-converter-${provision_route_color}:4001" \
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

# Route repair runs inside the target service during startup. When an active
# color already exists, keep APISIX upstream nodes pinned to that known-good
# color until the target stack has passed compose --wait and wait_color. The
# later promote_apisix call is the only point allowed to switch upstreams.
provision_route_color() {
  local target="$1"
  local previous="$2"
  local provision_on_start="$3"

  if [[ "$provision_on_start" == "true" && -n "$previous" ]]; then
    printf '%s' "$previous"
  else
    printf '%s' "$target"
  fi
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
  local destination="${2:-$EDGE_CONFIG}"
  validate_color "$color"
  mkdir -p "$(dirname "$destination")"
  # Guard the bind-mount directory trap: if the edge stack was ever started
  # before this config was rendered (e.g. a direct `docker compose -f
  # docker-compose.edge.yml up` instead of going through this script), Docker
  # creates a *directory* at the bind-mount path. A later `sed > "$EDGE_CONFIG"`
  # then fails with "Is a directory" and `set -e` aborts every subsequent deploy
  # until it is removed. Fail loudly with the fix instead of a cryptic error.
  if [[ -d "$destination" ]]; then
    echo "ERROR: $destination is a directory, not a file." >&2
    echo "       This usually means the edge stack was started before the config was" >&2
    echo "       rendered. Remove it and re-run the deploy:" >&2
    echo "         docker compose -p \"$EDGE_PROJECT\" -f \"$ROOT_DIR/docker-compose.edge.yml\" down" >&2
    echo "         rm -rf \"$destination\"" >&2
    exit 1
  fi

  # deploy refuses a name that is no usable server_name up front; promote and
  # rollback warn about it, and the edge then has no server for it.
  local bifrost_host
  bifrost_host="$(bifrost_ui_hostname 2>/dev/null)" || bifrost_host=""

  local temporary="${destination}.tmp.$$"
  if ! edge_config_text "$color" "$bifrost_host" > "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  mv "$temporary" "$destination"
}

# The edge config for $color: the UniBridge server, and the BIFROST_UI_HOSTNAME
# server when a name is given.
edge_config_text() {
  local color="$1"
  local bifrost_host="$2"
  sed "s/__ACTIVE_COLOR__/$color/g" "$EDGE_TEMPLATE" || return 1
  [[ -n "$bifrost_host" ]] || return 0
  printf '\n'
  sed \
    -e "s/__ACTIVE_COLOR__/$color/g" \
    -e "s/__BIFROST_UI_HOSTNAME__/$bifrost_host/g" \
    "$EDGE_BIFROST_TEMPLATE"
}

# BIFROST_UI_HOSTNAME as the edge's server_name, in lowercase; nothing when it is
# unset. On a name that is no usable server_name it says why and fails. The
# check is unibridge-ui/entrypoint.sh's (a DNS host name, and not the name
# UniBridge itself answers on), which also keeps the value safe in sed.
bifrost_ui_hostname() {
  local LC_ALL=C
  local name="${BIFROST_UI_HOSTNAME:-}"
  [[ -n "$name" ]] || return 0
  local pattern='^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$'
  if (( ${#name} > 253 )) || [[ ! "$name" =~ $pattern ]]; then
    echo "BIFROST_UI_HOSTNAME must be a DNS host name (no IP address, port or scheme): $name" >&2
    return 1
  fi
  local host_ip="${HOST_IP:-localhost}"
  if [[ "${name,,}" == "${host_ip,,}" ]]; then
    echo "BIFROST_UI_HOSTNAME must differ from HOST_IP, the name UniBridge itself answers on: $name" >&2
    return 1
  fi
  printf '%s' "${name,,}"
}

# Whether this user can tell that $1, which -f did not find, names no file. A
# directory it may not search hides the answer (certbot keeps live/ and
# archive/ at 0700), and so does a symlink it cannot follow; Docker's daemon
# and nginx run as root and may still find the file.
known_not_a_file() {
  local path="$1"
  [[ -e "$path" ]] && return 0
  [[ -L "$path" ]] && return 1
  while [[ "$path" == */* ]]; do
    path="${path%/*}"
    [[ -n "$path" ]] || path="/"
    if [[ -e "$path" ]]; then
      [[ -d "$path" && ! -x "$path" ]] && return 1
      return 0
    fi
    [[ -L "$path" ]] && return 1
  done
  return 0
}

# A message under its level, any further lines lined up with the first.
report_problem() {
  local level="$1"
  shift
  printf '%s: %s\n' "$level" "$1" >&2
  shift
  local line
  for line in "$@"; do
    printf '%*s%s\n' "$(( ${#level} + 2 ))" '' "$line" >&2
  done
}

# BIFROST_UI_HOSTNAME and the pair the edge and the UI nginx present for it:
# TLS_BIFROST_CERT_PATH + TLS_BIFROST_KEY_PATH, or the UniBridge pair while both
# are unset (compose falls back to TLS_CERT_PATH/TLS_KEY_PATH). `deploy`
# refuses every problem before anything starts. `switch` (promote, rollback)
# only moves traffic, so it refuses just what the edge cannot start with at
# all, and only warns about the rest: a value compose takes for a volume name
# (it then refuses every command on the edge and app projects), or a path that
# names no file while the name is served (nginx cannot load it, and Docker
# leaves an empty directory in its place on the host). A path this user cannot
# look into is only a warning in either mode; prepare_edge's nginx -t decides.
check_bifrost_ui_host() {
  local mode="$1"
  local level="ERROR"
  [[ "$mode" == deploy ]] || level="WARNING"
  local fatal=0 problems=0

  local name
  if ! name="$(bifrost_ui_hostname 2>&1)"; then
    if [[ "$mode" == deploy ]]; then
      report_problem "$level" "$name"
    else
      report_problem "$level" "$name" "The edge gets no server for it."
    fi
    problems=1
    name=""
  fi

  local cert="${TLS_BIFROST_CERT_PATH:-}"
  local key="${TLS_BIFROST_KEY_PATH:-}"
  if [[ -z "$cert" && -z "$key" ]]; then
    # Files at the documented place that nothing names: the name would quietly
    # get the UniBridge certificate.
    if [[ -n "$name" ]] \
      && [[ -e "$ROOT_DIR/certs/tls-bifrost.crt" || -e "$ROOT_DIR/certs/tls-bifrost.key" ]]; then
      report_problem "$level" \
        "certs/tls-bifrost.crt or .key exists, but TLS_BIFROST_CERT_PATH and TLS_BIFROST_KEY_PATH" \
        "are unset, so $name gets the UniBridge certificate. To use the files, add to $ENV_FILE:" \
        "  TLS_BIFROST_CERT_PATH=./certs/tls-bifrost.crt" \
        "  TLS_BIFROST_KEY_PATH=./certs/tls-bifrost.key" \
        "To keep the UniBridge certificate for that name, remove the files."
      problems=1
    fi
  else
    if [[ -z "$cert" || -z "$key" ]]; then
      report_problem "$level" "Set TLS_BIFROST_CERT_PATH and TLS_BIFROST_KEY_PATH together, or neither for the UniBridge pair."
      problems=1
    fi
    # compose takes a value starting with ., / or ~ for a host path, relative to
    # the project directory (this checkout), and anything else for a volume name.
    local path source message
    for path in "$cert" "$key"; do
      message=""
      case "$path" in
        "") continue ;;
        "~" | "~/"*)
          # Without HOME compose leaves the ~ as it is, under the project.
          source="$path"
          [[ -z "${HOME:-}" ]] || source="$HOME${path:1}"
          ;;
        "~"*)
          message="Write $path (TLS_BIFROST_CERT_PATH/TLS_BIFROST_KEY_PATH) with ./, / or ~/: compose versions resolve ~user differently."
          ;;
        .* | /*) source="$path" ;;
        *)
          report_problem ERROR "compose takes $path (TLS_BIFROST_CERT_PATH/TLS_BIFROST_KEY_PATH) for a volume name: start a path with ./ or /."
          fatal=1
          continue
          ;;
      esac
      if [[ -z "$message" ]]; then
        [[ "$source" == /* ]] || source="$ROOT_DIR/$source"
        [[ -f "$source" ]] && continue
        if ! known_not_a_file "$source"; then
          report_problem WARNING "Cannot check $source (TLS_BIFROST_CERT_PATH/TLS_BIFROST_KEY_PATH) as $(id -un); nginx -t decides."
          continue
        fi
        message="$source (TLS_BIFROST_CERT_PATH/TLS_BIFROST_KEY_PATH) is not a file; Docker would mount an empty directory in its place."
      fi
      if [[ -n "$name" ]]; then
        report_problem ERROR "$message"
        fatal=1
      else
        report_problem "$level" "$message"
        problems=1
      fi
    done
  fi

  if (( fatal )) || [[ "$mode" == deploy && "$problems" == 1 ]]; then
    exit 1
  fi
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

# A route to Bifrost itself must rewrite the normalized path
# (app/services/llm_routes.py): with the raw one, a dot-segment such as %2e%2e
# matches a route on the normalized path but reaches Bifrost unresolved.
route_rewrites_normalized_path() {
  local compact="${1//[[:space:]]/}"
  [[ "$compact" == *'"use_real_request_uri_unsafe":false'* ]]
}

# True only when APISIX answers 404 for $path: an admin API that fails any
# other way proves nothing about absence.
apisix_absent() {
  local path="$1"
  local base_url status
  base_url="$(admin_url)"
  status="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "X-API-KEY: $APISIX_ADMIN_KEY" \
    "$base_url/apisix/admin/$path" 2>/dev/null)" || return 1
  [[ "$status" == "404" ]]
}

# The rule boot provisioning applies to BIFROST_TEST_VK (unibridge-service
# app/services/llm_routes.py usable_virtual_key): URL-safe only, since the route
# checks below string-match it in route JSON. Without a usable key the service
# leaves the /api/llm routes as they are.
bifrost_vk_usable() {
  local LC_ALL=C
  [[ "${BIFROST_TEST_VK:-}" =~ ^sk-bf-[A-Za-z0-9_-]{16,}$ ]]
}

# LiteLLM support was removed: Bifrost is the only gateway behind /api/llm and
# nothing this checkout builds reads LLM_GATEWAY (compose passes a literal
# bifrost, for an older image recreated on rollback). A .env that still names
# litellm, or anything else, asks for a gateway this release cannot run, so say
# so before touching anything. A BIFROST_TEST_VK the service would not use is
# refused too: the Bifrost routes inject it, so the new color would leave the
# /api/llm routes as they are, on a key that may no longer be Bifrost's.
require_usable_llm_gateway() {
  case "${LLM_GATEWAY:-}" in
    ""|bifrost) ;;
    *)
      echo "ERROR: $ENV_FILE sets LLM_GATEWAY='${LLM_GATEWAY}', but LiteLLM support was removed and" >&2
      echo "       Bifrost is the only gateway behind /api/llm. Delete the LLM_GATEWAY line." >&2
      exit 1
      ;;
  esac
  if ! bifrost_vk_usable; then
    echo "ERROR: /api/llm needs a usable BIFROST_TEST_VK (sk-bf- and 16 or more of A-Z a-z 0-9 _ -)," >&2
    echo "       the virtual key the bifrost container runs with." >&2
    exit 1
  fi
}

# /api/llm-bi (the Bifrost side-by-side test) is gone: boot provisioning
# (unibridge-service app/main.py _retire_llm_bi_routes) deletes its four routes
# and the llm-converter-bi upstream and leaves llm-bi-not-found behind, saying
# so. A host that still has the old routes, or the old "switched off" 404,
# therefore re-provisions on its first deploy after upgrading.
apisix_llm_bi_retired() {
  local not_found_route compact route_id
  not_found_route="$(apisix_get "routes/llm-bi-not-found")" || return 1
  json_contains_pair "$not_found_route" "uri" "/api/llm-bi/*" || return 1
  compact="${not_found_route//[[:space:]]/}"
  [[ "$compact" == *"hasbeenremoved"* ]] || return 1
  for route_id in llm-bi-proxy llm-bi-messages llm-bi-responses llm-bi-models; do
    apisix_absent "routes/$route_id" || return 1
  done
  apisix_absent "upstreams/llm-converter-bi" || return 1
}

# A rotated BIFROST_TEST_VK reaches the /api/llm routes through the deploy,
# but the bifrost container keeps the value it was
# created with until it is recreated, and refuses every request carrying another
# (401). Say so: deploys leave infra alone, so recreating it is the operator's
# step. Compared in-shell so the key never lands on a command line.
warn_bifrost_vk_drift() {
  local container_id container_env line env_flag=""
  container_id="$(compose_infra ps -q bifrost 2>/dev/null | head -n1)" || return 0
  [[ -n "$container_id" ]] || return 0
  container_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
    "$container_id" 2>/dev/null)" || return 0
  while IFS= read -r line; do
    if [[ "$line" == "BIFROST_TEST_VK=$BIFROST_TEST_VK" ]]; then
      return 0
    fi
  done <<<"$container_env"
  if [[ -f "$ENV_FILE" ]]; then
    env_flag=" --env-file \"$ENV_FILE\""
  fi
  echo "WARNING: the bifrost container runs with another BIFROST_TEST_VK than $ENV_FILE." >&2
  echo "         The Bifrost routes inject the $ENV_FILE value, which Bifrost refuses" >&2
  echo "         (401) until the container is recreated:" >&2
  echo "           docker compose$env_flag -p \"$INFRA_PROJECT\" -f \"$ROOT_DIR/docker-compose.infra.yml\" up -d --wait bifrost" >&2
}

route_allows_method() {
  local json="$1"
  local method="$2"
  local compact methods
  compact="${json//[[:space:]]/}"
  methods="${compact#*\"methods\":[}"
  [[ "$methods" != "$compact" ]] || return 1
  methods="${methods%%]*}"
  [[ ",$methods," == *",\"$method\","* ]]
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
# built-in auth/header shape, the /api/llm routes match Bifrost and the
# /api/llm-bi routes are retired. A 404 or stale route shape (for example, missing
# the internal proxy trust header, or llm-proxy still pointing at an older
# gateway upstream) returns non-zero so the caller can force re-provisioning.
apisix_has_core_routes() {
  local query_route query_template_write_route s3_route nas_route usages_route mcp_route prometheus_route
  query_route="$(apisix_get "routes/query-api")" || return 1
  [[ -n "$query_route" ]] || return 1
  route_has_internal_proxy_header "$query_route" || return 1

  query_template_write_route="$(apisix_get "routes/query-template-write-api")" || return 1
  route_has_internal_proxy_header "$query_template_write_route" || return 1
  route_allows_method "$query_template_write_route" "PUT" || return 1
  route_allows_method "$query_template_write_route" "PATCH" || return 1
  route_allows_method "$query_template_write_route" "DELETE" || return 1
  json_contains_pair "$query_template_write_route" "uri" "/api/query/templates/*" || return 1
  json_contains_pair "$query_template_write_route" "upstream_id" "unibridge-service" || return 1

  s3_route="$(apisix_get "routes/s3-api")" || return 1
  route_has_internal_proxy_header "$s3_route" || return 1

  nas_route="$(apisix_get "routes/nas-api")" || return 1
  route_has_internal_proxy_header "$nas_route" || return 1

  usages_route="$(apisix_get "routes/usages-api")" || return 1
  route_has_internal_proxy_header "$usages_route" || return 1

  mcp_route="$(apisix_get "routes/mcp-api")" || return 1
  route_has_internal_proxy_header "$mcp_route" || return 1

  # Upstream is Prometheus, not this app, so there is no internal-proxy header
  # to check — but the route must exist, or steady-state deploys (which boot
  # with provisioning off) would never create it after this feature landed.
  prometheus_route="$(apisix_get "routes/prometheus-api")" || return 1
  json_contains_pair "$prometheus_route" "uri" "/api/prometheus/*" || return 1
  json_contains_pair "$prometheus_route" "upstream_id" "prometheus" || return 1

  apisix_llm_routes_match || return 1

  apisix_llm_bi_retired || return 1
}

# The /api/llm routes point at Bifrost under the route ids they had on LiteLLM
# (grants carry over): llm-proxy and llm-metrics go to Bifrost, the converter
# routes to llm-converter, every one of them injects the current
# BIFROST_TEST_VK, llm-not-found answers the rest of the prefix and LiteLLM's
# llm-admin is gone. So a rotated key, or a host still carrying LiteLLM's
# routes, re-provisions.
apisix_llm_routes_match() {
  # ASCII-only matching, and fast over a large route listing.
  local LC_ALL=C
  local route routes services route_id bifrost_upstream

  for route_id in llm-messages llm-responses llm-models; do
    route="$(apisix_get "routes/$route_id")" || return 1
    json_contains_pair "$route" "upstream_id" "llm-converter" || return 1
    json_contains_pair "$route" "x-bf-vk" "$BIFROST_TEST_VK" || return 1
  done
  route="$(apisix_get "routes/llm-models")" || return 1
  json_contains_pair "$route" "uri" "/api/llm/v1/models" || return 1

  route="$(apisix_get "routes/llm-proxy")" || return 1
  json_contains_pair "$route" "upstream_id" "bifrost" || return 1
  json_contains_pair "$route" "x-bf-vk" "$BIFROST_TEST_VK" || return 1
  route_rewrites_normalized_path "$route" || return 1

  route="$(apisix_get "routes/llm-metrics")" || return 1
  json_contains_pair "$route" "uri" "/api/llm/metrics" || return 1
  json_contains_pair "$route" "upstream_id" "bifrost" || return 1
  route_rewrites_normalized_path "$route" || return 1

  route="$(apisix_get "routes/llm-not-found")" || return 1
  json_contains_pair "$route" "uri" "/api/llm/*" || return 1

  # Boot provisioning deletes LiteLLM's admin route, and its upstream with it
  # unless a route or service of someone else's still uses it. Re-provisioning
  # removes an upstream left behind once nothing uses it.
  apisix_absent "routes/llm-admin" || return 1
  if ! apisix_absent "upstreams/litellm"; then
    routes="$(apisix_get "routes")" || return 1
    services="$(apisix_get "services")" || services=""
    [[ "${routes//[[:space:]]/}${services//[[:space:]]/}" == *'"upstream_id":"litellm"'* ]] || return 1
  fi

  bifrost_upstream="$(apisix_get "upstreams/bifrost")" || return 1
  [[ "${bifrost_upstream//[[:space:]]/}" == *"\"bifrost:8080\""* ]] || return 1
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

# Render and validate the edge config for $color WITHOUT touching the live
# bind-mounted config or container. `compose up` is intentionally forbidden in
# this phase: Compose may recreate edge when its service definition changes,
# which would apply a newly rendered live config before APISIX is promoted.
prepare_edge() {
  local color="$1"
  validate_color "$color"

  render_edge_config "$color" "$EDGE_CANDIDATE_CONFIG"
  EDGE_CONFIG_PATH="$EDGE_CANDIDATE_CONFIG" \
    compose_edge run --rm --no-deps edge nginx -t >/dev/null
}

# Restore the config that was active before switch_edge. This is a best-effort
# recovery path used when edge fails to start, validate, or reload after APISIX
# was promoted.
restore_previous_edge() {
  local had_previous="$1"

  if [[ "$had_previous" != "true" ]]; then
    compose_edge down >/dev/null 2>&1 || true
    rm -f "$EDGE_CONFIG" || true
    return 0
  fi

  if ! cp "$EDGE_PREVIOUS_CONFIG" "$EDGE_CONFIG"; then
    echo "WARNING: failed to restore the previous edge config file." >&2
    return 1
  fi
  if ! compose_edge up -d --wait; then
    echo "WARNING: failed to restart edge with its previous config." >&2
    return 1
  fi
  if ! compose_edge exec -T edge nginx -t >/dev/null; then
    echo "WARNING: restored edge config failed nginx validation." >&2
    return 1
  fi
  if ! compose_edge exec -T edge nginx -s reload >/dev/null; then
    echo "WARNING: failed to reload the restored edge config." >&2
    return 1
  fi
}

# Apply the already-rendered candidate only after APISIX promotion. Copying over
# the existing file preserves the inode used by Docker's file bind mount, so a
# running edge sees the new contents on reload. `compose up` is also delayed to
# this phase; if Compose must recreate edge, it can no longer switch traffic
# ahead of APISIX.
switch_edge() {
  if [[ ! -f "$EDGE_CANDIDATE_CONFIG" ]]; then
    echo "ERROR: validated edge candidate is missing: $EDGE_CANDIDATE_CONFIG" >&2
    return 1
  fi
  if [[ -d "$EDGE_CONFIG" ]]; then
    echo "ERROR: live edge config path is a directory: $EDGE_CONFIG" >&2
    return 1
  fi

  local had_previous="false"
  if ! rm -f "$EDGE_PREVIOUS_CONFIG"; then
    echo "ERROR: could not clear stale edge backup: $EDGE_PREVIOUS_CONFIG" >&2
    return 1
  fi
  if [[ -f "$EDGE_CONFIG" ]]; then
    if ! cp "$EDGE_CONFIG" "$EDGE_PREVIOUS_CONFIG"; then
      echo "ERROR: could not back up the live edge config." >&2
      return 1
    fi
    had_previous="true"
  fi

  if ! cp "$EDGE_CANDIDATE_CONFIG" "$EDGE_CONFIG"; then
    echo "ERROR: could not install the validated edge candidate." >&2
    return 1
  fi
  if ! compose_edge up -d --wait \
    || ! compose_edge exec -T edge nginx -t >/dev/null \
    || ! compose_edge exec -T edge nginx -s reload >/dev/null; then
    echo "ERROR: edge switch failed; restoring the previous edge config." >&2
    restore_previous_edge "$had_previous" || true
    return 1
  fi

  rm -f "$EDGE_CANDIDATE_CONFIG" || true
  return 0
}

restore_apisix_after_edge_failure() {
  local target="$1"
  local previous="$2"

  if [[ -z "$previous" || "$previous" == "$target" ]]; then
    return 0
  fi

  echo "Rolling APISIX upstreams back to $previous after edge switch failure..." >&2
  if ! promote_apisix "$previous" "$target"; then
    echo "WARNING: APISIX rollback to $previous failed; inspect upstreams before retrying." >&2
    return 1
  fi
}

write_active_color() {
  local color="$1"
  if ! mkdir -p "$STATE_DIR"; then
    return 1
  fi
  local temporary="${STATE_FILE}.tmp.$$"
  if ! printf '%s\n' "$color" > "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  if ! mv "$temporary" "$STATE_FILE"; then
    rm -f "$temporary"
    return 1
  fi
}

commit_active_color() {
  local target="$1"
  local previous="$2"

  if write_active_color "$target"; then
    rm -f "$EDGE_PREVIOUS_CONFIG" "$EDGE_CANDIDATE_CONFIG" || true
    return 0
  fi

  echo "ERROR: failed to record active color; rolling routing back to ${previous:-none}." >&2
  local had_previous="false"
  [[ -f "$EDGE_PREVIOUS_CONFIG" ]] && had_previous="true"
  restore_previous_edge "$had_previous" || true
  restore_apisix_after_edge_failure "$target" "$previous" || true
  return 1
}

wait_color() {
  local color="$1"
  local port
  port="$(color_port "$color")"

  wait_url "https://127.0.0.1:$port/healthz" "unibridge-ui-$color"
  wait_url "https://127.0.0.1:$port/_api/health" "unibridge-service-$color"
}

color_is_healthy() {
  local color="$1"
  local port
  validate_color "$color"
  port="$(color_port "$color")"

  local container status
  for container in \
    "unibridge-ui-$color" \
    "unibridge-service-$color" \
    "llm-converter-$color"; do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] || return 1
  done

  curl -kfsS --max-time 5 "https://127.0.0.1:$port/healthz" >/dev/null 2>&1 \
    && curl -kfsS --max-time 5 "https://127.0.0.1:$port/_api/health" >/dev/null 2>&1
}

# True only when the color's service container already has boot-time APISIX
# provisioning switched off. Returns non-zero when it is armed, when the
# container is gone, or when inspect fails — callers then fall through to the
# recreate, i.e. fail toward disarming.
standby_is_disarmed() {
  local color="$1"
  # Not validate_color: the caller's $old comes from the state file and is not
  # pre-validated, and this runs after a successful promotion — a bad value must
  # fall through to the (harmless, warned) recreate, not exit the script.
  case "$color" in
    blue|green) ;;
    *) return 1 ;;
  esac

  local container_env
  container_env="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
    "unibridge-service-$color" 2>/dev/null)" || return 1
  grep -qxF 'APISIX_PROVISION_ON_START=false' <<<"$container_env"
}

ensure_external_volume() {
  local volume="$1"
  if ! docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Creating shared app volume: $volume"
    docker volume create "$volume" >/dev/null
  fi
}

ensure_shared_app_volumes() {
  ensure_external_volume "${UNIBRIDGE_DATA_VOLUME:-unibridge_unibridge-data}"
  ensure_external_volume "${LLM_CONVERTER_STATE_VOLUME:-unibridge_llm-converter-state}"
}

up_infra() {
  compose_infra up -d --wait
}

require_existing_infra_healthy() {
  if ! docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    echo "ERROR: required Docker network is missing: $NETWORK_NAME" >&2
    echo "       Run with RECONCILE_INFRA_ON_DEPLOY=true to create/reconcile infra." >&2
    return 1
  fi

  local services=()
  mapfile -t services < <(compose_infra config --services)
  if [[ "${#services[@]}" -eq 0 ]]; then
    echo "ERROR: infra Compose file contains no services." >&2
    return 1
  fi

  local service container_id state
  for service in "${services[@]}"; do
    # -a: one-shot bootstraps (restart: "no", e.g. etcd-init) exit as soon as
    # they finish and drop off a plain `ps -q`, but their container still
    # exists and its exit code is the health signal.
    container_id="$(compose_infra ps -aq "$service" | head -n1)"
    if [[ -z "$container_id" ]]; then
      echo "ERROR: infra service is not running: $service" >&2
      echo "       Active traffic was not changed. Run with" >&2
      echo "       RECONCILE_INFRA_ON_DEPLOY=true after reviewing restart impact." >&2
      return 1
    fi

    state="$(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.HostConfig.RestartPolicy.Name}}|{{.State.ExitCode}}' "$container_id" 2>/dev/null || true)"
    case "$state" in
      running\|healthy\|*|running\|none\|*) ;;
      # A one-shot service (restart: "no") that exited 0 is in its healthy
      # state: its dependents already gated on service_completed_successfully.
      exited\|none\|no\|0|exited\|none\|\|0) ;;
      *)
        echo "ERROR: infra service is not ready: $service ($state)" >&2
        echo "       Active traffic was not changed. Run with" >&2
        echo "       RECONCILE_INFRA_ON_DEPLOY=true after reviewing restart impact." >&2
        return 1
        ;;
    esac
  done
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
  require_usable_llm_gateway
  check_bifrost_ui_host deploy

  local port
  port="$(color_port "$target")"

  if [[ -z "$old" || "$RECONCILE_INFRA_ON_DEPLOY" == "true" ]]; then
    echo "Starting/reconciling infra stack..."
    up_infra
  else
    echo "Verifying existing infra stack without recreating it..."
    require_existing_infra_healthy
  fi
  warn_bifrost_vk_drift
  ensure_shared_app_volumes

  # Decide whether the new color should provision APISIX routes at boot.
  # First-ever deploy (no active color) always provisions. Otherwise routes
  # normally already live in shared etcd, so we skip — UNLESS etcd was reset
  # and the core routes are gone, in which case skipping would leave the system
  # with no routes (silent total outage). Detect that and force provisioning.
  local provision_on_start="false"
  if [[ -z "$old" ]]; then
    provision_on_start="true"
  elif [[ -n "${APISIX_ADMIN_KEY:-}" ]] && wait_apisix_admin && ! apisix_has_core_routes; then
    echo "WARNING: APISIX is reachable but core routes are missing or out of date" >&2
    echo "         (etcd reset, a release with new routes, or a rotated" >&2
    echo "         BIFROST_TEST_VK)." >&2
    echo "         Forcing route re-provisioning on $target." >&2
    provision_on_start="true"
  fi

  local route_color
  route_color="$(provision_route_color "$target" "$old" "$provision_on_start")"
  if [[ "$provision_on_start" == "true" && "$route_color" != "$target" ]]; then
    echo "         Keeping APISIX upstreams on active $route_color until $target is healthy." >&2
  fi

  echo "Building and starting $target app stack on local port $port (provision=$provision_on_start)..."
  compose_app "$target" "$port" "$provision_on_start" "$route_color" up -d --build --wait
  wait_color "$target"

  # Validate a detached edge candidate before touching APISIX, then flip APISIX
  # upstreams and only apply the candidate once APISIX is on the new color. The
  # active-color state file is written last. If the edge switch fails, both the
  # edge config and APISIX are restored to the previous color before aborting.
  echo "Validating edge proxy config for $target..."
  prepare_edge "$target"

  echo "Promoting APISIX upstreams to $target..."
  promote_apisix "$target" "$old"

  echo "Switching edge proxy to $target..."
  if ! switch_edge; then
    restore_apisix_after_edge_failure "$target" "$old" || true
    return 1
  fi
  commit_active_color "$target" "$old"

  if [[ -n "$old" && "$old" != "$target" ]]; then
    if [[ "$STOP_OLD_AFTER_PROMOTE" == "true" ]]; then
      echo "Waiting ${DRAIN_SECONDS}s before stopping old $old stack..."
      sleep "$DRAIN_SECONDS"
      compose_app "$old" "$(color_port "$old")" "false" "$old" stop
    else
      # Disarm boot-time APISIX provisioning on the standby we are keeping alive.
      #
      # promote_apisix is the ONLY intended writer of the upstream -> color
      # mapping, but APISIX_PROVISION_ON_START / APISIX_*_NODE are baked into a
      # container's environment when it is *created*, and the app stack runs with
      # `restart: unless-stopped` — so a host reboot, dockerd restart or OOM
      # restart re-runs the lifespan of a container that was created armed (the
      # first-ever deploy, or the etcd-reset path above). Two layers stop that
      # from rolling traffic back:
      #   1. the service refuses to overwrite a color-pinned upstream that
      #      already points at another color (app/main.py
      #      ::_put_upstream_if_unclaimed), so an armed restart can no longer
      #      demote the active color — this is also what covers the now-ACTIVE
      #      $target when the etcd-reset path armed it pinned to $old;
      #   2. this recreate clears the armed env so the standby stops re-trying
      #      (and warning) on every boot.
      # Only recreate when the env is actually armed: a standby left over from a
      # previous deploy was already created with provision=false, and recreating
      # it would re-run the OLD image's lifespan — including `alembic upgrade
      # head`, which cannot know a migration that shipped in the release we just
      # promoted, leaving the rollback target in a boot-crash loop. The ACTIVE
      # color is never recreated here for the same reason, and because it serves
      # live traffic; layer 1 is its only guard by design.
      # provision_route_color is the now-active $target so that even a
      # pathological forced provisioning could only point forward, never
      # backward. --no-build reuses the old color's already-built image (never
      # rebuild the old checkout); --wait makes a bad recreate visible instead
      # of silent.
      if standby_is_disarmed "$old"; then
        echo "Standby $old already disarmed (APISIX_PROVISION_ON_START=false) — skipping recreate."
      else
        echo "Disarming APISIX boot provisioning on standby $old stack..."
        if ! compose_app "$old" "$(color_port "$old")" "false" "$target" up -d --no-build --wait; then
          echo "WARNING: could not recreate standby $old with APISIX_PROVISION_ON_START=false." >&2
          echo "         The promotion to $target SUCCEEDED and is left in place, but $old keeps a" >&2
          echo "         stale APISIX_PROVISION_ON_START=true in its container env, so it re-runs" >&2
          echo "         provisioning on every restart (host reboot, dockerd restart, OOM)." >&2
          echo "         It can no longer pull traffic back to $old — the service refuses to" >&2
          echo "         overwrite an upstream pointing at another color — but it will keep" >&2
          echo "         rewriting routes and logging skip warnings until the env is cleared." >&2
          echo "         Clear it by re-running this deploy, or by taking the standby down:" >&2
          echo "           scripts/deploy-bluegreen.sh stop $old" >&2
        fi
      fi
    fi
  fi

  echo "Active color: $target"
}

promote_color() {
  local target="$1"
  validate_color "$target"
  check_bifrost_ui_host switch

  local old
  old="$(active_color || true)"

  # Promotion is routing-only. Do not reconcile or recreate infra here: an
  # emergency promotion/rollback must not restart shared single-instance
  # services. A healthy target already proves its required infra is reachable.
  wait_color "$target"
  prepare_edge "$target"
  promote_apisix "$target" "$old"
  if ! switch_edge; then
    restore_apisix_after_edge_failure "$target" "$old" || true
    return 1
  fi
  commit_active_color "$target" "$old"
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
  # produces a cryptic health-check error instead of the clear color error.
  validate_color "$current"

  local target
  target="$(other_color "$current")"

  # Never recreate an inactive color during an emergency rollback. The shared
  # database may already be at a revision the old image does not know; `compose
  # up` would then replace a usable old process with a restart loop. Rollback is
  # deliberately a routing-only operation to an already-running healthy color.
  if ! color_is_healthy "$target"; then
    echo "ERROR: inactive $target stack is not already healthy; rollback was not attempted." >&2
    echo "       Refusing to recreate it because the shared database may have advanced" >&2
    echo "       beyond that image's Alembic revisions. Active $current remains unchanged." >&2
    echo "       Build and validate a compatible checkout with 'deploy $target', or" >&2
    echo "       restore a matching database/image under a planned recovery procedure." >&2
    return 1
  fi

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
  compose_app "$color" "$(color_port "$color")" "false" "$color" stop
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

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
