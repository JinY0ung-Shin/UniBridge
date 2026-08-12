#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Legacy unibridge-service metadata DB at /app/data/meta.db (SQLite)
# VACUUM INTO produces a consistent snapshot even under concurrent writes.

backup_unibridge_meta_sqlite() {
  local out="$1"
  local remote_tmp="/app/data/meta.backup.db"

  log "sqlite: VACUUM INTO on meta.db"
  # Quoted heredoc + env vars: paths never pass through the host shell as
  # template substitutions, so future parameterization cannot introduce
  # injection via a malformed path.
  app_compose exec -T \
    -e SRC="/app/data/meta.db" \
    -e DST="$remote_tmp" \
    unibridge-service python - <<'PYEOF'
import os
import sqlite3

src = os.environ["SRC"]
dst = os.environ["DST"]
if not os.path.exists(src):
    raise SystemExit(f"source missing: {src}")
if os.path.exists(dst):
    os.remove(dst)
# VACUUM INTO does not accept parameter binding for the target path;
# escape single quotes defensively. The path is set from env by the
# caller, never from user input.
escaped = dst.replace("'", "''")
conn = sqlite3.connect(src)
conn.execute(f"VACUUM INTO '{escaped}'")
conn.close()
PYEOF

  local uncompressed="${out%.gz}"
  app_compose cp "unibridge-service:${remote_tmp}" "$uncompressed"
  gzip -9 -f "$uncompressed"
  app_compose exec -T unibridge-service rm -f "$remote_tmp"
  log "sqlite: $(size_of "$out") bytes"
}

# Blue-green with SQLite is a legacy combination (ALLOW_SQLITE_BLUEGREEN): both
# colors mount the same data volume, so every running color must be stopped
# before the file is swapped, and only those colors are started again.
restore_unibridge_meta_sqlite() {
  local src="$1"
  [[ -f "$src" ]] || die "dump not found: $src"

  local per_color=0
  local stopped_colors=""
  local service_where=""
  if stack_is_bluegreen; then
    per_color=1
    stopped_colors="$(app_service_colors_running unibridge-service)"
    if [[ -n "$stopped_colors" ]]; then
      service_where=" in color(s) $(format_color_list "$stopped_colors")"
    else
      service_where=" (not running in any color)"
    fi
  fi

  cat >&2 <<EOF
This will:
  1. Stop unibridge-service$service_where
  2. Overwrite /app/data/meta.db with $src
  3. Remove stale WAL/SHM sidecars so SQLite doesn't recover from them
  4. Restart unibridge-service$service_where

API keys and encrypted credentials will be replaced with the snapshot contents.
EOF
  read -r -p "Type 'RESTORE META' to continue: " confirm
  [[ "$confirm" == "RESTORE META" ]] || die "aborted"

  local tmp
  tmp="$(mktemp)"
  gunzip -c "$src" > "$tmp"

  if [[ "$per_color" -eq 1 ]]; then
    local color
    while IFS= read -r color; do
      [[ -n "$color" ]] || continue
      log "sqlite: stopping unibridge-service ($color)"
      color_compose "$color" stop unibridge-service
    done <<< "$stopped_colors"
  else
    app_compose stop unibridge-service
  fi

  # Wipe stale WAL/SHM: if present after we swap meta.db, SQLite will try to
  # recover pages from them into the fresh DB and corrupt it. Both operations go
  # through one color; the data volume is shared, so either color reaches the
  # same file.
  app_compose run --rm --no-deps --entrypoint sh unibridge-service -c \
    'rm -f /app/data/meta.db /app/data/meta.db-wal /app/data/meta.db-shm'

  app_compose cp "$tmp" "unibridge-service:/app/data/meta.db"
  rm -f "$tmp"

  if [[ "$per_color" -eq 1 ]]; then
    local restart_color
    while IFS= read -r restart_color; do
      [[ -n "$restart_color" ]] || continue
      log "sqlite: starting unibridge-service ($restart_color)"
      # --no-recreate: keep the container the deploy script built, env included.
      color_compose "$restart_color" up -d --no-recreate --wait unibridge-service
    done <<< "$stopped_colors"
  else
    app_compose up -d --wait unibridge-service
  fi
  log "sqlite: restore complete"
}
