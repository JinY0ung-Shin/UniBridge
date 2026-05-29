#!/usr/bin/env bash
#
# Enable APPROVAL-GATED self-registration on the apihub realm.
#
# Trust model: anyone may register, but a newly registered user gets NO application
# role and therefore cannot use the app (the backend rejects role-less tokens with
# 401, and the UI shows a "pending approval" screen). An admin approves the user by
# assigning the `user` role from the UI Users page.
#
# Idempotent. Applies to the LIVE realm (realm-export.json is only read on first
# realm creation). Run once on the Docker host; safe to re-run.
#
# What it does (in this order, so registration is never open while it auto-grants):
#   1. ensure `user` is NOT in the default-roles-<realm> composite (remove if present)
#      -> new users stay "pending" until an admin assigns a role
#   2. realms/<realm>: registrationAllowed = true (+ bruteForceProtected = true)
#
# Usage:
#   ./keycloak/enable-self-registration.sh
#   KC_CONTAINER=unibridge-keycloak-1 KC_REALM=apihub ./keycloak/enable-self-registration.sh
#
# Auth: uses the Keycloak master-admin credentials already present inside the
# container (KC_BOOTSTRAP_ADMIN_USERNAME / KC_BOOTSTRAP_ADMIN_PASSWORD, i.e. your
# KC_ADMIN_USER / KC_ADMIN_PASSWORD). The service account lacks manage-realm and
# cannot do this. If the master-admin creds no longer match (password rotated), the
# admin-console fallback is printed.

set -euo pipefail

REALM="${KC_REALM:-apihub}"

# 1) Locate the Keycloak app container (exclude the *-db container).
KC="${KC_CONTAINER:-}"
if [ -z "$KC" ]; then
  matches="$(docker ps --format '{{.Names}}' | grep -i keycloak | grep -vi 'keycloak-db' || true)"
  n="$(printf '%s\n' "$matches" | grep -c . || true)"
  if [ "$n" -eq 0 ]; then
    echo "ERROR: no running Keycloak container found. Set KC_CONTAINER=<name>." >&2
    exit 1
  elif [ "$n" -gt 1 ]; then
    echo "ERROR: multiple Keycloak containers found; set KC_CONTAINER to pick one:" >&2
    printf '%s\n' "$matches" | sed 's/^/  - /' >&2
    exit 1
  fi
  KC="$matches"
fi
echo "[*] Keycloak container: $KC   realm: $REALM"

# 2) Apply idempotently inside the container, authenticating as master admin.
docker exec -i -e SELFREG_REALM="$REALM" "$KC" sh <<'INNER'
set -e
REALM="${SELFREG_REALM:-apihub}"
KCADM=/opt/keycloak/bin/kcadm.sh
# Preflight: confirm this really is the Keycloak app container (auto-discovery
# matches any name containing "keycloak"). Fail clearly instead of a cryptic
# "no such file" from a sidecar/exporter/db container.
if [ ! -x "$KCADM" ]; then
  echo "ERROR: $KCADM not found in this container — it does not look like the Keycloak app container." >&2
  echo "       Set KC_CONTAINER=<name> to target the right one." >&2
  exit 4
fi
# Isolated kcadm config so the admin bearer token does not persist in the
# long-lived container's default ~/.keycloak/kcadm.config after we exit.
KCCONF="/tmp/kcadm-selfreg-$$.config"
trap 'rm -f "$KCCONF"' EXIT INT TERM
# --config must follow the subcommand (kcadm rejects it as a pre-command global),
# so append it after the caller's args.
KC() { "$KCADM" "$@" --config "$KCCONF"; }

if ! KC config credentials --server http://localhost:8080 --realm master \
      --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1; then
  echo "ERROR: master-admin auth failed (KC_BOOTSTRAP_ADMIN_* may not match the running admin password)." >&2
  echo "Fallback — apply it in the admin console UI instead:" >&2
  echo "  - Realm roles -> default-roles-$REALM -> Action -> Remove associated roles -> uncheck 'user'" >&2
  echo "  - Realm settings -> Login tab -> User registration = ON" >&2
  exit 2
fi

# Approval model: make sure 'user' is NOT auto-granted via default roles, so new
# users register in a role-less "pending" state until an admin assigns a role.
if KC get-roles -r "$REALM" --rname "default-roles-$REALM" --fields name 2>/dev/null \
     | grep -qE '"name"[[:space:]]*:[[:space:]]*"user"'; then
  echo "[*] Removing 'user' from default-roles-$REALM (new users stay pending)..."
  KC remove-roles -r "$REALM" --rname "default-roles-$REALM" --rolename user
else
  echo "[*] 'user' already absent from default-roles-$REALM (approval-gated)."
fi

echo "[*] Enabling registration (+ bruteForceProtected) on realm '$REALM'..."
KC update "realms/$REALM" -s registrationAllowed=true -s bruteForceProtected=true

echo ""
echo "=== verification ==="
echo "- realm flags:"
KC get "realms/$REALM" --fields registrationAllowed,bruteForceProtected
echo "- default-roles-$REALM realm composites (must NOT include 'user'):"
KC get-roles -r "$REALM" --rname "default-roles-$REALM" --fields name
INNER

echo ""
echo "[OK] Done. Anyone can register, but each new user stays PENDING until an admin"
echo "     assigns them a role on the UI Users page."
