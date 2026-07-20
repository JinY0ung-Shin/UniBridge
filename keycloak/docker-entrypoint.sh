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
# LiteLLM's admin UI lives on LiteLLM's own HTTPS port; its SSO callback is
# fixed at {PROXY_BASE_URL}/sso/callback (see compose litellm env).
: "${LITELLM_REDIRECT_URI:=https://${HOST_IP}:${LITELLM_PORT:-4000}/sso/callback}"
export KEYCLOAK_REDIRECT_URI KEYCLOAK_WEB_ORIGIN GRAFANA_REDIRECT_URI LITELLM_REDIRECT_URI

KCADM=/opt/keycloak/bin/kcadm.sh

# Print the UUID of an apihub client by clientId (empty if absent).
# Requires an authenticated kcadm session.
client_uuid() {
  if command -v jq >/dev/null 2>&1; then
    "$KCADM" get clients -r apihub -q "clientId=$1" --fields id 2>/dev/null \
      | jq -r '.[0].id // empty'
  else
    "$KCADM" get clients -r apihub -q "clientId=$1" --fields id 2>/dev/null \
      | grep '"id"' | head -1 | sed 's/.*"id" *: *"//;s/".*//'
  fi
}

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
  LITELLM_SECRET_ESCAPED=$(printf '%s' "${LITELLM_OAUTH_CLIENT_SECRET}" | sed -e 's/[\\&|]/\\&/g')
  sed -e "s|\${KEYCLOAK_SERVICE_CLIENT_SECRET}|${SECRET_ESCAPED}|g" \
      -e "s|\${GRAFANA_OAUTH_CLIENT_SECRET}|${GRAFANA_SECRET_ESCAPED}|g" \
      -e "s|\${LITELLM_OAUTH_CLIENT_SECRET}|${LITELLM_SECRET_ESCAPED}|g" \
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

    # Grafana SSO client: --import-realm skips realms that already exist, so a
    # realm imported before this client was added never gets it from the
    # template. Create it here when missing, then keep redirect URI and secret
    # in sync with env on every boot.
    GRAFANA_CLIENT_UUID=$(client_uuid grafana)
    if [ -z "$GRAFANA_CLIENT_UUID" ] && [ -n "${GRAFANA_OAUTH_CLIENT_SECRET}" ]; then
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
      if [ $? -eq 0 ]; then
        GRAFANA_CLIENT_UUID=$(client_uuid grafana)
        echo "[init] Created grafana client (realm predates it)"
      else
        echo "[init] WARNING: Failed to create grafana client"
      fi
    fi
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
      echo "[init] NOTE: grafana client missing and GRAFANA_OAUTH_CLIENT_SECRET unset; set the secret to auto-create it"
    fi

    # LiteLLM SSO client: same create-if-missing + sync treatment as grafana,
    # plus the role wiring LiteLLM needs — a proxy_admin client role composited
    # into the realm admin role so admins carry a role=proxy_admin claim.
    LITELLM_CLIENT_UUID=$(client_uuid litellm)
    if [ -z "$LITELLM_CLIENT_UUID" ] && [ -n "${LITELLM_OAUTH_CLIENT_SECRET}" ]; then
      "$KCADM" create clients -r apihub -f - >/dev/null 2>&1 <<LITELLM_CLIENT_JSON
{
  "clientId": "litellm",
  "name": "LiteLLM Admin UI",
  "enabled": true,
  "publicClient": false,
  "secret": "${LITELLM_OAUTH_CLIENT_SECRET}",
  "serviceAccountsEnabled": false,
  "directAccessGrantsEnabled": false,
  "standardFlowEnabled": true,
  "redirectUris": ["${LITELLM_REDIRECT_URI}"],
  "webOrigins": [],
  "attributes": {"pkce.code.challenge.method": "S256"},
  "protocolMappers": [
    {
      "name": "litellm role",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-usermodel-client-role-mapper",
      "consentRequired": false,
      "config": {
        "usermodel.clientRoleMapping.clientId": "litellm",
        "claim.name": "role",
        "jsonType.label": "String",
        "multivalued": "false",
        "id.token.claim": "true",
        "access.token.claim": "true",
        "userinfo.token.claim": "true"
      }
    }
  ]
}
LITELLM_CLIENT_JSON
      if [ $? -eq 0 ]; then
        LITELLM_CLIENT_UUID=$(client_uuid litellm)
        echo "[init] Created litellm client (realm predates it)"
      else
        echo "[init] WARNING: Failed to create litellm client"
      fi
    fi
    if [ -n "$LITELLM_CLIENT_UUID" ]; then
      # Client role + composite, both idempotent (also heals partial
      # hand-made setups).
      if ! "$KCADM" get "clients/$LITELLM_CLIENT_UUID/roles/proxy_admin" -r apihub >/dev/null 2>&1; then
        "$KCADM" create "clients/$LITELLM_CLIENT_UUID/roles" -r apihub \
          -s name=proxy_admin \
          -s "description=LiteLLM Admin UI admin (surfaced as the role claim)" >/dev/null 2>&1 \
          && echo "[init] Created litellm client role proxy_admin" \
          || echo "[init] WARNING: Failed to create litellm client role proxy_admin"
      fi
      if ! "$KCADM" get roles/admin/composites -r apihub 2>/dev/null | grep -q '"proxy_admin"'; then
        # Composites on a realm role need manage-realm, which the service
        # account deliberately lacks — try anyway, then escalate to the
        # bootstrap admin and swap the kcadm session back afterwards.
        if "$KCADM" add-roles -r apihub --rname admin --cclientid litellm --rolename proxy_admin >/dev/null 2>&1; then
          echo "[init] Linked realm role admin -> litellm:proxy_admin"
        elif [ -n "${KC_BOOTSTRAP_ADMIN_USERNAME}" ] && [ -n "${KC_BOOTSTRAP_ADMIN_PASSWORD}" ] \
          && "$KCADM" config credentials --server http://localhost:8080 --realm master \
               --user "${KC_BOOTSTRAP_ADMIN_USERNAME}" --password "${KC_BOOTSTRAP_ADMIN_PASSWORD}" >/dev/null 2>&1 \
          && "$KCADM" add-roles -r apihub --rname admin --cclientid litellm --rolename proxy_admin >/dev/null 2>&1; then
          echo "[init] Linked realm role admin -> litellm:proxy_admin (via bootstrap admin)"
          "$KCADM" config credentials --server http://localhost:8080 --realm apihub \
            --client "${KEYCLOAK_SERVICE_CLIENT_ID:-apihub-service}" \
            --secret "${KEYCLOAK_SERVICE_CLIENT_SECRET}" >/dev/null 2>&1 || true
        else
          echo "[init] WARNING: Could not link realm role admin -> litellm:proxy_admin (needs manage-realm);"
          echo "[init]          add the composite in the Keycloak console (README 'LiteLLM admin UI SSO')"
        fi
      fi
      if [ -n "${LITELLM_OAUTH_CLIENT_SECRET}" ]; then
        /opt/keycloak/bin/kcadm.sh update "clients/$LITELLM_CLIENT_UUID" -r apihub \
          -s "redirectUris=[\"${LITELLM_REDIRECT_URI}\"]" \
          -s "secret=${LITELLM_OAUTH_CLIENT_SECRET}" 2>/dev/null \
          && echo "[init] Updated litellm client: redirectUris=${LITELLM_REDIRECT_URI} (secret synced)" \
          || echo "[init] WARNING: Failed to update litellm client"
      else
        /opt/keycloak/bin/kcadm.sh update "clients/$LITELLM_CLIENT_UUID" -r apihub \
          -s "redirectUris=[\"${LITELLM_REDIRECT_URI}\"]" 2>/dev/null \
          && echo "[init] Updated litellm client: redirectUris=${LITELLM_REDIRECT_URI}" \
          || echo "[init] WARNING: Failed to update litellm client"
      fi
    else
      echo "[init] NOTE: litellm client missing and LITELLM_OAUTH_CLIENT_SECRET unset; set the secret to auto-create it"
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
