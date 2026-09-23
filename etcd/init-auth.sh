#!/bin/sh
# Bootstrap etcd root authentication for UniBridge.
#
# The Bitnami etcd image this stack used to run did this itself, from
# ALLOW_NONE_AUTHENTICATION + ETCD_ROOT_PASSWORD. The upstream CoreOS image has
# no such bootstrap, so the `etcd-init` one-shot compose service runs this
# script after etcd reports healthy and reproduces the same contract:
#
#   * no password + ETCD_ALLOW_NONE_AUTH=yes  -> leave auth off (dev only)
#   * no password + ETCD_ALLOW_NONE_AUTH=no   -> refuse to run
#   * password set                            -> root user exists, its password
#                                                equals $ETCD_ROOT_PASSWORD,
#                                                and auth is enabled
#
# Compose re-runs this on every `up`, so every path below must be idempotent.
set -eu

ENDPOINTS="${ETCD_ENDPOINTS:-http://etcd:2379}"
ROOT_PASSWORD="${ETCD_ROOT_PASSWORD:-}"
ALLOW_NONE_AUTH="${ETCD_ALLOW_NONE_AUTH:-no}"
WAIT_SECONDS="${ETCD_INIT_WAIT_SECONDS:-60}"

log() { printf '[etcd-init] %s\n' "$*"; }
die() { printf '[etcd-init] ERROR: %s\n' "$*" >&2; exit 1; }

ETCDCTL_API=3
ETCDCTL_ENDPOINTS="$ENDPOINTS"
export ETCDCTL_API ETCDCTL_ENDPOINTS

# The password travels in the environment, never in argv: argv is visible in
# `ps` and /proc/*/cmdline to anything sharing the container's PID namespace.
# etcdctl ignores credentials while auth is still disabled, so exporting this
# unconditionally is safe on a fresh volume too.
if [ -n "$ROOT_PASSWORD" ]; then
  ETCDCTL_USER="root:$ROOT_PASSWORD"
  export ETCDCTL_USER
fi

# --- configuration gate (before the health wait, so a misconfigured stack
# --- fails in a second instead of after a minute of retries) -----------------
if [ -z "$ROOT_PASSWORD" ]; then
  case "$ALLOW_NONE_AUTH" in
    yes|YES|Yes|true|TRUE|True|1)
      log "ETCD_ROOT_PASSWORD is empty and ETCD_ALLOW_NONE_AUTH=${ALLOW_NONE_AUTH}: leaving etcd unauthenticated (development only)."
      exit 0
      ;;
    *)
      die "ETCD_ROOT_PASSWORD is empty. Set it in .env (recommended), or set ETCD_ALLOW_NONE_AUTH=yes to run etcd without authentication — development only, since etcd holds every APISIX route, upstream and consumer."
      ;;
  esac
fi

# --- wait for etcd to answer -------------------------------------------------
# `depends_on: etcd: condition: service_healthy` normally makes this a no-op;
# it stays as a guard for direct `docker run` invocations of this image.
log "waiting for etcd at ${ENDPOINTS} (up to ${WAIT_SECONDS}s)"
waited=0
while : ; do
  if health_output="$(etcdctl endpoint health 2>&1)"; then
    log "etcd is reachable"
    break
  fi

  # A wrong password looks exactly like an outage to the retry loop, so name it
  # instead of burning the whole timeout on it.
  case "$health_output" in
    *"authentication failed"*|*"invalid user ID or password"*)
      die "etcd rejected the root credentials: the password stored in the etcd data directory does not match ETCD_ROOT_PASSWORD. Restore the old value in .env, or rotate it with 'etcdctl user passwd root' against the running server."
      ;;
  esac

  waited=$((waited + 2))
  if [ "$waited" -ge "$WAIT_SECONDS" ]; then
    printf '%s\n' "$health_output" >&2
    die "etcd did not become reachable at ${ENDPOINTS} within ${WAIT_SECONDS}s"
  fi
  sleep 2
done

# --- converge auth state -----------------------------------------------------
if ! auth_status="$(etcdctl auth status 2>&1)"; then
  printf '%s\n' "$auth_status" >&2
  case "$auth_status" in
    *"authentication failed"*|*"invalid user ID or password"*)
      die "the root password stored in etcd does not match ETCD_ROOT_PASSWORD. Restore the old value in .env, or rotate it with 'etcdctl user passwd root' against the running server."
      ;;
  esac
  die "could not read etcd auth status"
fi

case "$auth_status" in
  *"Authentication Status: true"*)
    log "authentication is already enabled and the root password matches; nothing to do"
    exit 0
    ;;
esac

log "authentication is disabled; enabling it"

# `user add` is not idempotent (exit 1, "user name already exists"), so branch
# on whether root is there. Auth is off at this point, which is why both the
# lookup and the write below go through unauthenticated.
# Credentials are set but auth is off, so every call below goes through
# unauthenticated and etcd's client library logs a "authentication is not
# enabled" retry warning for each one. Those are expected here and would only
# scare an operator reading the migration log, so output is captured and
# printed only when the command actually fails.
run_quiet() {
  quiet_output="$("$@" 2>&1)" && return 0
  printf '%s\n' "$quiet_output" >&2
  return 1
}

if user_get_output="$(etcdctl user get root 2>&1)"; then
  log "root user already exists; setting its password to ETCD_ROOT_PASSWORD"
  printf '%s\n' "$ROOT_PASSWORD" | run_quiet etcdctl user passwd root --interactive=false \
    || die "failed to set the root password"
else
  case "$user_get_output" in
    *"user name not found"*)
      log "creating the root user"
      # etcdctl grants the built-in root role automatically when it is missing.
      printf '%s\n' "$ROOT_PASSWORD" | run_quiet etcdctl user add root --interactive=false \
        || die "failed to create the root user"
      ;;
    *)
      printf '%s\n' "$user_get_output" >&2
      die "could not look up the root user"
      ;;
  esac
fi

# Idempotent: exits 0 when auth is already on.
run_quiet etcdctl auth enable || die "failed to enable etcd authentication"

# Prove the credentials APISIX and the backups will use actually work, rather
# than trusting the write path above.
verify="$(etcdctl auth status 2>&1)" \
  || { printf '%s\n' "$verify" >&2; die "auth was enabled but the root credentials do not work"; }
case "$verify" in
  *"Authentication Status: true"*)
    log "authentication enabled and verified"
    ;;
  *)
    printf '%s\n' "$verify" >&2
    die "auth enable reported success but authentication is still disabled"
    ;;
esac
