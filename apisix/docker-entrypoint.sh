#!/bin/sh
CONFIG_SRC="/opt/apisix-config/config.yaml"
CONFIG_DST="/usr/local/apisix/conf/config.yaml"

cp "$CONFIG_SRC" "$CONFIG_DST"

if [ -n "$ETCD_PASSWORD" ] && [ -n "$ETCD_USERNAME" ]; then
  sed -i '/^  etcd:/a\    user: "'"$ETCD_USERNAME"'"\n    password: "'"$ETCD_PASSWORD"'"' "$CONFIG_DST"
  echo "[apisix-init] etcd auth credentials injected"
fi

echo "[apisix-init] Running apisix init..."
/usr/bin/apisix init 2>&1 || { echo "[apisix-init] ERROR: apisix init failed (exit $?)"; exit 1; }

echo "[apisix-init] Running apisix init_etcd..."
/usr/bin/apisix init_etcd 2>&1 || { echo "[apisix-init] ERROR: apisix init_etcd failed (exit $?)"; exit 1; }

rm -f /usr/local/apisix/conf/config_listen.sock
rm -f /usr/local/apisix/logs/worker_events.sock

echo "[apisix-init] Starting openresty..."
exec /usr/local/openresty/bin/openresty -p /usr/local/apisix -g 'daemon off;'
