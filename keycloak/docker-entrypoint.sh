#!/bin/sh
# Start Keycloak in background, wait for it, create roles, then keep running

# Derive redirect URI and web origin from HOST_IP + QUERY_UI_PORT if not explicitly set
: "${HOST_IP:=localhost}"
: "${QUERY_UI_PORT:=3000}"
: "${KEYCLOAK_REDIRECT_URI:=https://${HOST_IP}:${QUERY_UI_PORT}/*}"
: "${KEYCLOAK_WEB_ORIGIN:=https://${HOST_IP}:${QUERY_UI_PORT}}"
export KEYCLOAK_REDIRECT_URI KEYCLOAK_WEB_ORIGIN

# Substitute environment variables in realm-export.json before import
IMPORT_DIR="/opt/keycloak/data/import"
if command -v envsubst >/dev/null 2>&1 && [ -f "$IMPORT_DIR/realm-export.json" ]; then
  envsubst < "$IMPORT_DIR/realm-export.json" > "$IMPORT_DIR/realm-export-resolved.json"
  mv "$IMPORT_DIR/realm-export-resolved.json" "$IMPORT_DIR/realm-export.json"
  echo "[init] Environment variables substituted in realm-export.json"
fi

/opt/keycloak/bin/kc.sh start-dev --import-realm &
KC_PID=$!

# Wait for Keycloak to be ready
echo "[init] Waiting for Keycloak to start..."
for i in $(seq 1 60); do
  if /opt/keycloak/bin/kcadm.sh config credentials \
    --server http://localhost:8080 --realm master \
    --user "${KC_BOOTSTRAP_ADMIN_USERNAME:-admin}" \
    --password "${KC_BOOTSTRAP_ADMIN_PASSWORD:-admin}" 2>/dev/null; then
    echo "[init] Keycloak is ready. Creating roles..."
    for ROLE in admin developer viewer; do
      /opt/keycloak/bin/kcadm.sh create roles -r apihub -s name=$ROLE 2>/dev/null \
        && echo "[init] Created role: $ROLE" \
        || echo "[init] Role '$ROLE' already exists"
    done
    echo "[init] Role initialization complete."
    break
  fi
  sleep 3
done

# Keep Keycloak running in foreground
wait $KC_PID
