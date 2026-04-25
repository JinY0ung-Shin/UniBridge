#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# unibridge-service metadata DB at /app/data/meta.db (SQLite)
# VACUUM INTO produces a consistent snapshot even under concurrent writes.

backup_unibridge_meta() {
  local out="$1"
  local remote_tmp="/app/data/meta.backup.db"

  log "sqlite: VACUUM INTO on meta.db"
  # Quoted heredoc + env vars: paths never pass through the host shell as
  # template substitutions, so future parameterization cannot introduce
  # injection via a malformed path.
  compose exec -T \
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
  compose cp "unibridge-service:${remote_tmp}" "$uncompressed"
  gzip -9 -f "$uncompressed"
  compose exec -T unibridge-service rm -f "$remote_tmp"
  log "sqlite: $(size_of "$out") bytes"
}

restore_unibridge_meta() {
  local src="$1"
  [[ -f "$src" ]] || die "dump not found: $src"

  cat >&2 <<EOF
This will:
  1. Stop unibridge-service
  2. Overwrite /app/data/meta.db with $src
  3. Remove stale WAL/SHM sidecars so SQLite doesn't recover from them
  4. Restart unibridge-service

API keys and encrypted credentials will be replaced with the snapshot contents.
EOF
  read -r -p "Type 'RESTORE META' to continue: " confirm
  [[ "$confirm" == "RESTORE META" ]] || die "aborted"

  local tmp
  tmp="$(mktemp)"
  gunzip -c "$src" > "$tmp"

  compose stop unibridge-service

  # Wipe stale WAL/SHM: if present after we swap meta.db, SQLite will try to
  # recover pages from them into the fresh DB and corrupt it.
  compose run --rm --no-deps --entrypoint sh unibridge-service -c \
    'rm -f /app/data/meta.db /app/data/meta.db-wal /app/data/meta.db-shm'

  compose cp "$tmp" "unibridge-service:/app/data/meta.db"
  rm -f "$tmp"

  compose up -d --wait unibridge-service
  compose restart apisix
  log "sqlite: restore complete"
}
