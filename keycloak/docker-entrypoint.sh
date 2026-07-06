#!/bin/sh
# Start Keycloak in background, wait for it, update client settings, then keep running

# Derive redirect URI and web origin from HOST_IP + UNIBRIDGE_UI_PORT if not explicitly set
: "${HOST_IP:=localhost}"
: "${UNIBRIDGE_UI_PORT:=3000}"
: "${KEYCLOAK_REDIRECT_URI:=https://${HOST_IP}:${UNIBRIDGE_UI_PORT}/*}"
: "${KEYCLOAK_WEB_ORIGIN:=https://${HOST_IP}:${UNIBRIDGE_UI_PORT}}"
: "${GRAFANA_PORT:=3300}"
: "${GRAFANA_REDIRECT_URI:=http://${HOST_IP}:${GRAFANA_PORT}/*}"
export KEYCLOAK_REDIRECT_URI KEYCLOAK_WEB_ORIGIN GRAFANA_REDIRECT_URI

# Substitute environment variables in realm template and write to import dir
TEMPLATE="/opt/init/realm-export.json.tpl"
IMPORT_DIR="/opt/keycloak/data/import"
mkdir -p "$IMPORT_DIR"
if [ ! -f "$TEMPLATE" ]; then
  echo "[init] WARNING: realm template missing at $TEMPLATE"
elif command -v envsubst >/dev/null 2>&1; then
  envsubst < "$TEMPLATE" > "$IMPORT_DIR/realm-export.json"
  echo "[init] Environment variables substituted in realm-export.json (envsubst)"
else
  # envsubst is not present in keycloak's ubi-micro image; fall back to sed.
  # Escape sed replacement metachars (\, &, |) in the secrets before substitution.
  SECRET_ESCAPED=$(printf '%s' "${KEYCLOAK_SERVICE_CLIENT_SECRET}" | sed -e 's/[\\&|]/\\&/g')
  GRAFANA_SECRET_ESCAPED=$(printf '%s' "${GRAFANA_OAUTH_CLIENT_SECRET}" | sed -e 's/[\\&|]/\\&/g')
  sed -e "s|\${KEYCLOAK_SERVICE_CLIENT_SECRET}|${SECRET_ESCAPED}|g" \
      -e "s|\${GRAFANA_OAUTH_CLIENT_SECRET}|${GRAFANA_SECRET_ESCAPED}|g" \
    "$TEMPLATE" > "$IMPORT_DIR/realm-export.json"
  echo "[init] Environment variables substituted in realm-export.json (sed fallback)"
fi

# Start mode: set KEYCLOAK_DEV_MODE=true for development (relaxed security)
if [ "${KEYCLOAK_DEV_MODE:-false}" = "true" ]; then
  echo "[init] Starting Keycloak in DEVELOPMENT mode"
  /opt/keycloak/bin/kc.sh start-dev --import-realm &
else
  echo "[init] Starting Keycloak in PRODUCTION mode"
  /opt/keycloak/bin/kc.sh start --import-realm --hostname-strict=false --http-enabled=true &
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

    # Keep the grafana client's redirect URI in sync with HOST_IP/GRAFANA_PORT
    # (same pattern as apihub-ui above; the realm import only runs once).
    if command -v jq >/dev/null 2>&1; then
      GRAFANA_CLIENT_UUID=$(/opt/keycloak/bin/kcadm.sh get clients -r apihub -q clientId=grafana --fields id 2>/dev/null \
        | jq -r '.[0].id // empty')
    else
      GRAFANA_CLIENT_UUID=$(/opt/keycloak/bin/kcadm.sh get clients -r apihub -q clientId=grafana --fields id 2>/dev/null \
        | grep '"id"' | head -1 | sed 's/.*"id" *: *"//;s/".*//')
    fi
    if [ -n "$GRAFANA_CLIENT_UUID" ]; then
      /opt/keycloak/bin/kcadm.sh update "clients/$GRAFANA_CLIENT_UUID" -r apihub \
        -s "redirectUris=[\"${GRAFANA_REDIRECT_URI}\"]" 2>/dev/null \
        && echo "[init] Updated grafana client: redirectUris=${GRAFANA_REDIRECT_URI}" \
        || echo "[init] WARNING: Failed to update grafana client"
    else
      echo "[init] NOTE: grafana client not found (pre-existing realm import); create it to enable Grafana SSO"
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
