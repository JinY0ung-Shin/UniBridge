#!/bin/sh
# Start Keycloak in background, wait for it, update client settings, then keep running

# Derive redirect URI and web origin from HOST_IP + UNIBRIDGE_UI_PORT if not explicitly set
: "${HOST_IP:=localhost}"
: "${UNIBRIDGE_UI_PORT:=3000}"
: "${KEYCLOAK_HOSTNAME:=${HOST_IP}}"
: "${KEYCLOAK_REDIRECT_URI:=https://${HOST_IP}:${UNIBRIDGE_UI_PORT}/*}"
: "${KEYCLOAK_WEB_ORIGIN:=https://${HOST_IP}:${UNIBRIDGE_UI_PORT}}"
export KEYCLOAK_HOSTNAME KEYCLOAK_REDIRECT_URI KEYCLOAK_WEB_ORIGIN

# Substitute environment variables in realm template and write to import dir
TEMPLATE="/opt/init/realm-export.json.tpl"
IMPORT_DIR="/opt/keycloak/data/import"
mkdir -p "$IMPORT_DIR"
if command -v envsubst >/dev/null 2>&1 && [ -f "$TEMPLATE" ]; then
  envsubst < "$TEMPLATE" > "$IMPORT_DIR/realm-export.json"
  echo "[init] Environment variables substituted in realm-export.json"
else
  echo "[init] WARNING: envsubst not found or template missing; copying template as-is"
  cp "$TEMPLATE" "$IMPORT_DIR/realm-export.json" 2>/dev/null || true
fi

# Start mode: set KEYCLOAK_DEV_MODE=true for development (relaxed security)
if [ "${KEYCLOAK_DEV_MODE:-false}" = "true" ]; then
  echo "[init] Starting Keycloak in DEVELOPMENT mode"
  /opt/keycloak/bin/kc.sh start-dev --import-realm &
else
  echo "[init] Starting Keycloak in PRODUCTION mode"
  /opt/keycloak/bin/kc.sh start --import-realm --hostname=${KEYCLOAK_HOSTNAME} --hostname-strict=true --http-enabled=true &
fi
KC_PID=$!

# Wait for Keycloak + realm to be ready, then update client settings via service account
echo "[init] Waiting for Keycloak to start..."
for i in $(seq 1 60); do
  if [ -n "${KEYCLOAK_SERVICE_CLIENT_SECRET}" ] && \
     /opt/keycloak/bin/kcadm.sh config credentials \
       --server http://localhost:8080 --realm apihub \
       --client "${KEYCLOAK_SERVICE_CLIENT_ID:-apihub-service}" \
       --secret "${KEYCLOAK_SERVICE_CLIENT_SECRET}" 2>/dev/null; then

    echo "[init] Authenticated via service account."

    # Update apihub-ui client redirect URIs and web origins
    # kcadm.sh returns JSON array: [ { "id" : "uuid" } ]
    if command -v jq >/dev/null 2>&1; then
      CLIENT_UUID=$(/opt/keycloak/bin/kcadm.sh get clients -r apihub -q clientId=apihub-ui --fields id 2>/dev/null \
        | jq -r '.[0].id // empty')
    else
      CLIENT_UUID=$(/opt/keycloak/bin/kcadm.sh get clients -r apihub -q clientId=apihub-ui --fields id 2>/dev/null \
        | grep '"id"' | head -1 | sed 's/.*"id" *: *"//;s/".*//')
    fi
    if [ -n "$CLIENT_UUID" ]; then
      /opt/keycloak/bin/kcadm.sh update "clients/$CLIENT_UUID" -r apihub \
        -s "redirectUris=[\"${KEYCLOAK_REDIRECT_URI}\"]" \
        -s "webOrigins=[\"${KEYCLOAK_WEB_ORIGIN}\"]" 2>/dev/null \
        && echo "[init] Updated apihub-ui client: redirectUris=${KEYCLOAK_REDIRECT_URI}, webOrigins=${KEYCLOAK_WEB_ORIGIN}" \
        || echo "[init] WARNING: Failed to update apihub-ui client"
    else
      echo "[init] WARNING: apihub-ui client not found"
    fi

    break
  fi
  sleep 3
done

if [ "$i" -eq 60 ]; then
  echo "[init] WARNING: Timed out waiting for service account authentication (180s)."
  echo "[init] Check that KEYCLOAK_SERVICE_CLIENT_SECRET is set correctly in .env"
  echo "[init] Client redirect URIs were NOT updated."
fi

# Keep Keycloak running in foreground
wait $KC_PID
