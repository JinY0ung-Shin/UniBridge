# Network map (regions and firewall openings)

Servers in one **region** reach each other on every port. A connection across
regions needs a **firewall opening**, requested through the company system by
whoever needs it — so nobody has the whole picture of what is open. The network
map is that picture: one shared record of regions, hosts, the openings between
regions and the services listening on each port, kept by people and by agents.

UI: **Network / Firewall** (tabs Overview, Regions, Hosts, Firewall openings,
Port usage, Access check, Settings) and a page per host (`/network/hosts/:id`).
API: `/_api/network/*` for the UI, `/api/network/*` through the gateway for API
keys, and the MCP `network` tool group.

## What is recorded

| Record | Fields | Notes |
|---|---|---|
| Region | name, description, CIDRs | A host (or a raw IP in an access check) inside a CIDR belongs to that region; the most specific CIDR wins. |
| Host | name, IP, region, owner, description, linked monitored server | Without a region it is placed by the CIDRs; with none matching it stays unassigned. A name must not be an IP address, and names are unique ignoring case. |
| Firewall opening | source (registered host **or** CIDR) → destination host, port or port range, tcp/udp, open/closed, ticket no., requester, purpose | One direction each: a two-way opening is two records. Expiry is not tracked here; it stays in the company system. Openings inside one region are refused (everything is open there). |
| Port usage | host, port, tcp/udp, service, description, owner | Recorded by people or agents, plus what UniBridge discovers itself (below). |

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
   requester and purpose make them traceable), or have an agent do it.
5. **Roles** — `network.read` to view, `network.write` to edit. The built-in
   admin role has both.

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

## Flags

| Flag | Meaning |
|---|---|
| Unused | An open opening with no recorded or discovered service on its ports: a cleanup candidate (or a missing port-usage record). |
| Agent / Unverified | Written or last changed by an API key; a person confirms it with **Verify**. A person's edit verifies a record; an agent's edit makes it unverified again. |
| Not reachable from platform | A discovered usage UniBridge cannot reach under the recorded openings. |

The overview counts each, and its region × region matrix shows the open
openings between every pair of regions (click a cell for the list; a "CIDR
outside regions" row appears for CIDR sources no region covers).

## Agents

Grant an API key, on the **API Keys** page:

| Route | Allows |
|---|---|
| `network-api` | `GET /api/network/*`: overview, regions, hosts, openings, port usage, access checks |
| `network-write-api` | `POST`/`PUT`/`DELETE /api/network/*`: register and update hosts, record, update and delete openings and port usage |

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
  `network_get_host`, `network_check_access`, `network_list_openings`,
  `network_list_port_usages`
- write: `network_register_host`, `network_update_host`,
  `network_record_opening`, `network_update_opening`, `network_delete_opening`,
  `network_record_port_usage`, `network_delete_port_usage`

Tools take a host by name, IP or id. Recording an opening records one that
exists; it does not open anything.

People only (Keycloak login with `network.write`): defining regions, verifying,
deleting a record a person verified (or a host that has any), the platform-host
setting and the monitored-server import. What an agent records is stored as
`source=agent` and stays unverified until a person confirms it. Every change is
in the admin audit log with its actor (`apikey:<name>` for agents).

## API

All under `/network` (`/_api/network` from the UI, `/api/network` with a key).

| Method | Path | Notes |
|---|---|---|
| GET | `/overview` | counts, region links, `auto_sources_failed` |
| GET / PUT | `/settings` | `platform_host_id`; PUT people only |
| GET | `/check` | `src`, `dst`, `port`, `protocol` |
| GET / POST | `/regions` | POST people only |
| PUT / DELETE | `/regions/{id}` | people only; a region with hosts cannot be deleted |
| GET / POST | `/hosts` | GET filters `region_id`, `q`, `unverified`; POST takes `region_id` or `region` (name) |
| GET / PUT / DELETE | `/hosts/{id}` | GET adds inbound and outbound openings and port usage; DELETE removes its openings and port usage too |
| POST | `/hosts/{id}/verify` | people only |
| POST | `/hosts/import-monitored` | people only |
| GET / POST | `/openings` | GET filters `host_id`, `region_id`, `status`, `port`, `unverified`; POST takes `src_host_id`/`src_host`/`src_cidr` and `dst_host_id`/`dst_host` (name or IP); a duplicate answers 409 with the existing `id` |
| PUT / DELETE | `/openings/{id}` | |
| POST | `/openings/{id}/verify` | people only |
| GET / POST | `/port-usages` | GET filters `host_id`, `port`, `source` (`manual`/`agent`/`auto`), `unverified` |
| PUT / DELETE | `/port-usages/{id}` | stored rows only |
| POST | `/port-usages/{id}/verify` | people only |

## Not covered yet

- A graph view of regions and openings (the overview has the matrix).
- CSV bulk import from the company system's export.
- Agents reporting live connection tests from the servers themselves.
- The network map in the config export/import.
