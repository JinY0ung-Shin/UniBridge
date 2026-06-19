# Server (host) monitoring

UniBridge monitors arbitrary Linux servers — the machines your APIs run on, or
any host you operate — for reachability, disk, CPU, and memory, and raises
proactive alerts through the existing alert pipeline (per-resource 담당자 +
global 관리자, webhook/mail channel, alert history, and the Alert Status UI).

## How it works

```
[server] node_exporter:9100 ─┐
[server] node_exporter:9100 ─┤→ Prometheus scrape ──→ UniBridge alert_checker
[server] node_exporter:9100 ─┘   (job "nodes")          (instant_query per signal)
                                                              │
                                            AlertStateManager (debounce + severity)
                                                              │
                                              dispatch_alert → 담당자 + 관리자
                                                → webhook/mail + AlertHistory + UI
```

* Each registered host runs **node_exporter**; UniBridge writes the scrape
  targets into a Prometheus `file_sd` file (`/etc/prometheus/file_sd/nodes.json`,
  a shared volume) from the `MonitoredHost` registry — no Prometheus reload
  needed when you add/remove hosts.
* The alert checker (the same ~60s loop that checks DB/NAS/route health) queries
  Prometheus for each host signal, compares against thresholds, and feeds the
  result through the shared alert-state machine and `dispatch_alert`. No
  Alertmanager is required — alerts reuse UniBridge recipients, audit, and UI.

## Setup

### 1. Install node_exporter on each server

Run as root on the target host:

```bash
sudo ./scripts/install_node_exporter.sh            # defaults: v1.8.2, 0.0.0.0:9100
sudo ./scripts/install_node_exporter.sh 1.8.2 0.0.0.0:9100
```

Open port 9100 from the central Prometheus host to the server.

### 2. Register the host in UniBridge

UI → **Servers → Add server**, with `address = <host-ip>:9100`. Optionally set
per-host threshold overrides; leave them blank to inherit the global defaults.
The status column shows live `up`/`down` from Prometheus.

## Signals & thresholds

| alert_type             | Fires when                                              | Severity |
|------------------------|---------------------------------------------------------|----------|
| `server_down`          | node_exporter scrape is down / host unreachable         | critical |
| `server_disk`          | worst filesystem usage ≥ warn (≥ crit → critical)       | warn/crit|
| `server_disk_forecast` | disk projected to fill within the forecast horizon      | warning  |
| `server_cpu`           | CPU utilisation ≥ warn                                   | warning  |
| `server_mem`           | memory utilisation ≥ warn                               | warning  |

Global defaults live in **Alert settings → Server thresholds**
(disk warn 80 / crit 90, CPU 90, memory 90, forecast 24h). Per-host overrides
live on each server. The disk-fill forecast uses Prometheus `predict_linear`
over a 6h window — a genuine "will fill within N hours" early warning rather
than a static threshold. Set the forecast horizon to 0 to disable it.

`server_disk` escalates: a warning re-fires as critical when usage crosses the
crit threshold. Set **Re-notify every N cycles** (`repeat_alert_after_cycles`)
to re-send a still-firing alert every N check cycles (0 = notify once per
transition).

## Push mode (firewalled hosts)

When the central Prometheus cannot reach a host's `:9100` (host behind a
firewall/NAT), run a forwarding agent on the host that pushes metrics out
instead. Enable the remote-write receiver on Prometheus:

```yaml
# docker-compose.yml → prometheus.command
- '--web.enable-remote-write-receiver'
```

…and on the host run [grafana-agent](https://github.com/grafana/agent) or
[vmagent](https://docs.victoriametrics.com/vmagent.html) scraping local
node_exporter and remote-writing to `http(s)://<prometheus-host>:9090/api/v1/write`.
Add a `host` label in the agent's `external_labels` matching the name you
registered in UniBridge so the alert checker's queries line up. Pull mode is the
default and is simpler; use push only for the hosts that need it.

## Not covered yet (future)

* **Windows servers** — add `windows_exporter` and a parallel scrape job; the
  evaluation queries assume node_exporter metric names.
* **Container-level metrics** — add cAdvisor for per-container CPU/memory.
* **Alert grouping/correlation** — multiple signals firing on one host are
  currently independent alerts.
