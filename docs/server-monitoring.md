# Server (host) monitoring

UniBridge monitors arbitrary Linux servers — the machines your APIs run on, or
any host you operate — for reachability, disk, CPU, and memory, and raises
proactive alerts through the existing alert pipeline (per-resource 담당자 +
global 관리자, webhook/mail channel, alert history, and the Alert Status UI).

> Monitoring an API service's **traffic stats** (requests, error rate, latency)
> without routing it through the gateway uses the same registry + `file_sd`
> pipeline — see [api-metrics-convention.md](api-metrics-convention.md).

## How it works

```
[server] node_exporter:39100 ─┐
[server] node_exporter:39100 ─┤→ Prometheus scrape ──→ UniBridge alert_checker
[server] node_exporter:39100 ─┘   (job "nodes")          (instant_query per signal)
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
* Hosts that also register a **GPU exporter** address go into a second `file_sd`
  file (`gpus.json`) scraped by a second job (`gpu-nodes`) — same mechanism, same
  `host` label, still no reload. See [GPU monitoring](#gpu-monitoring-optional).
* The alert checker (the same ~60s loop that checks DB/NAS/route health) queries
  Prometheus for each host signal, compares against thresholds, and feeds the
  result through the shared alert-state machine and `dispatch_alert`. Everything
  on this page reuses UniBridge recipients, audit, and UI; no Alertmanager is
  involved in the host signals.

### The other alerting path

Host alerts (this page) are not the whole picture. UniBridge runs **two
complementary paths**, and both end in the same mailbox and the same alert
history:

| | Evaluated by | Covers | Recipients |
|---|---|---|---|
| **In-app checker** | unibridge-service, ~60s loop | registered resources: hosts, DBs, gateway routes, S3/NAS | resource 담당자 + 관리자 |
| **Prometheus rules → Alertmanager → webhook** | the Prometheus and Alertmanager containers | the platform itself: APISIX 5xx rate, unibridge-service reachability, metadata/Keycloak/LiteLLM DBs, missing audit writes, no active blue-green color | 관리자 |

The second path exists because the checker runs *inside* unibridge-service and
therefore cannot report its own death — `UniBridgeServiceDown` is precisely the
alert it can never raise. Separate containers can. Alertmanager POSTs the fired
rules to `/_api/internal/alertmanager`, which turns each into mail plus an alert
history entry with rule type `prometheus_alert`. The rules live in
[`prometheus/rules/unibridge-alerts.yml`](../prometheus/rules/unibridge-alerts.yml)
and the routing in
[`alertmanager/alertmanager.yml`](../alertmanager/alertmanager.yml).

> **`ALERTMANAGER_WEBHOOK_TOKEN` must be set for that path to deliver anything.**
> It is empty by default and the receiver then rejects every delivery with 503 —
> the alerts are visible in the Alertmanager UI but silently produce no mail. The
> app logs the reason once per process on the first rejected delivery.

Two wrinkles worth knowing:

* **The service-down mail depends on the service.** The webhook receiver is the
  thing that is down, so those alerts arrive on Alertmanager's next retry, once
  the app is back. Setting `ALERTMANAGER_SMTP_HOST` + `ALERTMANAGER_SMTP_TO`
  (full set in `.env.example`) adds a **direct-SMTP copy of just the two
  service-down alerts** that never touches the app. Off by default; when off, the
  fallback route and receiver are stripped from the rendered config.
* **In the blue-green stack, `up` can lie.** The `unibridge-service` scrape
  target is a DNS alias that *both* colors answer, so a dead active color with a
  live standby still scrapes `up == 1`. The `unibridge-service-colors` job scrapes
  each color by name, and `UniBridgeNoActiveInstance` fires when no instance
  reports `unibridge_active_instance == 1` — the edge serving a dead or demoted
  color, a state in which in-app background alerting is silent too. Those two
  per-color targets are expectedly **down** in the single-stack compose, where
  the names do not resolve; nothing alerts on that.

## Setup

### 1. Install node_exporter on each server

Pick the method that fits the host. Both expose node_exporter on `:39100`.

**Method A — systemd binary (works on any Linux host, incl. non-Docker).**
Run as root on the target host:

```bash
sudo ./scripts/install_node_exporter.sh            # defaults: v1.8.2, 0.0.0.0:39100
sudo ./scripts/install_node_exporter.sh 1.8.2 0.0.0.0:39100
```

**Method B — Docker Compose (for hosts already running Docker).**
Copy [`deploy/node-exporter/docker-compose.yml`](../deploy/node-exporter/docker-compose.yml)
to the host and:

```bash
docker compose up -d
curl -s http://localhost:39100/metrics | grep -m1 node_filesystem_avail_bytes
```

> The compose file runs node_exporter with `network_mode: host`, `pid: host`,
> and the host root bind-mounted at `/host` with `--path.rootfs=/host`. This is
> required — a plain container only sees its own namespaces and would report
> *container* metrics, not the host's. Don't drop those settings.

Then **open port 39100 from the central Prometheus host to the server** (host
firewall, Prometheus IP only — node_exporter has no auth):

```bash
sudo ufw allow from <PROMETHEUS_IP> to any port 39100 proto tcp
```

### 2. Register the host in UniBridge

UI → **Servers → Add server**, with `address = <host-ip>:39100`. Optionally set
per-host threshold overrides; leave them blank to inherit the global defaults.
The status column shows live `up`/`down` from Prometheus.

For disk capacity, each server can also set a comma-separated list of
node_exporter `mountpoint` labels to watch, for example:

```text
/,/data,/backup
```

When set, only those mountpoints are considered for that server's `server_disk`
and `server_disk_forecast` checks. When blank, the server inherits the global
`NODE_EXPORTER_DISK_MOUNTPOINTS` env value; if that is also blank, every real
non-pseudo filesystem counts and the most-full one drives the alert.

The server detail disk chart keeps the same mountpoint scope but displays each
returned `mountpoint` label as a separate line. Alerts still use the worst
selected filesystem per host so existing warning/critical behavior stays stable.

## Signals & thresholds

| alert_type             | Fires when                                              | Severity |
|------------------------|---------------------------------------------------------|----------|
| `server_down`          | node_exporter scrape is down / host unreachable         | critical |
| `server_disk`          | worst filesystem usage ≥ warn (≥ crit → critical)       | warn/crit|
| `server_disk_forecast` | disk projected to fill within the forecast horizon      | warning  |
| `server_cpu`           | CPU utilisation ≥ warn                                   | warning  |
| `server_mem`           | memory utilisation ≥ warn                               | warning  |

Hosts with a GPU exporter registered get three more signals — see
[GPU monitoring](#gpu-monitoring-optional).

Global defaults live in **Alert settings → Server thresholds**
(disk warn 80 / crit 90, CPU 90, memory 90, forecast 24h). Per-host overrides
live on each server. The disk-fill forecast uses Prometheus `predict_linear`
over a 6h window — a genuine "will fill within N hours" early warning rather
than a static threshold. Set the forecast horizon to 0 to disable it.

`server_disk` escalates: a warning re-fires as critical when usage crosses the
crit threshold. Set **Re-notify every N cycles** (`repeat_alert_after_cycles`)
to re-send a still-firing alert every N check cycles (0 = notify once per
transition).

**복구 판정 연속 성공 횟수** (`resolve_after_successes`) is the recovery-side
mirror of `trigger_after_failures`: an alert resolves only after that many
consecutive healthy cycles, and a single bad cycle restarts the streak. The
default is 5 — recovery is announced only after five consecutive healthy
cycles (about five minutes at the default check interval); set it to 1 to
resolve on the first healthy cycle, as older releases did. Raise it further on
hosts whose disk usage oscillates around the warn threshold — a write/delete
pipeline that repeatedly crosses 80% would otherwise mail a triggered/resolved
pair on every crossing.

## GPU monitoring (optional)

GPU monitoring is per host and off by default: a server is monitored for
disk/CPU/memory whether or not it has GPUs. To turn it on for a host, run
**NVIDIA dcgm-exporter** there and fill in the GPU exporter address in the same
**Servers** form (leave it empty = GPU monitoring off for that host).

### 1. Run dcgm-exporter on the GPU host

Prerequisites: the NVIDIA driver plus **nvidia-container-toolkit**, with the
Docker runtime configured (`sudo nvidia-ctk runtime configure --runtime=docker &&
sudo systemctl restart docker`). Copy
[`deploy/dcgm-exporter/docker-compose.yml`](../deploy/dcgm-exporter/docker-compose.yml)
to the host and:

```bash
docker compose up -d
curl -s http://localhost:39400/metrics | grep -m1 DCGM_FI_DEV_GPU_UTIL
```

Then open port 39400 from the central Prometheus host only — dcgm-exporter is
unauthenticated, same as node_exporter:

```bash
sudo ufw allow from <PROMETHEUS_IP> to any port 39400 proto tcp
```

> Docker is the supported path here. On a bare-metal host that doesn't run
> Docker there is no install script — you'd install DCGM and the dcgm-exporter
> binary manually and run it under systemd on `:39400` yourself.

### 2. Register the GPU address

UI → **Servers** → edit the host, set the GPU exporter address to
`<host-ip>:39400`. UniBridge writes it to the `gpus.json` `file_sd` file and the
`gpu-nodes` job picks it up within ~30s; no Prometheus reload.

### GPU signals

| alert_type        | Fires when                                                | Severity |
|-------------------|-----------------------------------------------------------|----------|
| `server_gpu_down` | dcgm-exporter scrape is down while the host itself is up  | critical |
| `server_gpu_util` | average GPU utilisation (across GPUs) ≥ threshold        | warning  |
| `server_gpu_mem`  | average GPU memory usage (across GPUs) ≥ threshold       | warning  |

`server_gpu_down` deliberately requires the host to be *up* — when a machine
dies you get one `server_down`, not a pair. Thresholds default to 90/90 in
**Alert settings → Server thresholds**, with per-host overrides on each server;
**0 disables** that check for the host.

> **GPU utilisation pinned at 100% is normal** on training nodes — that's the
> hardware doing its job, not an incident. On those hosts set the util threshold
> to 0 (off) or to something deliberately high; memory and `server_gpu_down` are
> the load-bearing signals there.

### Daily under-utilisation report

The GPU signals above all fire when usage is *too high*. The daily report is the
inverse: it surfaces GPU servers that sat idle, so wasted capacity gets noticed
instead of quietly costing money.

Once a day at **08:00 KST** UniBridge takes each GPU host's **trailing-24h
average GPU utilisation** (the mean across that host's cards, the same collapse
the alerts use) and mails the host's 담당자 plus the 관리자 for every host that
came in **below its target**. Hosts at or above target send nothing — a quiet
mailbox means the fleet is busy.

Targets work like the other thresholds, except the default is *off*:

| Setting                              | Where                                | Meaning                                  |
|--------------------------------------|--------------------------------------|------------------------------------------|
| `server_gpu_util_target_pct`         | **Alert settings → Server thresholds** | Global default. **0 = report off** everywhere. |
| `gpu_util_target_pct`                | **Servers** → per host                 | Empty = inherit the global default; **0 = report off** for that host. |

So a single global target of, say, 30% switches the report on for every GPU
host, and a host that is *meant* to idle (a spare, a dev box) opts out with a
per-host 0. The reverse also works: leave the global at 0 and set a target on
just the two hosts you care about.

The send hour is `GPU_UTIL_REPORT_HOUR_KST` (default `8`). KST is treated as a
fixed UTC+9 offset — Korea has no DST — so the container needs no tzdata.

Practical notes:

* **Downtime catches up.** The check runs on every checker cycle (~60s) and the
  once-a-day marker is a KST calendar date, so if UniBridge was down at 08:00
  the report still goes out later the same day — once, not once per cycle.
* **A mute skips that day's mail.** Muting a server suppresses its report the
  same way it suppresses its alerts, but nothing is queued: this is a report,
  not an incident, so there is no delayed re-fire when the mute lifts.
* **A host with no data is skipped** (with a log line) rather than reported as
  0% — an exporter that was down all day is already `server_gpu_down`'s job.
* **It appears in alert history** as a `report` entry with rule type
  `server_gpu_underutil`, alongside the triggered/resolved rows.

### Multi-GPU hosts

dcgm-exporter exposes one series **per GPU** (labels `gpu`, `UUID`,
`modelName`), and Prometheus records every one of them individually — nothing
is averaged at collection time. The two consumers then differ:

* **Alerts** evaluate the **average across the host's GPUs**, so a host
  produces one alert per signal, keyed `(alert_type, host)` like every other
  server signal. The flip side: one hot or nearly-full GPU among several idle
  ones barely moves the average — that case shows up in the per-GPU chart
  lines, and a lower per-host threshold can compensate on hosts where a single
  GPU matters.
* **Charts** on the server detail page draw one line per GPU, the same way the
  disk chart splits per `mountpoint` — the full per-GPU history stays
  queryable in Prometheus.

### Supported hardware

DCGM officially targets NVIDIA **datacenter** GPUs (A/H/L/Tesla series).
Consumer GeForce cards generally still report the basics UniBridge uses
(utilisation, memory, temperature) but are not officially supported — treat them
as best-effort.

## Push mode (firewalled hosts)

When the central Prometheus cannot reach a host's `:39100` (host behind a
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
default and is simpler; use push only for the hosts that need it. GPU hosts work
the same way — point the agent at the local dcgm-exporter too and keep the same
`host` external label.

## Not covered yet (future)

* **Windows servers** — add `windows_exporter` and a parallel scrape job; the
  evaluation queries assume node_exporter metric names.
* **AMD GPUs** — add the ROCm SMI exporter and a parallel scrape job; the GPU
  queries assume `DCGM_FI_*` metric names.
* **Container-level metrics** — add cAdvisor for per-container CPU/memory.
* **Alert grouping/correlation** — multiple signals firing on one host are
  currently independent alerts.
