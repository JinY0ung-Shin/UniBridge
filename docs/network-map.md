# Network map (regions and firewall openings)

Servers in one **region** reach each other on every port. A connection across
regions needs a **firewall opening**, requested through the company system by
whoever needs it — so nobody has the whole picture of what is open. The network
map is that picture: one shared record of regions, hosts, the openings between
regions and the services listening on each port, kept by people and by agents.

UI: **Network / Firewall** (tabs Overview, Free ports, Regions, Hosts, Firewall
openings, Port usage, Probes, Access check, Settings) and a page per host
(`/network/hosts/:id`). The overview has three views:

- **By server** — one server in the middle, what reaches it on the left
  (inbound openings, from hosts or CIDR ranges), what it reaches on the right
  (outbound openings, its own and those of a range its IP is in) and its own
  region below (every port, both ways). Edges carry their ports and are
  coloured by state (open, has unverified, probe mismatch, closed only); click
  an edge for its openings and a server to centre the map on it
  (`/network?view=host&host=<id>`). The host page shows the same map.
- **Matrix** and **Map** — regions against regions (as a table, or regions as
  nodes and open openings as directed edges); click a cell or an edge for its
  openings.
API: `/_api/network/*` for the UI, `/api/network/*` through the gateway for API
keys, and the MCP `network` tool group.

## What is recorded

