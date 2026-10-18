#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Bifrost keeps its providers, provider keys (encrypted with
# BIFROST_ENCRYPTION_KEY), virtual keys and governance in a SQLite config store
# on its data volume (config_store in bifrost/config.json). Under
# LLM_GATEWAY=bifrost, /api/llm itself runs on what is in it. Only that file is
# backed up: logs.db on the same volume holds the request logs, which Bifrost
# keeps for 14 days and traffic writes again.
#
# The Bifrost image has neither sqlite3 nor Python, so a one-shot sibling
# container that mounts the volume does the SQLite work. Its default image is
# the one compose builds for llm-converter-bi next to Bifrost, so nothing is
# pulled (--pull never: production may be air-gapped). Any local image with
# python3 works.
BIFROST_BACKUP_IMAGE="${BIFROST_BACKUP_IMAGE:-llm-converter:bifrost-test}"

# Where the bifrost service mounts its data volume (the sibling container
# mounts it at the same path), and the config store in it.
BIFROST_MOUNT="/app/data"
BIFROST_CONFIG_DB="config.db"

# Container ids (running or stopped) of the bifrost service. Empty when the
# stack has none, including a compose file without a bifrost service.
bifrost_containers() {
  infra_compose ps -aq bifrost 2>/dev/null || true
}

# Fail before anything changes when the image is missing: --pull never would
# only fail later, and a restore would by then have stopped Bifrost.
require_bifrost_tools_image() {
  docker image inspect "$BIFROST_BACKUP_IMAGE" >/dev/null 2>&1 || \
    die "bifrost: image $BIFROST_BACKUP_IMAGE not found on this host; build it (\`build llm-converter-bi\` on Bifrost's compose project) or set BIFROST_BACKUP_IMAGE to a local image with python3"
}

# run_bifrost_tools <volume> <python-program> [docker-run-option...]
# Runs the program with python3 -I in a one-shot container that has the volume
# at $BIFROST_MOUNT, BIFROST_DB naming the config store. As root, so the
# program can act as whoever owns the store. No network, and no log driver: the
# store itself crosses stdout or stdin and must not be copied into container
# logs.
run_bifrost_tools() {
  local volume="$1" program="$2"
  shift 2
  docker run --rm "$@" --pull never --network none --log-driver none --user 0:0 \
    -v "${volume}:${BIFROST_MOUNT}" \
    -e "BIFROST_DB=${BIFROST_MOUNT}/${BIFROST_CONFIG_DB}" \
    --entrypoint python3 \
    "$BIFROST_BACKUP_IMAGE" -I -c "$program"
}

# Writes a consistent copy of the config store to stdout, while Bifrost keeps
# running.
bifrost_snapshot_program() {
  cat <<'PYEOF'
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

db = Path(os.environ["BIFROST_DB"])
if not db.is_file():
    sys.exit(f"bifrost: {db} not found on the volume")
if os.getuid() == 0:
    # Read as the store's owner, Bifrost's own user: a reader of a WAL database
    # writes its -shm index, and creates it (and -wal) while Bifrost is stopped.
    # SQLite builds with HAVE_FCHOWN hand such files to the store's owner when
    # run as root, but reading as that owner needs no root at all and does not
    # depend on how BIFROST_BACKUP_IMAGE's SQLite was built.
    owner = db.stat()
    os.setgroups([])
    os.setgid(owner.st_gid)
    os.setuid(owner.st_uid)

with tempfile.TemporaryDirectory() as tmp:
    copy = os.path.join(tmp, db.name)
    # Read-only, so nothing here can checkpoint or otherwise write Bifrost's
    # file. backup() copies every page in one step, inside one read
    # transaction: a consistent snapshot even while Bifrost writes.
    src = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(copy)
    try:
        src.backup(dst)
    finally:
        src.close()
    problems = dst.execute("PRAGMA integrity_check").fetchall()
    dst.close()
    if problems != [("ok",)]:
        sys.exit(f"bifrost: the snapshot fails PRAGMA integrity_check: {problems[:5]}")
    with open(copy, "rb") as fh:
        shutil.copyfileobj(fh, sys.stdout.buffer)
PYEOF
}

