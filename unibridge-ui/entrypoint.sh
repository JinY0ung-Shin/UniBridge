#!/bin/sh
# Generate runtime config from environment variables
LITELLM_ADMIN_URL="https://${HOST_IP:-localhost}:${LITELLM_PORT:-4000}/ui"
KEYCLOAK_URL="${KEYCLOAK_EXTERNAL_URL:-https://${HOST_IP:-localhost}:${KEYCLOAK_PORT:-8443}}"
KEYCLOAK_REALM_VALUE="${KEYCLOAK_REALM:-apihub}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_JWT_AUDIENCE:-apihub-ui}"

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

cat > /usr/share/nginx/html/runtime-config.js <<EOF
window.__RUNTIME_CONFIG__ = {
  LITELLM_ADMIN_URL: "$(json_escape "$LITELLM_ADMIN_URL")",
  KEYCLOAK_URL: "$(json_escape "$KEYCLOAK_URL")",
  KEYCLOAK_REALM: "$(json_escape "$KEYCLOAK_REALM_VALUE")",
  KEYCLOAK_CLIENT_ID: "$(json_escape "$KEYCLOAK_CLIENT_ID")"
};
EOF

exec nginx -g 'daemon off;'