| Record | Fields | Notes |
|---|---|---|
| Region | name, description, CIDRs | A host (or a raw IP in an access check) inside a CIDR belongs to that region; the most specific CIDR wins. |
| Host | name, IP, region, owner, description, linked monitored server | Without a region it is placed by the CIDRs; with none matching it stays unassigned. A name must not be an IP address, and names are unique ignoring case. |
| Firewall opening | source (registered host **or** CIDR) → destination host, port or port range, tcp/udp, open/closed, ticket no., requester, purpose | One direction each: a two-way opening is two records, and the same opening (source, destination, protocol, ports) is recorded once. Expiry is not tracked here; it stays in the company system. Openings inside one region are refused (everything is open there). |
| Port usage | host, port, tcp/udp, service, description, owner | Recorded by people or agents, plus what UniBridge discovers itself (below). |
| Probe | source host → destination host, port, tcp/udp, result, latency, when | The latest measured connection attempt on that path, reported from the source host (see [Probes](#probes)). |

Every host, opening and port usage also records **who** wrote it
(`created_by`), its **source** (`manual`, `agent`, `import`) and whether a
person has **verified** it.

## How access is decided

`GET /network/check?src=&dst=&port=&protocol=` (UI: **Access check**; MCP:
`network_check_access`). `src` and `dst` are a registered host's name or IP, or
any IP (placed by the region CIDRs).

| Status | Reason | When |
|---|---|---|
| open | `same_region` | Both ends are in the same region. |
| open | `opening` | An **open** opening matches: same destination host, protocol and a port range containing the port, and a source that is the host or a CIDR containing its IP. |
| blocked | `opening_closed` | Only **closed** openings match. |
| blocked | `no_opening` | Different regions and nothing matches. |
| unknown | `unknown_region` | A region is missing on either end and no open opening matches. |

A blocked answer carries a `request_draft`: the source, destination (with IPs
and regions), protocol/port and direction, ready to paste into the company
request form. The answer also lists the matching openings and the services
recorded on that port.

Records are not proof: an opening marked open may have expired in the company
system, and one nobody recorded may exist. Keep the map current — mark expired
or revoked openings **closed** rather than deleting them, so their history stays.

## Free ports

"I am bringing up a service these servers must reach: which ports work today,
without a firewall request?" — **Free ports** tab, `GET /network/port-finder`,
MCP `network_find_ports`.

Give the client servers (`src`, repeatable: host names or IPs; `src_region`:
every host of a region) and optionally the destination (`dst`), a wanted
`port` and `protocol`. For each destination host:

| Field | Meaning |
|---|---|
| `any_port` | Every source is in the host's region: any port not in use works. |
| `reachable` | Port spans **every** source reaches (same region, or an open opening covering that source); the intersection across sources. |
| `free` / `free_count` | `reachable` minus ports in use and ports measured blocked. |
| `used` | `recorded` (port usage), `auto` (UniBridge connects there) or `listening` (a probe got an answer from a service nobody recorded). |
| `blocked` | Ports recorded open that a source's probe timed out on: excluded from `free`, check the firewall. |
| `suggestions` | Up to 8 free ports: `probed_free` first (a source's probe was refused: it passed the firewall and nothing listens), then the lowest free ports, or the ones nearest `port` when given. Same-region hosts get none: any port works. |
| `requested_port` | With `port`: `free`, `used`, `blocked` or `unreachable`. |

Without `dst` every other host is checked (optionally only `dst_region`) and
the ones with a free port are listed, same-region hosts first, then by measured
free ports and room; with `port`, only hosts where that port is free. A source
in no region counts only openings that name it or its range
(`unplaced_sources`); a destination host in no region that no source reaches
through an opening is left out and named in `unplaced_destinations`.
`dst_region` applies only without `dst`. A probe timeout older than the latest
change to the opening that covers it does not count as blocked. When nothing fits, the access check drafts the request.

The **Find free ports on this server** link on a host fills in the destination
(`/network?tab=finder&dst=<name>`; `src=` can be added the same way).

## Setup

1. **Regions** — on the Regions tab, add each region with the CIDRs it covers
   (e.g. `서울-DC1` → `10.10.0.0/16`).
2. **Hosts** — **Import monitored servers** on the Hosts tab adds every server
   from **Servers** whose address is an IP (servers with a DNS address are
   skipped; add them by hand), placed by CIDR and linked to its monitored
   server. Add the rest by hand or let agents register them. Fix any host left
   **unassigned** (the overview counts them).
3. **Platform host** — on the Settings tab, pick the host UniBridge runs on.
   Discovered port usage is then checked for reachability from it.
4. **Existing openings** — record the openings already approved (ticket no.,
   requester and purpose make them traceable): import them from a sheet
   ([CSV import](#csv-import)), or have an agent do it.
5. **Roles** — `network.read` to view, `network.write` to edit. The built-in
   admin role has both.
6. **Probes** (optional) — run [`scripts/network_probe.py`](#probe-script) on
   the servers, or let agents report, to check the records against reality.

## Discovered port usage

UniBridge lists, as read-only rows with source `auto`, the ports it connects to
itself, on hosts registered in the map (matched by IP or host name):

| Kind | From |
|---|---|
| `db` | database connections (host:port) |
| `s3` | S3 endpoints (port from the URL, else 443/80) |
| `node_exporter`, `dcgm` | server monitoring scrape targets |
| `metrics` | external API monitoring targets |
| `gateway_upstream` | APISIX upstream nodes (read from the Admin API, cached 30 s; listed under `auto_sources_failed` on the overview when it cannot be read) |

With a platform host set, each such row shows `reachable_from_platform`:
`false` means UniBridge is in another region with no open opening to that port —
a likely reason a database connection, scrape or upstream fails.

API keys other than master keys see these rows by kind only (e.g.
`database (discovered)`), not the names of UniBridge's own connections.

## CSV import

People with `network.write` can add hosts, firewall openings or port usage in
bulk: **CSV import** on the Hosts, Firewall openings and Port usage tabs, or
`POST /network/import {kind, csv, dry_run}`. A dry run first shows what every
line would do — `create`, `skip` (already recorded, or an opening inside one
region) or `error` (with the reason); importing then adds the valid lines in
one go, as source `import`, verified by the importer. Files may be UTF-8 or
EUC-KR (Excel on Korean Windows); comma, tab or semicolon separated; at most
5000 lines. The header row names the columns, in English or Korean (case,
spaces and `_` ignored; other columns are ignored):

| Kind | Columns (required in bold) |
|---|---|
| hosts | **name** (서버명, 호스트명), **ip** (IP 주소), region (리전, 지역), owner (담당자), description (설명, 비고) |
| openings | **src** (출발지), **dst** (목적지), **ports** (포트), protocol (프로토콜), status (상태), ticket_no (신청번호), requester (신청자), purpose (용도, 목적) |
| port_usages | **host** (호스트), **port** (포트), **service_name** (서비스), protocol, owner, description |

For openings, `src` is a registered host (name or IP), a CIDR, or an IP no host
has (recorded as that single address); `dst` must be a registered host, so
import hosts first. `ports` takes `5432`, `8000-8010` or `8000~8010`, or several
separated by commas or spaces (one opening each; at most 64 per cell and 5000
openings per import). `status` reads open/closed and
열림/허용/승인 or 해제/만료/폐기/차단; empty means open. Hosts without a region
column are placed by the region CIDRs.

## Probes

Records are what people wrote down; a **probe** is what a connection attempt
actually did. Probes are reported from the **source** host — by the probe script
below or by an agent — and each replaces the previous one on its path
(an older measurement never replaces a newer one). A report may carry
`observed_at` (at most 5 minutes ahead of UniBridge's clock); without it
UniBridge's clock stamps it, which is what the probe script does, so a server
with a wrong clock still reports correctly. A wrong measurement can be deleted
(`DELETE /network/probes/{id}`).

| Result | Meaning |
|---|---|
| `open` | Connected. |
| `refused` | The network let it through but nothing listens: the firewall is open, the service is down or elsewhere. |
| `timeout` | No answer: what a firewall that drops the connection looks like. |
| `error` | Anything else (DNS, routing, …). |

Each probe is compared with the records (the access-check answer for its path):

| Mismatch | When |
|---|---|
| `recorded_open_but_blocked` | The records say open, the probe timed out: the opening expired, was never applied, or the record is wrong. |
| `passes_without_opening` | The records say blocked, the probe got through: an opening nobody recorded (record it), or a closed one still in place. |

The Probes tab lists them (filter: mismatches only); the overview counts
mismatches; openings, the access check and host pages show the latest probe.

### Probe script

[`scripts/network_probe.py`](../scripts/network_probe.py) runs on a server
(Python 3.6+, standard library only). It asks UniBridge which openings this
host's source is covered by (`GET /api/network/probe-targets?src=`, closed ones
included to confirm they are closed; port ranges of up to 16 ports are tried
port by port, wider ones on their first port and the ports with a known
service), tries a TCP connection to each, and reports the results. UDP targets
are skipped. The host must be registered on the map; the API key needs
`network-api` and `network-write-api`.

```bash
export UNIBRIDGE_URL=https://<HOST_IP>:<UNIBRIDGE_UI_PORT>
export UNIBRIDGE_API_KEY=<key>
python3 network_probe.py --cacert /etc/pki/unibridge-ca.pem   # or --insecure
python3 network_probe.py --dry-run          # measure and print, report nothing

# hourly, quiet unless something is wrong
0 * * * * UNIBRIDGE_URL=... UNIBRIDGE_API_KEY=... python3 /opt/unibridge/network_probe.py -q --cacert /etc/pki/unibridge-ca.pem
```

Options: `--src` (this host's registered name or IP; default its primary IP,
else hostname), `--timeout` (default 3 s), `--concurrency` (default 8),
`--fail-on-mismatch`. Exit codes: 0 done, 1 an HTTP failure or a rejected
result, 2 bad arguments, 3 a mismatch with `--fail-on-mismatch`.

## Flags

| Flag | Meaning |
|---|---|
| Unused | An open opening with no recorded or discovered service on its ports: a cleanup candidate (or a missing port-usage record). |
| Agent / Unverified | Written or last changed by an API key; a person confirms it with **Verify**. A person's edit verifies a record; an agent's edit makes it unverified again. |
| Not reachable from platform | A discovered usage UniBridge cannot reach under the recorded openings. |
| Probe mismatch | The latest probe on a path contradicts the records (see [Probes](#probes)). |

The overview counts each, and its region × region matrix shows the open
openings between every pair of regions (click a cell for the list; a "CIDR
outside regions" row appears for CIDR sources no region covers).

## Agents

Grant an API key, on the **API Keys** page:

| Route | Allows |
|---|---|
| `network-api` | `GET /api/network/*`: overview, regions, hosts, openings, port usage, access checks |
| `network-write-api` | `POST`/`PUT`/`DELETE /api/network/*`: register and update hosts, record, update and delete openings and port usage, report probes |

```bash
curl -H "apikey: $UNIBRIDGE_API_KEY" \
  "https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/network/check?src=batch-01&dst=10.20.1.10&port=5432"

curl -X POST -H "apikey: $UNIBRIDGE_API_KEY" -H "Content-Type: application/json" \
  "https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/network/openings" \
  -d '{"src_host": "batch-01", "dst_host": "orders-db", "port_from": 5432,
       "ticket_no": "FW-2026-0412", "requester": "kim", "purpose": "nightly export"}'
```

Over MCP (`/api/mcp`, key also granted `mcp-api`; the `network` group can be
switched off in MCP settings), a key with `network-api` gets the read tools and
one that also has `network-write-api` the write tools:

- read: `network_overview`, `network_list_regions`, `network_find_hosts`,
  `network_get_host`, `network_check_access`, `network_find_ports`, `network_list_openings`,
  `network_list_port_usages`, `network_list_probes`, `network_probe_targets`
- write: `network_register_host`, `network_update_host`,
  `network_record_opening`, `network_update_opening`, `network_delete_opening`,
  `network_record_port_usage`, `network_delete_port_usage`,
  `network_report_probes`

Tools take a host by name, IP or id. Recording an opening records one that
exists; it does not open anything. An agent that can run commands on a server
can measure from it: `network_probe_targets` for that host, try each
connection there, then `network_report_probes` with the results (the source is
the host it measured from).

People only (Keycloak login with `network.write`): defining regions, verifying,
deleting a record a person verified (or a host that has any), the platform-host
setting, the monitored-server import and CSV import. What an agent records is stored as
`source=agent` and stays unverified until a person confirms it. Every change is
in the admin audit log with its actor (`apikey:<name>` for agents).

## API

All under `/network` (`/_api/network` from the UI, `/api/network` with a key).

| Method | Path | Notes |
|---|---|---|
| GET | `/overview` | counts, region links, `auto_sources_failed` |
| GET / PUT | `/settings` | `platform_host_id`; PUT people only |
| GET | `/check` | `src`, `dst`, `port`, `protocol` |
| GET | `/port-finder` | `src` (repeatable), `src_region` (repeatable), `dst`, `dst_region`, `port`, `protocol`: see [Free ports](#free-ports) |
| GET / POST | `/regions` | POST people only |
| PUT / DELETE | `/regions/{id}` | people only; a region with hosts cannot be deleted |
| GET / POST | `/hosts` | GET filters `region_id`, `q`, `unverified`; POST takes `region_id` or `region` (name) |
| GET / PUT / DELETE | `/hosts/{id}` | GET adds inbound and outbound openings (`outbound_via_cidr`: those whose source range contains its IP), port usage and `same_region_hosts`; DELETE removes its openings, port usage and probes too |
| POST | `/hosts/{id}/verify` | people only |
| POST | `/hosts/import-monitored` | people only |
| GET / POST | `/openings` | GET filters `host_id`, `region_id`, `status`, `port`, `unverified`; POST takes `src_host_id`/`src_host`/`src_cidr` and `dst_host_id`/`dst_host` (name or IP); a duplicate answers 409 with the existing `id` |
| PUT / DELETE | `/openings/{id}` | |
| POST | `/openings/{id}/verify` | people only |
| GET / POST | `/port-usages` | GET filters `host_id`, `port`, `source` (`manual`/`agent`/`auto`), `unverified` |
| PUT / DELETE | `/port-usages/{id}` | stored rows only |
| POST | `/port-usages/{id}/verify` | people only |
| GET / POST | `/probes` | GET filters `host_id`, `mismatch`, `result`; POST `{results: [...]}` (up to 500; each fails on its own, returned under `errors` by index) |
| DELETE | `/probes/{id}` | drop one measurement |
| GET | `/probe-targets` | `src`: what that host should test |
| POST | `/import` | people only; `{kind: hosts\|openings\|port_usages, csv, dry_run}` |

## Config export / import

**Admin → Config transfer** carries regions, hosts (with their region and
linked monitored server by name), firewall openings, recorded port usage and the
platform-host setting, keyed by name so they land in another deployment. Probes
and discovered usage are not exported (they describe one deployment). Rows the
import creates are source `import`; a row verified in the file is verified by
the importing admin, an unverified one stays unverified, and an unchanged row
keeps its state.

## Not covered yet

- Syncing with the company firewall system (no API for it).
- UDP probes.
