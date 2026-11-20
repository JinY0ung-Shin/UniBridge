#!/bin/sh
# Start Keycloak in background, wait for it, update client settings, then keep running

# Derive redirect URI and web origin from HOST_IP + UNIBRIDGE_UI_PORT if not explicitly set
: "${HOST_IP:=localhost}"
: "${UNIBRIDGE_UI_PORT:=3000}"
: "${KEYCLOAK_REDIRECT_URI:=https://${HOST_IP}:${UNIBRIDGE_UI_PORT}/*}"
: "${KEYCLOAK_WEB_ORIGIN:=https://${HOST_IP}:${UNIBRIDGE_UI_PORT}}"
# Grafana is served same-origin behind the UI/edge nginx at /grafana, so its
# OAuth redirect shares the UI's HTTPS endpoint (UNIBRIDGE_UI_PORT is the edge
# port in blue/green deployments — see compose keycloak env).
: "${GRAFANA_REDIRECT_URI:=https://${HOST_IP}:${UNIBRIDGE_UI_PORT}/grafana/*}"
export KEYCLOAK_REDIRECT_URI KEYCLOAK_WEB_ORIGIN GRAFANA_REDIRECT_URI

KCADM=/opt/keycloak/bin/kcadm.sh

# Print the UUID of an apihub client by clientId (empty if absent).
# Requires an authenticated kcadm session. kcadm.sh prints a JSON array,
# [ { "id" : "uuid" } ], and the image has no jq.
client_uuid() {
  "$KCADM" get clients -r apihub -q "clientId=$1" --fields id 2>/dev/null \
    | grep '"id"' | head -1 | sed 's/.*"id" *: *"//;s/".*//'
}

# Print the id of an apihub client scope by name (empty if absent).
client_scope_id() {
  "$KCADM" get client-scopes -r apihub --fields id,name --format csv --noquotes 2>/dev/null \
    | grep ",$1\$" | head -1 | cut -d, -f1
}

# Create the Grafana SSO client. Needs a kcadm session that may manage the
# realm: since Keycloak 26.8.2 a client administrator (the service account)
# gets 403 on a client that carries protocol mappers.
create_grafana_client() {
  "$KCADM" create clients -r apihub -f - >/dev/null 2>&1 <<GRAFANA_CLIENT_JSON
{
  "clientId": "grafana",
  "name": "Grafana",
  "enabled": true,
  "publicClient": false,
  "secret": "${GRAFANA_OAUTH_CLIENT_SECRET}",
  "serviceAccountsEnabled": false,
  "directAccessGrantsEnabled": false,
  "standardFlowEnabled": true,
  "redirectUris": ["${GRAFANA_REDIRECT_URI}"],
  "webOrigins": [],
  "attributes": {"pkce.code.challenge.method": "S256"},
  "protocolMappers": [
    {
      "name": "realm roles",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-usermodel-realm-role-mapper",
      "consentRequired": false,
      "config": {
        "claim.name": "realm_access.roles",
        "jsonType.label": "String",
        "multivalued": "true",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true"
      }
    }
  ]
}
GRAFANA_CLIENT_JSON
}

# Substitute environment variables in realm template and write to import dir
TEMPLATE="/opt/init/realm-export.json.tpl"
IMPORT_DIR="/opt/keycloak/data/import"
mkdir -p "$IMPORT_DIR"
if [ ! -f "$TEMPLATE" ]; then
  echo "[init] WARNING: realm template missing at $TEMPLATE"
else
  # The image has no envsubst, so sed fills in the template's two placeholders.
  # Escape sed replacement metachars (\, &, |) in the secrets before substitution.
  SECRET_ESCAPED=$(printf '%s' "${KEYCLOAK_SERVICE_CLIENT_SECRET}" | sed -e 's/[\\&|]/\\&/g')
  GRAFANA_SECRET_ESCAPED=$(printf '%s' "${GRAFANA_OAUTH_CLIENT_SECRET}" | sed -e 's/[\\&|]/\\&/g')
  sed -e "s|\${KEYCLOAK_SERVICE_CLIENT_SECRET}|${SECRET_ESCAPED}|g" \
      -e "s|\${GRAFANA_OAUTH_CLIENT_SECRET}|${GRAFANA_SECRET_ESCAPED}|g" \
    "$TEMPLATE" > "$IMPORT_DIR/realm-export.json"
  echo "[init] Environment variables substituted in realm-export.json"
fi

