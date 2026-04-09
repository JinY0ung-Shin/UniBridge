#!/bin/sh
set -e

KCADM="/opt/keycloak/bin/kcadm.sh"
SERVER="https://keycloak:8443"
REALM="apihub"

echo "Waiting for Keycloak to be ready..."
for i in $(seq 1 30); do
  if $KCADM config credentials --server $SERVER --realm master \
    --user "${KC_ADMIN_USER:-admin}" --password "${KC_ADMIN_PASSWORD:-admin}" \
    --truststore /opt/keycloak/conf/tls.crt 2>&1; then
    echo "Authenticated to Keycloak."
    break
  fi
  echo "Attempt $i failed, retrying in 5s..."
  sleep 5
done

echo "Creating roles..."
for ROLE in admin developer viewer; do
  $KCADM create roles -r $REALM -s name=$ROLE \
    --truststore /opt/keycloak/conf/tls.crt 2>&1 || echo "Role '$ROLE' already exists"
done

echo "Done. Roles initialized."
