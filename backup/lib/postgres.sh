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
  infra_compose exec -T "$service" pg_dump -U "$user" -d "$db" \
    --no-owner --clean --if-exists --quote-all-identifiers \
    | gzip -9 > "$out"
  log "postgres[$service]: $(size_of "$out") bytes"
}

# restore_postgres <service> <db> <user> <in.sql.gz> [consumer-service]
# The consumer service (e.g. keycloak, litellm) holds a connection pool to the
# DB. It must be stopped before restoring or DROP TABLE in the dump will
# deadlock on AccessExclusiveLock.
#
# Under blue-green an app-tier consumer (unibridge-service) runs once per color
# against the same database, so every running color has to be stopped — and only
# those colors are started again afterwards.
restore_postgres() {
  local service="$1"
  local db="$2"
  local user="$3"
  local src="$4"
  local consumer="${5:-}"

  [[ -f "$src" ]] || die "dump not found: $src"

  local per_color=0
  local stopped_colors=""
  local consumer_where=""
  if [[ -n "$consumer" ]] && is_app_tier_service "$consumer" && stack_is_bluegreen; then
    per_color=1
    stopped_colors="$(app_service_colors_running "$consumer")"
    if [[ -n "$stopped_colors" ]]; then
      consumer_where=" in color(s) $(format_color_list "$stopped_colors")"
    else
      consumer_where=" (not running in any color)"
    fi
  fi

  cat >&2 <<EOF
This will:
$( [[ -n "$consumer" ]] && printf '  1. Stop %s%s (which holds a connection pool to %s)\n' "$consumer" "$consumer_where" "$db" )
$( [[ -n "$consumer" ]] && printf '  2. ' || printf '  1. ' )DROP all objects in database '$db' and reload from $src
$( [[ -n "$consumer" ]] && printf '  3. Restart %s%s\n' "$consumer" "$consumer_where" )
Changes made after the dump was taken will be lost.
EOF
  read -r -p "Type 'RESTORE PG' to continue: " confirm
  [[ "$confirm" == "RESTORE PG" ]] || die "aborted"

  if [[ -n "$consumer" ]]; then
    if [[ "$per_color" -eq 1 ]]; then
      if [[ -n "$stopped_colors" ]]; then
        local color
        while IFS= read -r color; do
          [[ -n "$color" ]] || continue
          log "postgres[$service]: stopping consumer $consumer ($color)"
          color_compose "$color" stop "$consumer"
        done <<< "$stopped_colors"
      else
        log "postgres[$service]: consumer $consumer is not running in any color, nothing to stop"
      fi
    else
      log "postgres[$service]: stopping consumer $consumer"
      infra_compose stop "$consumer"
    fi
  fi

  log "postgres[$service]: restoring $src into $db"
  gunzip -c "$src" | infra_compose exec -T "$service" \
    psql -U "$user" -d "$db" -v ON_ERROR_STOP=1 --quiet

  if [[ -n "$consumer" ]]; then
    if [[ "$per_color" -eq 1 ]]; then
      local color
      while IFS= read -r color; do
        [[ -n "$color" ]] || continue
        log "postgres[$service]: starting consumer $consumer ($color)"
        # --no-recreate: the color was built by scripts/deploy-bluegreen.sh with
        # deploy-time env (APISIX_PROVISION_ON_START, upstream nodes). Recreating
        # it here would rebuild that env from defaults and could let an inactive
        # color re-provision APISIX routes at itself.
        color_compose "$color" up -d --no-recreate --wait "$consumer"
      done <<< "$stopped_colors"
    else
      log "postgres[$service]: starting consumer $consumer"
      infra_compose up -d --wait "$consumer"
    fi
  fi
  log "postgres[$service]: restore complete"
}
