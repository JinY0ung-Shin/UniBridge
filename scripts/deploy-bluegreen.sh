#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

STATE_DIR="${BLUEGREEN_STATE_DIR:-$ROOT_DIR/.deploy}"
STATE_FILE="${BLUEGREEN_STATE_FILE:-$STATE_DIR/bluegreen-active}"
EDGE_TEMPLATE="$ROOT_DIR/deploy/edge/default.conf.template"
EDGE_CONFIG="$ROOT_DIR/deploy/edge/generated/default.conf"

INFRA_PROJECT="${UNIBRIDGE_INFRA_PROJECT:-unibridge-infra}"
EDGE_PROJECT="${UNIBRIDGE_EDGE_PROJECT:-unibridge-edge}"
NETWORK_NAME="${UNIBRIDGE_NETWORK_NAME:-unibridge-net}"
STOP_OLD_AFTER_PROMOTE="${STOP_OLD_AFTER_PROMOTE:-false}"
DRAIN_SECONDS="${DRAIN_SECONDS:-15}"

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
  sed "s/__ACTIVE_COLOR__/$color/g" "$EDGE_TEMPLATE" > "$EDGE_CONFIG"
}

admin_url() {
  local default_url="http://127.0.0.1:${APISIX_ADMIN_PORT:-9180}"
  local url="${APISIX_ADMIN_HOST_URL:-$default_url}"
  printf '%s' "${url%/}"
}

promote_apisix() {
  local color="$1"
  validate_color "$color"

  if [[ -z "${APISIX_ADMIN_KEY:-}" ]]; then
    echo "APISIX_ADMIN_KEY is required for APISIX promotion" >&2
    exit 1
  fi

  local base_url
  base_url="$(admin_url)"
  local service_body
  local converter_body
  service_body="$(printf '{"name":"unibridge-service","type":"roundrobin","nodes":{"unibridge-service-%s:8000":1}}' "$color")"
  converter_body="$(printf '{"name":"llm-converter","type":"roundrobin","scheme":"http","nodes":{"llm-converter-%s:4001":1}}' "$color")"

  curl -fsS -X PUT "$base_url/apisix/admin/upstreams/unibridge-service" \
    -H "X-API-KEY: $APISIX_ADMIN_KEY" \
    -H "Content-Type: application/json" \
    --data "$service_body" >/dev/null
  curl -fsS -X PUT "$base_url/apisix/admin/upstreams/llm-converter" \
    -H "X-API-KEY: $APISIX_ADMIN_KEY" \
    -H "Content-Type: application/json" \
    --data "$converter_body" >/dev/null
}

promote_edge() {
  local color="$1"
  validate_color "$color"

  render_edge_config "$color"
  compose_edge up -d --wait
  compose_edge exec -T edge nginx -t >/dev/null
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

  local port
  port="$(color_port "$target")"
  local provision_on_start="false"
  if [[ -z "$old" ]]; then
    provision_on_start="true"
  fi

  echo "Starting infra stack..."
  up_infra

  echo "Building and starting $target app stack on local port $port..."
  compose_app "$target" "$port" "$provision_on_start" up -d --build --wait
  wait_color "$target"

  echo "Promoting APISIX upstreams to $target..."
  promote_apisix "$target"

  echo "Promoting edge proxy to $target..."
  promote_edge "$target"
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

  wait_color "$target"
  promote_apisix "$target"
  promote_edge "$target"
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
  promote_color "$(other_color "$current")"
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
      deploy_color "${1:-}"
      ;;
    promote)
      [[ $# -eq 1 ]] || { usage >&2; exit 2; }
      promote_color "$1"
      ;;
    rollback)
      rollback
      ;;
    status)
      status
      ;;
    stop)
      [[ $# -eq 1 ]] || { usage >&2; exit 2; }
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
