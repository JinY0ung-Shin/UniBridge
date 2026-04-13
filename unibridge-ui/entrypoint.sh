#!/bin/sh
# Generate runtime config from environment variables
LITELLM_ADMIN_URL="https://${HOST_IP:-localhost}:${LITELLM_PORT:-4000}/ui"

cat > /usr/share/nginx/html/runtime-config.js <<EOF
window.__RUNTIME_CONFIG__ = {
  LITELLM_ADMIN_URL: "${LITELLM_ADMIN_URL}"
};
EOF

exec nginx -g 'daemon off;'
