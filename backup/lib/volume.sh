#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# backup_volume <service> <mount-dest> <out.tar.gz>
backup_volume() {
  local service="$1"
  local mount_dest="$2"
  local out="$3"

  local volume
  volume="$(resolve_volume "$service" "$mount_dest")"
  local out_dir out_name
  out_dir="$(dirname "$out")"
  out_name="$(basename "$out")"

  log "volume[$service]: $volume:$mount_dest -> $out"
  docker run --rm \
    -v "$volume":/source:ro \
    -v "$out_dir":/backup \
    alpine:3.20 \
    sh -c "cd /source && tar -czf /backup/$out_name ."
  log "volume[$service]: $(size_of "$out") bytes"
}

# restore_volume <service> <mount-dest> <in.tar.gz> [consumer-service]
restore_volume() {
  local service="$1"
  local mount_dest="$2"
  local src="$3"
  local consumer="${4:-$service}"

  [[ -f "$src" ]] || die "archive not found: $src"

  local volume
  volume="$(resolve_volume "$service" "$mount_dest")"
  local src_dir src_name
  src_dir="$(dirname "$src")"
  src_name="$(basename "$src")"

  cat >&2 <<EOF
This will:
  1. Stop $consumer
  2. Delete all files in volume '$volume' mounted at $mount_dest
  3. Restore that volume from $src
  4. Restart $consumer

Changes made after the archive was taken will be lost.
EOF
  read -r -p "Type 'RESTORE VOLUME' to continue: " confirm
  [[ "$confirm" == "RESTORE VOLUME" ]] || die "aborted"

  log "volume[$service]: stopping consumer $consumer"
  compose stop "$consumer"

  log "volume[$service]: restoring $src into $volume"
  docker run --rm \
    -v "$volume":/target \
    -v "$src_dir":/backup:ro \
    alpine:3.20 \
    sh -c "find /target -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar -xzf /backup/$src_name -C /target"

  log "volume[$service]: starting consumer $consumer"
  compose up -d --wait "$consumer"
  log "volume[$service]: restore complete"
}
