#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# unibridge-service metadata is Postgres by default in compose, but older
# deployments may still point META_DB_URL at SQLite. Dispatch backup/restore by
# the actual URL instead of assuming a stale /app/data/meta.db file exists.

UNIBRIDGE_META_PG_SERVICE="${UNIBRIDGE_META_PG_SERVICE:-unibridge-db}"
UNIBRIDGE_META_PG_DB="${UNIBRIDGE_META_PG_DB:-unibridge}"
UNIBRIDGE_META_PG_USER="${UNIBRIDGE_META_PG_USER:-unibridge}"

unibridge_meta_kind() {
  local url="${META_DB_URL:-}"
  if [[ -z "$url" ]]; then
    printf 'postgres\n'
    return
  fi

  case "$url" in
    sqlite:*|sqlite+*) printf 'sqlite\n' ;;
    postgresql:*|postgresql+*|postgres:*|postgres+*) printf 'postgres\n' ;;
    *) die "unsupported META_DB_URL for unibridge metadata backup: $url" ;;
  esac
}

backup_unibridge_meta() {
  local dest="$1"

  case "$(unibridge_meta_kind)" in
    sqlite)
      backup_unibridge_meta_sqlite "$dest/unibridge-meta.db.gz"
      ;;
    postgres)
      backup_postgres \
        "$UNIBRIDGE_META_PG_SERVICE" \
        "$UNIBRIDGE_META_PG_DB" \
        "$UNIBRIDGE_META_PG_USER" \
        "$dest/unibridge-meta.sql.gz"
      ;;
  esac
}

unibridge_meta_backup_file() {
  local dir="$1"

  if [[ -f "$dir/unibridge-meta.sql.gz" ]]; then
    printf 'unibridge-meta.sql.gz\n'
    return
  fi
  if [[ -f "$dir/unibridge-meta.db.gz" ]]; then
    printf 'unibridge-meta.db.gz\n'
    return
  fi

  die "missing unibridge metadata backup in $dir (expected unibridge-meta.sql.gz or unibridge-meta.db.gz)"
}

restore_unibridge_meta_snapshot() {
  local src="$1"

  case "$(basename "$src")" in
    unibridge-meta.sql.gz)
      restore_postgres \
        "$UNIBRIDGE_META_PG_SERVICE" \
        "$UNIBRIDGE_META_PG_DB" \
        "$UNIBRIDGE_META_PG_USER" \
        "$src" \
        unibridge-service
      ;;
    unibridge-meta.db.gz)
      restore_unibridge_meta_sqlite "$src"
      ;;
    *)
      die "unsupported unibridge metadata backup file: $src"
      ;;
  esac
}