# Start mode: set KEYCLOAK_DEV_MODE=true for development (relaxed security)
if [ "${KEYCLOAK_DEV_MODE:-false}" = "true" ]; then
  echo "[init] Starting Keycloak in DEVELOPMENT mode"
  /opt/keycloak/bin/kc.sh start-dev --import-realm --http-host=0.0.0.0 &
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

    # Two things need the bootstrap admin, after which the service-account
    # kcadm session is restored.
    if [ -n "${KC_BOOTSTRAP_ADMIN_USERNAME}" ] && [ -n "${KC_BOOTSTRAP_ADMIN_PASSWORD}" ] \
      && "$KCADM" config credentials --server http://localhost:8080 --realm master \
           --user "${KC_BOOTSTRAP_ADMIN_USERNAME}" --password "${KC_BOOTSTRAP_ADMIN_PASSWORD}" >/dev/null 2>&1; then
      # Keep session policy deterministic even when the realm already existed:
      # startup realm import skips existing realms.
      if "$KCADM" update realms/apihub \
           -s accessTokenLifespan=300 \
           -s ssoSessionIdleTimeout=86400 \
           -s ssoSessionMaxLifespan=604800 >/dev/null 2>&1; then
        echo "[init] Synced session policy: access=5m, idle=24h, max=7d"
      else
        echo "[init] WARNING: Failed to sync Keycloak session policy"
      fi
      # Keycloak 26.8.2 and later answer 401 on the Admin API unless the token
      # carries the built-in kc-admin-api client scope. The upgrade gives it to
      # Keycloak's own clients only, so the service client gets it here: without
      # it the client sync below and UniBridge's user management both fail.
      ADMIN_API_SCOPE_ID=$(client_scope_id kc-admin-api)
      SERVICE_CLIENT_UUID=$(client_uuid "${KEYCLOAK_SERVICE_CLIENT_ID:-apihub-service}")
      if [ -n "$ADMIN_API_SCOPE_ID" ] && [ -n "$SERVICE_CLIENT_UUID" ]; then
        "$KCADM" update "clients/$SERVICE_CLIENT_UUID/default-client-scopes/$ADMIN_API_SCOPE_ID" \
          -r apihub >/dev/null 2>&1 \
          && echo "[init] Service client carries the kc-admin-api scope" \
          || echo "[init] WARNING: Failed to give the service client the kc-admin-api scope"
      fi
      # --import-realm skips realms that already exist, so a realm imported
      # before the Grafana client was added never gets it from the template.
      if [ -z "$(client_uuid grafana)" ] && [ -n "${GRAFANA_OAUTH_CLIENT_SECRET}" ]; then
        create_grafana_client \
          && echo "[init] Created grafana client (realm predates it)" \
          || echo "[init] WARNING: Failed to create grafana client"
      fi
    else
      echo "[init] WARNING: Bootstrap admin login failed: session policy, the kc-admin-api scope and a missing grafana client were not synced"
    fi
    "$KCADM" config credentials --server http://localhost:8080 --realm apihub \
      --client "${KEYCLOAK_SERVICE_CLIENT_ID:-apihub-service}" \
      --secret "${KEYCLOAK_SERVICE_CLIENT_SECRET}" >/dev/null 2>&1 || true
    if ! "$KCADM" get clients -r apihub -q clientId=apihub-ui --fields id >/dev/null 2>&1; then
      echo "[init] WARNING: The service account cannot call the Admin API. Add the kc-admin-api"
      echo "[init]   client scope to ${KEYCLOAK_SERVICE_CLIENT_ID:-apihub-service} as a default scope (README, Upgrading Keycloak)."
    fi

    # Update apihub-ui client redirect URIs and web origins
    CLIENT_UUID=$(client_uuid apihub-ui)
    if [ -n "$CLIENT_UUID" ]; then
      /opt/keycloak/bin/kcadm.sh update "clients/$CLIENT_UUID" -r apihub \
        -s "redirectUris=[\"${KEYCLOAK_REDIRECT_URI}\"]" \
        -s "webOrigins=[\"${KEYCLOAK_WEB_ORIGIN}\"]" 2>/dev/null \
        && echo "[init] Updated apihub-ui client: redirectUris=${KEYCLOAK_REDIRECT_URI}, webOrigins=${KEYCLOAK_WEB_ORIGIN}" \
        || echo "[init] WARNING: Failed to update apihub-ui client"
    else
      echo "[init] WARNING: apihub-ui client not found"
    fi

    # Grafana SSO client (created above when the realm predates it): keep
    # redirect URI and secret in sync with env on every boot.
    GRAFANA_CLIENT_UUID=$(client_uuid grafana)
    if [ -n "$GRAFANA_CLIENT_UUID" ]; then
      # Sync the secret too (not just redirect URIs) so rotating
      # GRAFANA_OAUTH_CLIENT_SECRET in .env — or hand-creating the client on a
      # pre-existing realm with a console-generated secret — converges on the
      # env value at the next boot instead of failing token exchange.
      if [ -n "${GRAFANA_OAUTH_CLIENT_SECRET}" ]; then
        /opt/keycloak/bin/kcadm.sh update "clients/$GRAFANA_CLIENT_UUID" -r apihub \
          -s "redirectUris=[\"${GRAFANA_REDIRECT_URI}\"]" \
          -s "secret=${GRAFANA_OAUTH_CLIENT_SECRET}" 2>/dev/null \
          && echo "[init] Updated grafana client: redirectUris=${GRAFANA_REDIRECT_URI} (secret synced)" \
          || echo "[init] WARNING: Failed to update grafana client"
      else
        /opt/keycloak/bin/kcadm.sh update "clients/$GRAFANA_CLIENT_UUID" -r apihub \
          -s "redirectUris=[\"${GRAFANA_REDIRECT_URI}\"]" 2>/dev/null \
          && echo "[init] Updated grafana client: redirectUris=${GRAFANA_REDIRECT_URI}" \
          || echo "[init] WARNING: Failed to update grafana client"
      fi
    else
      echo "[init] NOTE: grafana client missing; it is created at boot when GRAFANA_OAUTH_CLIENT_SECRET is set and the bootstrap admin can log in"
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
