#!/usr/bin/env bash
# Shared helpers for UniBridge backup scripts.
#
# Two deployment layouts are supported and detected at runtime:
#   single    - one stack from docker-compose.yml (compose project "unibridge").
#   bluegreen - scripts/deploy-bluegreen.sh: shared infra (etcd, apisix,
#               keycloak(-db), litellm(-db), unibridge-db, prometheus) in project
#               "unibridge-infra" from docker-compose.infra.yml, plus one app
#               stack per color ("unibridge-blue" / "unibridge-green") from
#               docker-compose.app.yml.
# Compose finds containers by project label, so every exec/cp/ps/stop must be
# aimed at the right project. Use infra_compose/app_compose/color_compose rather
# than compose() directly; they degrade to plain compose() in single mode.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SNAPSHOTS_ROOT="${SNAPSHOTS_ROOT:-$PROJECT_ROOT/snapshots}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

# Blue-green layout (mirrors scripts/deploy-bluegreen.sh).
INFRA_PROJECT="${UNIBRIDGE_INFRA_PROJECT:-unibridge-infra}"
BLUEGREEN_STATE_FILE="${BLUEGREEN_STATE_FILE:-$PROJECT_ROOT/.deploy/bluegreen-active}"
APP_PROJECT_PREFIX="unibridge-"
APP_COLORS=(blue green)

log()  { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }
die()  { printf '[%s] ERROR: %s\n' "$(date -Iseconds)" "$*" >&2; exit 1; }

compose() {
  (cd "$PROJECT_ROOT" && docker compose "$@")
}

# Detected stack mode, cached for the life of the process. Guarded with :- so
# re-sourcing this file (every lib does) keeps an already-detected value.
_BACKUP_STACK_MODE="${_BACKUP_STACK_MODE:-}"

# Container IDs (running or stopped) in the blue-green infra project. Empty when
# that project was never created — or when compose cannot evaluate the file at
# all, which is treated the same on purpose: detection must never abort a run.
_infra_project_containers() {
  (cd "$PROJECT_ROOT" && docker compose -p "$INFRA_PROJECT" \
    -f docker-compose.infra.yml ps -aq 2>/dev/null) || true
}

# Populate $_BACKUP_STACK_MODE. Kept separate from backup_stack_mode() so
# callers can warm the cache in the *current* shell — a `$(backup_stack_mode)`
# runs in a subshell and its cache write would be discarded.
_detect_stack_mode() {
  [[ -n "$_BACKUP_STACK_MODE" ]] && return 0

  local mode
  case "${BACKUP_STACK:-}" in
    single|bluegreen) mode="$BACKUP_STACK" ;;
    "")
      if [[ -f "$BLUEGREEN_STATE_FILE" ]] || [[ -n "$(_infra_project_containers)" ]]; then
        mode="bluegreen"
      else
        mode="single"
      fi
      ;;
    *) die "BACKUP_STACK must be 'single' or 'bluegreen', got: '${BACKUP_STACK}'" ;;
  esac

  _BACKUP_STACK_MODE="$mode"
  # stdout of backup_stack_mode() is the mode itself, so this notice goes to stderr.
  log "stack mode: $mode" >&2
}

# Echo "single" or "bluegreen".
backup_stack_mode() {
  _detect_stack_mode
  printf '%s\n' "$_BACKUP_STACK_MODE"
}

# Predicate form. Prefer this inside the libs: unlike "$(backup_stack_mode)" it
# does not fork, so the detection result stays cached in the calling shell.
stack_is_bluegreen() {
  _detect_stack_mode
  [[ "$_BACKUP_STACK_MODE" == "bluegreen" ]]
}

# Compose against the shared infra stack (databases, etcd, apisix, keycloak,
# litellm). In single mode this is the one and only stack.
infra_compose() {
  if stack_is_bluegreen; then
    (cd "$PROJECT_ROOT" && docker compose -p "$INFRA_PROJECT" \
      -f docker-compose.infra.yml "$@")
  else
    compose "$@"
  fi
}

# Host port docker-compose.app.yml publishes for a color's UI container.
# Mirrors scripts/deploy-bluegreen.sh::color_port so a later `up` cannot
# recreate a container with a different published port than the deploy gave it.
color_ui_port() {
  case "$1" in
    blue)  printf '%s' "${BLUEGREEN_BLUE_UI_PORT:-3001}" ;;
    green) printf '%s' "${BLUEGREEN_GREEN_UI_PORT:-3002}" ;;
    *)     die "unknown app color: $1" ;;
  esac
}

# Compose against one app color's project. APP_COLOR and UNIBRIDGE_UI_PORT are
# `${VAR:?}` in docker-compose.app.yml, so they must be set even for read-only
# commands like `ps` or interpolation fails before compose does anything.
color_compose() {
  local color="$1"
  shift
  local port
  port="$(color_ui_port "$color")"
  (cd "$PROJECT_ROOT" && APP_COLOR="$color" UNIBRIDGE_UI_PORT="$port" \
    docker compose -p "${APP_PROJECT_PREFIX}${color}" -f docker-compose.app.yml "$@")
}

# Compose against the app tier: the active color under blue-green, the single
# stack otherwise.
app_compose() {
  if stack_is_bluegreen; then
    # Assigned on its own line: if resolving the color dies, set -e stops here
    # instead of calling color_compose with an empty color.
    local color
    color="$(bluegreen_active_color)"
    color_compose "$color" "$@"
  else
    compose "$@"
  fi
}

# App-tier services run once per color under blue-green. Everything else in the
# stack is a single shared instance living in the infra project.
is_app_tier_service() {
  case "$1" in
    unibridge-service|llm-converter|unibridge-ui) return 0 ;;
    *) return 1 ;;
  esac
}

