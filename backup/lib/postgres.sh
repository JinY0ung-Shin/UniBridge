#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# backup_postgres <service> <db> <user> <out.sql.gz>
backup_postgres() {
  local service="$1"
  local db="$2"
  local user="$3"
  local out="$4"

  log "postgres[$service]: pg_dump -> $out"
  compose exec -T "$service" pg_dump -U "$user" -d "$db" \
    --no-owner --clean --if-exists --quote-all-identifiers \
    | gzip -9 > "$out"
  log "postgres[$service]: $(size_of "$out") bytes"
}

# restore_postgres <service> <db> <user> <in.sql.gz> [consumer-service]
# The consumer service (e.g. keycloak, litellm) holds a connection pool to the
# DB. It must be stopped before restoring or DROP TABLE in the dump will
# deadlock on AccessExclusiveLock.
restore_postgres() {
  local service="$1"
  local db="$2"
  local user="$3"
  local src="$4"
  local consumer="${5:-}"

  [[ -f "$src" ]] || die "dump not found: $src"

  cat >&2 <<EOF
This will:
$( [[ -n "$consumer" ]] && printf '  1. Stop %s (which holds a connection pool to %s)\n' "$consumer" "$db" )
$( [[ -n "$consumer" ]] && printf '  2. ' || printf '  1. ' )DROP all objects in database '$db' and reload from $src
$( [[ -n "$consumer" ]] && printf '  3. Restart %s\n' "$consumer" )
Changes made after the dump was taken will be lost.
EOF
  read -r -p "Type 'RESTORE PG' to continue: " confirm
  [[ "$confirm" == "RESTORE PG" ]] || die "aborted"

  if [[ -n "$consumer" ]]; then
    log "postgres[$service]: stopping consumer $consumer"
    compose stop "$consumer"
  fi

  log "postgres[$service]: restoring $src into $db"
  gunzip -c "$src" | compose exec -T "$service" \
    psql -U "$user" -d "$db" -v ON_ERROR_STOP=1 --quiet

  if [[ -n "$consumer" ]]; then
    log "postgres[$service]: starting consumer $consumer"
    compose up -d --wait "$consumer"
  fi
  log "postgres[$service]: restore complete"
}
