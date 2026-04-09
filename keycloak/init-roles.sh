#!/bin/sh
set -e

KCADM="/opt/keycloak/bin/kcadm.sh"
SERVER="https://localhost:8443"
REALM="apihub"

echo "Waiting for Keycloak to be ready..."
until $KCADM config credentials --server $SERVER --realm master \
  --user "${KC_ADMIN_USER:-admin}" --password "${KC_ADMIN_PASSWORD:-admin}" \
  --truststore-disabled 2>/dev/null; do
  sleep 3
done

echo "Creating roles..."
for ROLE in admin developer viewer; do
  $KCADM create roles -r $REALM -s name=$ROLE --truststore-disabled 2>/dev/null || echo "Role '$ROLE' already exists"
done

echo "Done. Roles initialized."