# Colors whose <service> has a RUNNING container, one per line. Lets a restore
# put back exactly what it took down instead of starting a color the operator
# deliberately left stopped.
app_service_colors_running() {
  local service="$1"
  local color cid
  for color in "${APP_COLORS[@]}"; do
    # No `| head`: under pipefail a reader closing the pipe early makes the
    # whole substitution fail, and a color that IS running would look stopped.
    cid="$(color_compose "$color" ps -q "$service" 2>/dev/null)" || true
    if [[ -n "$cid" ]]; then
      printf '%s\n' "$color"
    fi
  done
}

# "blue, green" for a newline-separated color list; empty string for none.
format_color_list() {
  local line out=""
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    out+="${out:+, }$line"
  done <<< "${1:-}"
  printf '%s' "$out"
}

# Active app color under blue-green, from the state file scripts/deploy-bluegreen.sh
# writes. If that file is gone, fall back to the single color that actually has a
# unibridge-service container — anything more ambiguous is the operator's call.
bluegreen_active_color() {
  local color
  if [[ -f "$BLUEGREEN_STATE_FILE" ]]; then
    color="$(tr -d '[:space:]' < "$BLUEGREEN_STATE_FILE")" || color=""
    [[ "$color" =~ ^(blue|green)$ ]] || \
      die "invalid active color '$color' in $BLUEGREEN_STATE_FILE (expected 'blue' or 'green')"
    printf '%s' "$color"
    return
  fi

  local candidates=()
  local candidate cid
  for candidate in "${APP_COLORS[@]}"; do
    cid="$(color_compose "$candidate" ps -aq unibridge-service 2>/dev/null)" || true
    if [[ -n "$cid" ]]; then
      candidates+=("$candidate")
    fi
  done

  [[ "${#candidates[@]}" -eq 1 ]] || \
    die "cannot determine the active app color: $BLUEGREEN_STATE_FILE does not exist and ${#candidates[@]} colors have a unibridge-service container (need exactly 1). Write the active color to that file, or point BLUEGREEN_STATE_FILE at the right one."
  printf '%s' "${candidates[0]}"
}

load_env() {
  local env_file="$PROJECT_ROOT/.env"
  [[ -f "$env_file" ]] || die ".env not found at $env_file"
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
}

sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}

size_of() {
  stat -c '%s' "$1"
}

# Read a single file's recorded SHA256 from manifest.json.
# Uses jq if available, python3 otherwise. Env-var passing avoids injection.
manifest_sha256_of() {
  local manifest="$1"
  local name="$2"
  if command -v jq >/dev/null 2>&1; then
    jq -er --arg n "$name" '.files[] | select(.name==$n) | .sha256' "$manifest"
  elif command -v python3 >/dev/null 2>&1; then
    MANIFEST_PATH="$manifest" NEEDED_NAME="$name" python3 -c '
import json, os, sys
with open(os.environ["MANIFEST_PATH"]) as f:
    m = json.load(f)
for e in m["files"]:
    if e["name"] == os.environ["NEEDED_NAME"]:
        print(e["sha256"]); sys.exit(0)
sys.exit(1)
'
  else
    die "need jq or python3 on host to verify manifest integrity"
  fi
}

verify_sha256() {
  local file="$1"
  local expected="$2"
  local actual
  actual="$(sha256_of "$file")"
  [[ "$actual" == "$expected" ]] || \
    die "SHA256 mismatch on $file (expected $expected, got $actual) — backup may be corrupted or truncated"
}

# Fail fast on bad operator-supplied env values.
validate_env_vars() {
  [[ "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]] || \
    die "RETENTION_DAYS must be a positive integer, got: '$RETENTION_DAYS'"
  [[ -n "$SNAPSHOTS_ROOT" ]] || die "SNAPSHOTS_ROOT cannot be empty"
  [[ "$SNAPSHOTS_ROOT" != "/" ]] || die "SNAPSHOTS_ROOT cannot be /"
}

# Resolve the actual docker volume name backing <service>'s mount at <dest>.
# Must be called while the service's container exists (running or stopped).
# Never guesses from $PROJECT_ROOT's basename - that breaks when operators
# set COMPOSE_PROJECT_NAME or use -p. Looks in the infra stack: every service
# with a volume worth backing up lives there under blue-green, and infra_compose
# is plain compose() in single mode.
resolve_volume() {
  local service="$1"
  local mount_dest="$2"

  local cid
  cid="$(infra_compose ps -aq "$service" 2>/dev/null | head -1)" || true
  if [[ -z "$cid" ]]; then
    if stack_is_bluegreen; then
      die "cannot resolve volume for '$service': no container exists (run 'docker compose -p $INFRA_PROJECT -f docker-compose.infra.yml up -d' first)"
    fi
    die "cannot resolve volume for '$service': no container exists (run 'docker compose up' first)"
  fi

  local name
  name="$(docker inspect -f \
    "{{range .Mounts}}{{if eq .Destination \"${mount_dest}\"}}{{.Name}}{{end}}{{end}}" \
    "$cid")"
  [[ -n "$name" ]] || die "cannot resolve volume for '$service' at '$mount_dest'"
  printf '%s' "$name"
}

# Acquire a single-instance lock for the duration of the script.
# Prevents cron overlap from stomping on partial backups.
acquire_lock() {
  local lock_file="${1:-$PROJECT_ROOT/.backup.lock}"
  exec 9>"$lock_file" || die "cannot open lock file: $lock_file"
  flock -n 9 || die "another backup/restore is already running (lock: $lock_file)"
}
