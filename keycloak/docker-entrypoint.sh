#!/bin/sh
# Start Keycloak in background, wait for it, create roles, then keep running

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
