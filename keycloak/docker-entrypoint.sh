#!/bin/sh
# Start Keycloak in background, wait for it, configure clients, then keep running

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

# Wait for Keycloak to be ready, then authenticate
echo "[init] Waiting for Keycloak to start..."
AUTH_METHOD=""

for i in $(seq 1 60); do
  # Try admin credentials first (works on first boot, or when password matches .env)
  if /opt/keycloak/bin/kcadm.sh config credentials \
      --server http://localhost:8080 --realm master \
      --user "${KC_BOOTSTRAP_ADMIN_USERNAME:-admin}" \
      --password "${KC_BOOTSTRAP_ADMIN_PASSWORD:-admin}" 2>/dev/null; then
    AUTH_METHOD="admin"
    break
  fi

  # Fallback: service account (works even if admin password was changed via UI)
  if [ -n "${KEYCLOAK_SERVICE_CLIENT_SECRET}" ]; then
    if /opt/keycloak/bin/kcadm.sh config credentials \
        --server http://localhost:8080 --realm apihub \
        --client "${KEYCLOAK_SERVICE_CLIENT_ID:-apihub-service}" \
        --secret "${KEYCLOAK_SERVICE_CLIENT_SECRET}" 2>/dev/null; then
      AUTH_METHOD="service-account"
      break
    fi
  fi

  sleep 3
done

if [ -z "$AUTH_METHOD" ]; then
  echo "[init] WARNING: Could not authenticate with admin or service account. Skipping initialization."
else
  echo "[init] Keycloak is ready (auth: ${AUTH_METHOD})."

  # Role creation (admin auth only — on first boot)
  if [ "$AUTH_METHOD" = "admin" ]; then
    for ROLE in admin developer viewer; do
      /opt/keycloak/bin/kcadm.sh create roles -r apihub -s name=$ROLE 2>/dev/null \
        && echo "[init] Created role: $ROLE" \
        || echo "[init] Role '$ROLE' already exists"
    done
    echo "[init] Role initialization complete."

    # Ensure service account has manage-clients for future fallback auth
    SA_UUID=$(/opt/keycloak/bin/kcadm.sh get users -r apihub -q username=service-account-apihub-service --fields id 2>/dev/null \
      | grep '"id"' | head -1 | sed 's/.*"id" *: *"//;s/".*//')
    if [ -n "$SA_UUID" ]; then
      /opt/keycloak/bin/kcadm.sh add-roles -r apihub \
        --uuserId "$SA_UUID" --cclientid realm-management \
        --rolename manage-clients 2>/dev/null \
        && echo "[init] Granted manage-clients to service account"
    fi
  fi

  # Update apihub-ui client redirect URIs and web origins (works with either auth)
  CLIENT_UUID=$(/opt/keycloak/bin/kcadm.sh get clients -r apihub -q clientId=apihub-ui --fields id 2>/dev/null \
    | grep '"id"' | head -1 | sed 's/.*"id" *: *"//;s/".*//')
  if [ -n "$CLIENT_UUID" ]; then
    /opt/keycloak/bin/kcadm.sh update "clients/$CLIENT_UUID" -r apihub \
      -s "redirectUris=[\"${KEYCLOAK_REDIRECT_URI}\"]" \
      -s "webOrigins=[\"${KEYCLOAK_WEB_ORIGIN}\"]" 2>/dev/null \
      && echo "[init] Updated apihub-ui client: redirectUris=${KEYCLOAK_REDIRECT_URI}, webOrigins=${KEYCLOAK_WEB_ORIGIN}" \
      || echo "[init] WARNING: Failed to update apihub-ui client"
  else
    echo "[init] WARNING: apihub-ui client not found"
  fi
fi

# Keep Keycloak running in foreground
wait $KC_PID
