#!/usr/bin/env bash
# Shared helpers for UniBridge backup scripts.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SNAPSHOTS_ROOT="${SNAPSHOTS_ROOT:-$PROJECT_ROOT/snapshots}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

log()  { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }
die()  { printf '[%s] ERROR: %s\n' "$(date -Iseconds)" "$*" >&2; exit 1; }

compose() {
  (cd "$PROJECT_ROOT" && docker compose "$@")
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
# set COMPOSE_PROJECT_NAME or use -p.
resolve_volume() {
  local service="$1"
  local mount_dest="$2"

  local cid
  cid="$(compose ps -aq "$service" 2>/dev/null | head -1)" || true
  [[ -n "$cid" ]] || die "cannot resolve volume for '$service': no container exists (run 'docker compose up' first)"

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
