#!/bin/sh
# Fail-closed wrapper around the Bifrost image entrypoint.
#
# config.json reads the encryption key, the admin login and the gateway virtual
# key from the environment. Compose requires them (${VAR:?}), but a container
# started some other way would boot without them anyway, just broken: no
# encryption key stores provider keys unencrypted, an empty admin login leaves
# the UI and management API locked behind an unusable password, and an empty
# virtual key means every gateway request is refused. So check them here too,
# along with their format, and refuse to start instead.
set -eu

missing=""
[ -n "${BIFROST_ENCRYPTION_KEY:-}" ] || missing="$missing BIFROST_ENCRYPTION_KEY"
[ -n "${BIFROST_ADMIN_USERNAME:-}" ] || missing="$missing BIFROST_ADMIN_USERNAME"
[ -n "${BIFROST_ADMIN_PASSWORD:-}" ] || missing="$missing BIFROST_ADMIN_PASSWORD"
[ -n "${BIFROST_TEST_VK:-}" ] || missing="$missing BIFROST_TEST_VK"
if [ -n "$missing" ]; then
  echo "bifrost: refusing to start, required variable(s) not set:$missing" >&2
  exit 1
fi
# Bifrost derives its storage key from this passphrase with Argon2id and only
# warns below 16 bytes; a short one would protect stored provider keys poorly.
if [ "${#BIFROST_ENCRYPTION_KEY}" -lt 16 ]; then
  echo "bifrost: refusing to start, BIFROST_ENCRYPTION_KEY must be at least 16 characters" >&2
  exit 1
fi
# A config.json virtual key without the sk-bf- prefix is silently replaced by a
# random one, which would turn every gateway request into a 401.
case "$BIFROST_TEST_VK" in
  sk-bf-????????????????*) ;;
  *)
    echo "bifrost: refusing to start, BIFROST_TEST_VK must be sk-bf- followed by at least 16 characters" >&2
    exit 1
    ;;
esac

exec /app/docker-entrypoint.sh "$@"
