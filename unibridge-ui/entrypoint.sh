#!/bin/sh
# Fail fast: a silent sed/template failure here would otherwise leave nginx
# serving unsubstituted "__…__" placeholders, which only surface as 502s at
# request time.
set -eu

# Generate runtime config from environment variables
LITELLM_ADMIN_URL="https://${HOST_IP:-localhost}:${LITELLM_PORT:-4000}/ui"
# Bifrost has no TLS of its own; the bifrost-tls nginx publishes its UI.
BIFROST_ADMIN_URL="https://${HOST_IP:-localhost}:${BIFROST_UI_PORT:-18443}"
# Optional DNS name on which this nginx also serves the Bifrost UI, on its own
# port (nginx-bifrost-ui.conf); the Bifrost button then links there instead.
BIFROST_UI_HOSTNAME="${BIFROST_UI_HOSTNAME:-}"
# Same-origin path proxied by this nginx (see nginx.conf /grafana/); override
# with GRAFANA_EXTERNAL_URL when Grafana lives behind a different endpoint.
GRAFANA_URL="${GRAFANA_EXTERNAL_URL:-/grafana}"
GRAFANA_UPSTREAM="${GRAFANA_UPSTREAM:-grafana}"
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

# BIFROST_UI_HOSTNAME becomes an nginx server_name, so only a DNS host name
# passes: no port, scheme, wildcard or anything else nginx would parse. An IP
# address would take the requests UniBridge itself gets on that address, so
# the last label has to start with a letter, as top-level domains do.
valid_hostname() {
  case "$1" in
    "" | *[!A-Za-z0-9.-]*) return 1 ;;
  esac
  [ "${#1}" -le 253 ] &&
    printf '%s\n' "$1" |
    grep -Eqx '([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
}

lowercase() {
  printf '%s' "$1" | tr 'A-Z' 'a-z'
}

if [ -n "$BIFROST_UI_HOSTNAME" ]; then
  if ! valid_hostname "$BIFROST_UI_HOSTNAME"; then
    echo "entrypoint: BIFROST_UI_HOSTNAME must be a DNS host name (no IP address, port or scheme): $BIFROST_UI_HOSTNAME" >&2
    exit 1
  fi
  # HOST_IP may be a host name too; the same name would hand UniBridge's own
  # requests to Bifrost.
  if [ "$(lowercase "$BIFROST_UI_HOSTNAME")" = "$(lowercase "${HOST_IP:-localhost}")" ]; then
    echo "entrypoint: BIFROST_UI_HOSTNAME must differ from HOST_IP, the name UniBridge itself answers on: $BIFROST_UI_HOSTNAME" >&2
    exit 1
  fi
fi

cat > /usr/share/nginx/html/runtime-config.js <<EOF
window.__RUNTIME_CONFIG__ = {
  LITELLM_ADMIN_URL: "$(json_escape "$LITELLM_ADMIN_URL")",
  BIFROST_ADMIN_URL: "$(json_escape "$BIFROST_ADMIN_URL")",
  BIFROST_UI_HOSTNAME: "$(json_escape "$BIFROST_UI_HOSTNAME")",
  GRAFANA_URL: "$(json_escape "$GRAFANA_URL")",
  KEYCLOAK_URL: "$(json_escape "$KEYCLOAK_URL")",
  KEYCLOAK_REALM: "$(json_escape "$KEYCLOAK_REALM_VALUE")",
  KEYCLOAK_CLIENT_ID: "$(json_escape "$KEYCLOAK_CLIENT_ID")"
};
EOF

sed -i \
  -e "s/__UNIBRIDGE_SERVICE_UPSTREAM__/$(sed_escape "$UNIBRIDGE_SERVICE_UPSTREAM")/g" \
  -e "s/__APISIX_UPSTREAM__/$(sed_escape "$APISIX_UPSTREAM")/g" \
  -e "s/__GRAFANA_UPSTREAM__/$(sed_escape "$GRAFANA_UPSTREAM")/g" \
  /etc/nginx/conf.d/default.conf

# The template sits outside conf.d, so the Bifrost server exists only when
# BIFROST_UI_HOSTNAME is set (validated above, so it is safe in sed and nginx).
if [ -n "$BIFROST_UI_HOSTNAME" ]; then
  sed "s/__BIFROST_UI_HOSTNAME__/$BIFROST_UI_HOSTNAME/g" \
    /etc/nginx/bifrost-ui.conf.template > /etc/nginx/conf.d/bifrost-ui.conf
fi

# Catch both a failed substitution (leftover placeholder) and any resulting
# invalid config before handing off to nginx, so the container fails loudly
# instead of booting with a broken proxy.
if grep -q '__UNIBRIDGE_SERVICE_UPSTREAM__\|__APISIX_UPSTREAM__\|__GRAFANA_UPSTREAM__\|__BIFROST_UI_HOSTNAME__' /etc/nginx/conf.d/*.conf; then
  echo "entrypoint: placeholders were not substituted in /etc/nginx/conf.d" >&2
  exit 1
fi
nginx -t

exec nginx -g 'daemon off;'
