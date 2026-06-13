#!/bin/sh
# Generate runtime config from environment variables
LITELLM_ADMIN_URL="https://${HOST_IP:-localhost}:${LITELLM_PORT:-4000}/ui"
KEYCLOAK_URL="${KEYCLOAK_EXTERNAL_URL:-https://${HOST_IP:-localhost}:${KEYCLOAK_PORT:-8443}}"
KEYCLOAK_REALM_VALUE="${KEYCLOAK_REALM:-apihub}"
KEYCLOAK_CLIENT_ID="${KEYCLOAK_JWT_AUDIENCE:-apihub-ui}"
UNIBRIDGE_SERVICE_UPSTREAM="${UNIBRIDGE_SERVICE_UPSTREAM:-unibridge-service}"
APISIX_UPSTREAM="${APISIX_UPSTREAM:-apisix}"

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

sed_escape() {
  printf '%s' "$1" | sed 's/[\/&]/\\&/g'
}

cat > /usr/share/nginx/html/runtime-config.js <<EOF
window.__RUNTIME_CONFIG__ = {
  LITELLM_ADMIN_URL: "$(json_escape "$LITELLM_ADMIN_URL")",
  KEYCLOAK_URL: "$(json_escape "$KEYCLOAK_URL")",
  KEYCLOAK_REALM: "$(json_escape "$KEYCLOAK_REALM_VALUE")",
  KEYCLOAK_CLIENT_ID: "$(json_escape "$KEYCLOAK_CLIENT_ID")"
};
EOF

sed -i \
  -e "s/__UNIBRIDGE_SERVICE_UPSTREAM__/$(sed_escape "$UNIBRIDGE_SERVICE_UPSTREAM")/g" \
  -e "s/__APISIX_UPSTREAM__/$(sed_escape "$APISIX_UPSTREAM")/g" \
  /etc/nginx/conf.d/default.conf

exec nginx -g 'daemon off;'
