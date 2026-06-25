#!/usr/bin/env bash
# UniBridge restore tool. Restores one component at a time on purpose:
# ordering and confirmations differ per component, and a blanket "restore all"
# is too easy to fire by accident. See README.md for recovery runbook.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/common.sh"
source "$HERE/lib/etcd.sh"
source "$HERE/lib/postgres.sh"
source "$HERE/lib/sqlite.sh"
source "$HERE/lib/volume.sh"

usage() {
  cat <<EOF
Usage: $0 <component> <backup-dir>

components:
  etcd              restore APISIX config store from etcd.snap
  keycloak-db       restore Keycloak Postgres from keycloak-db.sql.gz
  bifrost-data      restore Bifrost app data from bifrost-data.tar.gz
  unibridge-db      restore unibridge-service Postgres from unibridge-db.sql.gz
  unibridge-meta    restore unibridge-service SQLite from unibridge-meta.db.gz

Pick unibridge-db or unibridge-meta to match the file present in the backup dir
(the default deployment backs up the bundled unibridge-db Postgres).

example:
  $0 etcd ./snapshots/2026-04-19_030000Z
EOF
  exit 2
}

# Verify the backup dir is complete AND the target file's SHA256 matches the
# manifest before attempting destructive restore. Catches partially-copied
# dirs (interrupted rsync etc.) and silent bit rot.
verify_backup_dir() {
  local dir="$1"
  local needs="$2"
  [[ -f "$dir/manifest.json" ]] || die "no manifest.json in $dir (incomplete backup?)"
  [[ -f "$dir/$needs" ]] || die "missing required file in backup: $dir/$needs"

  local expected
  expected="$(manifest_sha256_of "$dir/manifest.json" "$needs")" || \
    die "'$needs' not listed in $dir/manifest.json"
  verify_sha256 "$dir/$needs" "$expected"
  log "verified: $needs matches manifest sha256"
}

# If restore exits non-zero, services left stopped by the restore flow will
# still be down. We don't auto-recover (mid-restore state is ambiguous), but
# we leave the operator a deterministic next step.
on_restore_failure() {
  local rc=$?
  trap - EXIT
  [[ $rc -eq 0 ]] && exit 0
  cat >&2 <<EOF

================================================================
RESTORE FAILED with exit code $rc.
Some services may still be stopped. Check current state:
  docker compose ps

Bring up anything that's down once you've resolved the failure:
  docker compose up -d --wait
================================================================
EOF
  exit $rc
}

main() {
  [[ $# -eq 2 ]] || usage
  local component="$1"
  local dir="$2"
  [[ -d "$dir" ]] || die "backup dir not found: $dir"

  acquire_lock
  load_env
  validate_env_vars
  trap on_restore_failure EXIT

  case "$component" in
    etcd)
      verify_backup_dir "$dir" "etcd.snap"
      restore_etcd "$dir/etcd.snap"
      ;;
    keycloak-db)
      verify_backup_dir "$dir" "keycloak-db.sql.gz"
      restore_postgres keycloak-db keycloak "${KC_DB_USER:-keycloak}" \
        "$dir/keycloak-db.sql.gz" keycloak
      ;;
    bifrost-data)
      verify_backup_dir "$dir" "bifrost-data.tar.gz"
      restore_volume bifrost /app/data "$dir/bifrost-data.tar.gz" bifrost
      ;;
    unibridge-db)
      verify_backup_dir "$dir" "unibridge-db.sql.gz"
      # unibridge-service holds a connection pool to unibridge-db; stop it
      # first or DROP ... in the dump deadlocks on AccessExclusiveLock.
      if [[ -n "${BACKUP_APP_COLORS:-}" ]]; then
        # Blue/green: the app tier lives in separate compose projects, so
        # restore_postgres's same-project consumer stop can't reach it. Quiesce
        # each color's container ourselves around the restore.
        stop_app_consumers
        restore_postgres unibridge-db unibridge unibridge \
          "$dir/unibridge-db.sql.gz"
        start_app_consumers
      else
        # Single-stack: unibridge-service shares this compose project.
        restore_postgres unibridge-db unibridge unibridge \
          "$dir/unibridge-db.sql.gz" unibridge-service
      fi
      ;;
    unibridge-meta)
      verify_backup_dir "$dir" "unibridge-meta.db.gz"
      restore_unibridge_meta "$dir/unibridge-meta.db.gz"
      ;;
    *)
      usage
      ;;
  esac
}

main "$@"
