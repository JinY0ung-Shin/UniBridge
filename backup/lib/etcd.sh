#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# etcd image used in docker-compose.yml. Kept in sync with the service image.
ETCD_IMAGE="${ETCD_IMAGE:-bitnamilegacy/etcd:3.5.11}"

# Path inside the bitnami etcd container where the data volume is mounted.
ETCD_MOUNT="/bitnami/etcd"

backup_etcd() {
  local out="$1"
  local remote_tmp="/tmp/etcd-backup.snap"

  log "etcd: taking snapshot"
  # Pass the password via environment (ETCDCTL_USER), never argv, so it does
  # not appear in `ps` on the host or in /proc/*/cmdline inside the container.
  if [[ -n "${ETCD_ROOT_PASSWORD:-}" ]]; then
    compose exec -T \
      -e "ETCDCTL_USER=root:${ETCD_ROOT_PASSWORD}" \
      etcd etcdctl --command-timeout=30s snapshot save "$remote_tmp"
  else
    compose exec -T \
      etcd etcdctl --command-timeout=30s snapshot save "$remote_tmp"
  fi

  compose cp "etcd:${remote_tmp}" "$out"
  compose exec -T etcd rm -f "$remote_tmp"
  log "etcd: snapshot saved to $out ($(size_of "$out") bytes)"
}

# Restore uses a one-shot root container that mounts the etcd data volume
# directly. This avoids the permission problems (bitnami runs as UID 1001 and
# cannot recreate /bitnami/etcd subdirectories) and the data-dir hot-swap race
# that breaks restoring into a running etcd.
restore_etcd() {
  local snap="$1"
  [[ -f "$snap" ]] || die "snapshot not found: $snap"

  # Resolve volume name while the etcd container still exists.
  local volume
  volume="$(resolve_volume etcd "$ETCD_MOUNT")"
  log "etcd: resolved volume name: $volume"

  cat >&2 <<EOF
This will:
  1. Stop apisix and etcd
  2. DELETE and recreate docker volume: $volume
  3. Restore snapshot $snap as the new etcd data
  4. Start etcd then apisix

APISIX routes/consumers/plugin configs will be replaced with the snapshot
contents. Any changes made after the snapshot was taken will be lost.
EOF
  read -r -p "Type 'RESTORE ETCD' to continue: " confirm
  [[ "$confirm" == "RESTORE ETCD" ]] || die "aborted"

  log "etcd: stopping apisix and etcd"
  compose stop apisix etcd
  compose rm -f etcd

  log "etcd: recreating volume $volume"
  docker volume rm "$volume" || die "failed to remove volume $volume"
  docker volume create "$volume" >/dev/null

  log "etcd: running one-shot restore container (as root)"
  # Snapshot is piped via stdin so no temp file needs cleanup, no `docker cp`
  # into a stopped container, no shell quoting of paths from the host side.
  docker run --rm -i --user 0:0 \
    -v "${volume}:${ETCD_MOUNT}" \
    --entrypoint sh \
    "$ETCD_IMAGE" -c "
      set -e
      cat > /tmp/snap
      rm -rf ${ETCD_MOUNT}/data
      if command -v etcdutl >/dev/null 2>&1; then
        etcdutl snapshot restore /tmp/snap --data-dir=${ETCD_MOUNT}/data
      else
        ETCDCTL_API=3 etcdctl snapshot restore /tmp/snap --data-dir=${ETCD_MOUNT}/data
      fi
      chown -R 1001:0 ${ETCD_MOUNT}
      rm -f /tmp/snap
    " < "$snap"

  log "etcd: starting etcd and waiting for healthy"
  compose up -d --wait etcd
  log "etcd: starting apisix"
  compose up -d --wait apisix
  log "etcd: restarting unibridge-service to replay APISIX consumers"
  compose restart unibridge-service
  log "etcd: restore complete"
}
