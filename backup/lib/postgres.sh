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

# litellm-db does not run while /api/llm is on Bifrost (LLM_GATEWAY=bifrost,
# litellm/compose-gate.yml), so a backup then skips it: its volume keeps the
# data, and the snapshots from before the switch hold the dump. A litellm-db
# that is down on LiteLLM still fails the backup.
backup_litellm_db() {
  local out="$1"
  if [[ "${LLM_GATEWAY:-litellm}" == "bifrost" && -z "$(infra_compose ps -q litellm-db 2>/dev/null)" ]]; then
    log "postgres[litellm-db]: not running under LLM_GATEWAY=bifrost, skipped (its volume keeps the data)"
    return 0
  fi
  backup_postgres litellm-db litellm litellm "$out"
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
  # litellm-db, for one, stays stopped while LLM_GATEWAY=bifrost.
  [[ -n "$(infra_compose ps -q "$service" 2>/dev/null)" ]] || \
    die "$service is not running; start it first (\`up -d --wait $service\` on its compose project)"

  local per_color=0
  local stopped_colors=""
  local consumer_where=""
  local consumer_running=0
  if [[ -n "$consumer" ]] && is_app_tier_service "$consumer" && stack_is_bluegreen; then
    per_color=1
    stopped_colors="$(app_service_colors_running "$consumer")"
    if [[ -n "$stopped_colors" ]]; then
      consumer_where=" in color(s) $(format_color_list "$stopped_colors")"
    else
      consumer_where=" (not running in any color)"
    fi
  elif [[ -n "$consumer" ]]; then
    # Put back only what this restore takes down: LiteLLM, for one, stays
    # stopped on purpose while /api/llm runs on Bifrost (litellm/compose-gate.yml).
    if [[ -n "$(infra_compose ps -q "$consumer" 2>/dev/null)" ]]; then
      consumer_running=1
    else
      consumer_where=" (not running: skipped)"
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
    elif [[ "$consumer_running" -eq 1 ]]; then
      log "postgres[$service]: stopping consumer $consumer"
      infra_compose stop "$consumer"
    else
      log "postgres[$service]: consumer $consumer is not running, nothing to stop"
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
    elif [[ "$consumer_running" -eq 1 ]]; then
      log "postgres[$service]: starting consumer $consumer"
      infra_compose up -d --wait "$consumer"
    else
      log "postgres[$service]: leaving consumer $consumer stopped, as it was"
    fi
  fi
  log "postgres[$service]: restore complete"
}
