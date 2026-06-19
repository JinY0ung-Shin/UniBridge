#!/usr/bin/env bash
#
# install_node_exporter.sh — install/upgrade Prometheus node_exporter as a
# systemd service on a Linux host so UniBridge can monitor it.
#
# Usage (run as root on the target server):
#   ./install_node_exporter.sh [VERSION] [LISTEN_ADDR]
#
#   VERSION      node_exporter release, default 1.8.2
#   LISTEN_ADDR  bind address, default 0.0.0.0:9100
#
# After it runs, register the host in UniBridge (Servers → Add server) with
# address "<this-host-ip>:9100". The central Prometheus must be able to reach
# that address (open the port from Prometheus → host). For hosts behind a
# firewall that Prometheus cannot reach, use push mode instead — see
# docs/server-monitoring.md.
set -euo pipefail

VERSION="${1:-1.8.2}"
LISTEN_ADDR="${2:-0.0.0.0:9100}"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  GOARCH="amd64" ;;
  aarch64) GOARCH="arm64" ;;
  armv7l)  GOARCH="armv7" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "This script must run as root (sudo)." >&2
  exit 1
fi

TARBALL="node_exporter-${VERSION}.linux-${GOARCH}.tar.gz"
URL="https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/${TARBALL}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> Downloading ${URL}"
curl -fsSL "$URL" -o "${TMP}/${TARBALL}"
tar -xzf "${TMP}/${TARBALL}" -C "$TMP"
install -m 0755 "${TMP}/node_exporter-${VERSION}.linux-${GOARCH}/node_exporter" /usr/local/bin/node_exporter

if ! id node_exporter >/dev/null 2>&1; then
  echo "==> Creating node_exporter system user"
  useradd --no-create-home --shell /usr/sbin/nologin node_exporter
fi

echo "==> Writing systemd unit"
cat > /etc/systemd/system/node_exporter.service <<EOF
[Unit]
Description=Prometheus node_exporter
Wants=network-online.target
After=network-online.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
ExecStart=/usr/local/bin/node_exporter --web.listen-address=${LISTEN_ADDR}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling and starting node_exporter"
systemctl daemon-reload
systemctl enable --now node_exporter
systemctl --no-pager status node_exporter | head -n 5 || true

echo
echo "node_exporter ${VERSION} listening on ${LISTEN_ADDR}."
echo "Verify:  curl -s http://localhost:${LISTEN_ADDR##*:}/metrics | head"
echo "Then register this host in UniBridge → Servers with address <ip>:${LISTEN_ADDR##*:}"
