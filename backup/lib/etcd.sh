#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# etcdctl/etcdutl toolbox built from etcd/Dockerfile by the `etcd-init` service
# in docker-compose.yml / docker-compose.infra.yml. The etcd server image itself
# is distroless (no shell, no rm, no temp dirs), so snapshots are taken and
# restored from sibling containers of this image instead of `exec`ing into etcd.
# Build it by hand with `docker compose build etcd-init` if it is missing.
ETCD_TOOLS_IMAGE="${ETCD_TOOLS_IMAGE:-unibridge-etcd-tools:3.5.33}"

# Client endpoint of the etcd service, as seen from a sibling container on the
# same docker network.
ETCD_ENDPOINT="${ETCD_ENDPOINT:-http://etcd:2379}"

# Path inside the etcd container where the data volume is mounted. The upstream
# image keeps the old Bitnami path on purpose (ETCD_DATA_DIR=/bitnami/etcd/data)
# so existing volumes migrate in place — see the compose comment.
ETCD_MOUNT="/bitnami/etcd"

backup_etcd() {
  local out="$1"

  # docker -v needs absolute paths, and the snapshot is written by a container,
  # so the host directory has to exist before the mount is created.
  local out_dir out_file
  out_dir="$(cd "$(dirname "$out")" && pwd)" || die "backup directory not found: $(dirname "$out")"
  out_file="$(basename "$out")"

  local network
  network="$(resolve_network etcd)"
  log "etcd: taking snapshot via $ETCD_TOOLS_IMAGE on network $network"

  local -a run_args=(
    run --rm
    --network "$network"
    # Write as the invoking user so the snapshot is not left root-owned and the
    # chmod below (and backup.sh's final chmod sweep) can actually apply.
    --user "$(id -u):$(id -g)"
    -v "${out_dir}:/out"
    --entrypoint etcdctl
  )
  # Pass the password via environment (ETCDCTL_USER), never argv, so it does
  # not appear in `ps` on the host or in /proc/*/cmdline inside the container.
  if [[ -n "${ETCD_ROOT_PASSWORD:-}" ]]; then
    run_args+=(-e "ETCDCTL_USER=root:${ETCD_ROOT_PASSWORD}")
  fi

  docker "${run_args[@]}" "$ETCD_TOOLS_IMAGE" \
    --endpoints "$ETCD_ENDPOINT" --command-timeout=30s snapshot save "/out/${out_file}" \
    || die "etcd snapshot failed (is the etcd container running and does ETCD_ROOT_PASSWORD match?)"

  chmod 600 "$out"
  log "etcd: snapshot saved to $out ($(size_of "$out") bytes)"
}

# Restore uses a one-shot container that mounts the etcd data volume directly.
# This avoids the data-dir hot-swap race that breaks restoring into a running
# etcd, and it needs no chown: the upstream etcd image runs as root, so the
# files etcdutl writes as root are exactly what etcd expects to find.
restore_etcd() {
  local snap="$1"
  [[ -f "$snap" ]] || die "snapshot not found: $snap"
  # Absolute path for the bind mount below.
  snap="$(cd "$(dirname "$snap")" && pwd)/$(basename "$snap")"

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
  infra_compose stop apisix etcd
  infra_compose rm -f etcd

  log "etcd: recreating volume $volume"
  docker volume rm "$volume" || die "failed to remove volume $volume"
  docker volume create "$volume" >/dev/null

  log "etcd: running one-shot restore container (as root)"
  # The volume is brand new, so there is nothing to delete first — which is why
  # this works in an image without `rm`. etcdutl creates ${ETCD_MOUNT}/data.
  docker run --rm \
    -v "${volume}:${ETCD_MOUNT}" \
    -v "${snap}:/tmp/snap:ro" \
    --entrypoint etcdutl \
    "$ETCD_TOOLS_IMAGE" \
    snapshot restore /tmp/snap --data-dir="${ETCD_MOUNT}/data" \
    || die "etcd snapshot restore failed"

  log "etcd: starting etcd and waiting for healthy"
  infra_compose up -d --wait etcd
  # Bringing up apisix also runs etcd-init, because apisix depends on it with
  # service_completed_successfully. Do NOT `up --wait etcd-init` on its own:
  # compose fails a --wait whose selected set contains a service that exits,
  # even with exit 0, unless a selected service depends on its completion.
  # The bootstrap is a no-op here — the snapshot carries etcd's own auth state
  # — but it still verifies ETCD_ROOT_PASSWORD before apisix connects.
  log "etcd: running the auth bootstrap and starting apisix"
  infra_compose up -d --wait apisix
  log "etcd: restore complete"
}
