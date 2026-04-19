#!/usr/bin/env bash
# UniBridge full backup orchestrator.
# Runs each component backup, writes manifest, rotates old backups.
# Safe to run manually or via cron. Exits non-zero on any failure.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/common.sh"
source "$HERE/lib/etcd.sh"
source "$HERE/lib/postgres.sh"
source "$HERE/lib/sqlite.sh"

main() {
  acquire_lock
  load_env
  validate_env_vars

  local stamp
  stamp="$(date -u +%Y-%m-%d_%H%M%SZ)"
  local dest="$SNAPSHOTS_ROOT/$stamp"

  log "==== backup start: $stamp ===="
  log "destination: $dest"
  mkdir -p "$SNAPSHOTS_ROOT" "$dest"
  chmod 700 "$dest"

  backup_etcd "$dest/etcd.snap"
  backup_postgres keycloak-db keycloak "${KC_DB_USER:-keycloak}"   "$dest/keycloak-db.sql.gz"
  backup_postgres litellm-db  litellm  litellm                     "$dest/litellm-db.sql.gz"
  backup_unibridge_meta "$dest/unibridge-meta.db.gz"

  write_manifest "$dest" "$stamp"

  find "$dest" -type f -exec chmod 600 {} +

  rotate_old

  log "==== backup done: $dest ===="
}

write_manifest() {
  local dest="$1"
  local stamp="$2"
  local manifest="$dest/manifest.json"

  {
    printf '{\n'
    printf '  "timestamp": "%s",\n' "$stamp"
    printf '  "retention_days": %s,\n' "$RETENTION_DAYS"
    printf '  "files": [\n'
    local first=1
    shopt -s nullglob
    for f in "$dest"/*; do
      [[ "$f" == "$manifest" ]] && continue
      [[ $first -eq 1 ]] || printf ',\n'
      first=0
      printf '    {"name": "%s", "size": %s, "sha256": "%s"}' \
        "$(basename "$f")" "$(size_of "$f")" "$(sha256_of "$f")"
    done
    shopt -u nullglob
    printf '\n  ]\n}\n'
  } > "$manifest"
  log "manifest written"
}

rotate_old() {
  log "rotating backups older than ${RETENTION_DAYS} days"
  local removed=0
  # find -mtime +N means "older than N full days", so +14 keeps 15 days of
  # backups. Use +(N-1) so retention of 14 days actually keeps 14 days.
  local threshold=$((RETENTION_DAYS - 1))
  while IFS= read -r -d '' dir; do
    log "removing old backup: $dir"
    rm -rf "$dir"
    removed=$((removed + 1))
  done < <(find "$SNAPSHOTS_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+${threshold}" -print0)
  log "rotation: removed $removed"
}

main "$@"