# Replaces the config store with the SQLite file it reads on stdin. Bifrost
# must be stopped.
bifrost_restore_program() {
  cat <<'PYEOF'
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

db = Path(os.environ["BIFROST_DB"])
with tempfile.TemporaryDirectory() as tmp:
    restored = Path(tmp) / db.name
    with open(restored, "wb") as fh:
        shutil.copyfileobj(sys.stdin.buffer, fh)
    conn = sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)
    try:
        problems = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    if problems != [("ok",)]:
        sys.exit(f"bifrost: the snapshot fails PRAGMA integrity_check: {problems[:5]}")

    # Keep the owner and mode Bifrost gave its store. Without one, the volume
    # directory, which the image creates for Bifrost's user, names the owner.
    if db.exists():
        ref = db.stat()
        mode = stat.S_IMODE(ref.st_mode)
    else:
        ref = db.parent.stat()
        mode = 0o644
    # Stage the new file next to the store first, so a failure here (a full
    # disk, say) leaves the old store and its sidecars as they were.
    staged = db.with_name(f".{db.name}.restore")
    shutil.copyfile(restored, staged)
    os.chown(staged, ref.st_uid, ref.st_gid)
    os.chmod(staged, mode)
    with open(staged, "rb") as fh:
        os.fsync(fh.fileno())
    # Then drop the old store's sidecars, right before the swap: SQLite would
    # replay a leftover -wal or -journal into the restored file and corrupt it.
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    os.replace(staged, db)
    dir_fd = os.open(db.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
PYEOF
}

# backup_bifrost <out.db.gz>
backup_bifrost() {
  local out="$1"

  if [[ -z "$(bifrost_containers)" ]]; then
    # /api/llm runs on Bifrost then: no container is an outage, not a stack
    # without Bifrost.
    [[ "${LLM_GATEWAY:-litellm}" != "bifrost" ]] || \
      die "bifrost: LLM_GATEWAY=bifrost, but the stack has no bifrost container to back up; start it (\`up -d --wait bifrost\` on its compose project)"
    log "bifrost: no bifrost container in this stack (Bifrost is not deployed), skipped"
    return 0
  fi

  local volume
  volume="$(resolve_volume bifrost "$BIFROST_MOUNT")"
  require_bifrost_tools_image
  log "bifrost: snapshot of $BIFROST_CONFIG_DB on volume $volume via $BIFROST_BACKUP_IMAGE"

  # The copy comes back on stdout, so this shell writes the snapshot as the
  # invoking user and the container never touches the snapshot directory.
  if ! run_bifrost_tools "$volume" "$(bifrost_snapshot_program)" | gzip -9 > "$out"; then
    rm -f "$out"
    die "bifrost: snapshot of $BIFROST_CONFIG_DB failed"
  fi
  chmod 600 "$out"
  log "bifrost: $(size_of "$out") bytes"
}

# restore_bifrost <in.db.gz>
# Bifrost holds the store open, so it is stopped while config.db is replaced
# and started again afterwards. logs.db stays as it is.
restore_bifrost() {
  local src="$1"
  [[ -f "$src" ]] || die "snapshot not found: $src"
  [[ -n "$(bifrost_containers)" ]] || \
    die "no bifrost container to restore into; create it first (\`up -d --wait bifrost\` on its compose project)"

  # Resolve the volume name while the container exists.
  local volume
  volume="$(resolve_volume bifrost "$BIFROST_MOUNT")"
  log "bifrost: resolved volume name: $volume"
  require_bifrost_tools_image

  cat >&2 <<EOF
This will:
  1. Stop bifrost: /api/llm-bi, and /api/llm under LLM_GATEWAY=bifrost, are down until step 3
  2. Replace $BIFROST_CONFIG_DB on docker volume $volume with $src
     (logs.db, the request logs, stays as it is)
  3. Start bifrost and wait for it to be healthy

Providers, provider keys, virtual keys and governance settings will be replaced
with the snapshot contents. Changes made after the snapshot was taken will be
lost. The provider keys in it decrypt only with the BIFROST_ENCRYPTION_KEY that
was set when the snapshot was taken.
EOF
  read -r -p "Type 'RESTORE BIFROST' to continue: " confirm
  [[ "$confirm" == "RESTORE BIFROST" ]] || die "aborted"

  log "bifrost: stopping bifrost"
  infra_compose stop bifrost

  log "bifrost: replacing $BIFROST_CONFIG_DB"
  if ! gunzip -c "$src" | run_bifrost_tools "$volume" "$(bifrost_restore_program)" -i; then
    # The program checks the snapshot before it touches the volume, so the
    # store Bifrost had is still in place: bring Bifrost back on it.
    log "bifrost: replacing $BIFROST_CONFIG_DB failed; starting bifrost again on its previous store"
    infra_compose up -d --wait bifrost || log "bifrost: could not start bifrost again; start it by hand"
    die "bifrost: replacing $BIFROST_CONFIG_DB failed"
  fi

  log "bifrost: starting bifrost and waiting for healthy"
  infra_compose up -d --wait bifrost
  log "bifrost: restore complete"
}
