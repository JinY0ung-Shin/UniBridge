# Blue/Green Deployment with Docker Compose

UniBridge can run blue/green deployments without Kubernetes by splitting the
stack into three Compose files:

- `docker-compose.infra.yml`: persistent services, APISIX, Keycloak, LiteLLM,
  Prometheus, and databases.
- `docker-compose.app.yml`: one color of the application tier:
  `unibridge-service`, `llm-converter`, and `unibridge-ui`.
- `docker-compose.edge.yml`: the stable public HTTPS entrypoint. It proxies to
  either `unibridge-ui-blue` or `unibridge-ui-green`.

The original `docker-compose.yml` is still supported for single-stack
deployments.

## Pre-Deploy Checklist (read before applying)

Work through this before running `deploy-bluegreen.sh` on a real environment.

1. **Use a networked metadata DB — not SQLite.** Both colors share one meta
   volume; SQLite cannot be written by two containers safely, and a new color
   runs `alembic upgrade head` on the shared file at boot. Set
   `META_DB_URL=postgresql+asyncpg://…`. The script refuses SQLite unless
   `ALLOW_SQLITE_BLUEGREEN=true`. (See "Database" below.)

2. **Set ports in `.env`, and make them consistent.** The script reads `.env`
   (`--env-file`); define every port there:
   - `UNIBRIDGE_EDGE_PORT` must equal the **actual public port**. CORS and
     Keycloak redirect/origin derive from it. If it is wrong, the UI loads but
     login/API calls fail.
   - Keycloak's registered redirect URI / web origin
     (`KEYCLOAK_REDIRECT_URI`, `KEYCLOAK_WEB_ORIGIN`, realm config) must match
     `https://HOST_IP:UNIBRIDGE_EDGE_PORT`.
   - All host ports must be distinct: `UNIBRIDGE_EDGE_PORT`,
     `BLUEGREEN_BLUE_UI_PORT`, `BLUEGREEN_GREEN_UI_PORT`, `KEYCLOAK_PORT`,
     `LITELLM_PORT`, `PROMETHEUS_PORT`, `APISIX_ADMIN_PORT`.
   - If you changed the APISIX admin port, also set `APISIX_ADMIN_PORT` (or
     `APISIX_ADMIN_HOST_URL`) — promotion calls the admin API there.

3. **Know what is and isn't zero-downtime.** Only UniBridge (the edge port) is
   rotated blue/green. Keycloak, LiteLLM, APISIX and the databases live in the
   **infra** stack as single instances: they stay up *during* an app deploy, but
   updating/restarting them is a normal restart with downtime — blue/green does
   not cover them.

4. **Migrations must be backward-compatible (expand/contract).** While both
   colors run, the old code must tolerate the new schema. Add nullable/new
   columns first, deploy compatible code, drop old fields in a later release.

5. **First run is a bootstrap, not a swap.** `deploy-bluegreen.sh deploy blue`
   provisions APISIX routes for the first time; the public edge port only starts
   listening once the edge stack comes up. Plan the initial cutover accordingly.

6. **One operation at a time.** `deploy`/`promote`/`rollback`/`stop` take a lock
   (`.deploy/bluegreen.lock`) — do not run two in parallel.

## Runtime Model

Public traffic should enter through `unibridge-edge` on `UNIBRIDGE_EDGE_PORT`
(default `3000`). The blue and green UI containers publish localhost-only
verification ports:

- blue: `BLUEGREEN_BLUE_UI_PORT` (default `3001`)
- green: `BLUEGREEN_GREEN_UI_PORT` (default `3002`)

The deploy script starts the inactive color, waits for:

- `https://127.0.0.1:<color-port>/healthz`
- `https://127.0.0.1:<color-port>/_api/health`

Then it promotes APISIX upstreams and reloads the edge proxy.

## Database: Postgres Required (not SQLite)

> **Blue/green needs a networked metadata database. Do not use the default
> SQLite store.**

Both the blue and green app stacks mount the **same** meta volume
(`UNIBRIDGE_DATA_VOLUME`), so they share one database file. During a normal
update both colors run at once (the old color stays up for rollback, and the
new color runs `alembic upgrade head` at boot). SQLite cannot be safely written
by two containers concurrently — and migrating its file while the old version is
still live risks `database is locked` errors, lost writes, or corruption.

Set `META_DB_URL` to a networked database before deploying, e.g.:

```env
META_DB_URL=postgresql+asyncpg://unibridge:<password>@<host>:5432/unibridge
```

`scripts/deploy-bluegreen.sh` refuses to deploy when `META_DB_URL` is SQLite (or
unset). To override anyway — single-color use, or you accept the risk — set
`ALLOW_SQLITE_BLUEGREEN=true`.

## Existing Volume Names

The split Compose files use explicit volume names so existing single-stack
deployments can keep their data. Defaults match Docker Compose's normal names
when this repo runs as project `unibridge`:

- `unibridge_unibridge-data`
- `unibridge_etcd-data`
- `unibridge_keycloak-db-data`
- `unibridge_litellm-db-data`
- `unibridge_prometheus-data`

If the old deployment used a different `COMPOSE_PROJECT_NAME`, set these in
`.env` before running the split stack:

```env
UNIBRIDGE_DATA_VOLUME=<old-project>_unibridge-data
ETCD_DATA_VOLUME=<old-project>_etcd-data
KEYCLOAK_DB_DATA_VOLUME=<old-project>_keycloak-db-data
LITELLM_DB_DATA_VOLUME=<old-project>_litellm-db-data
PROMETHEUS_DATA_VOLUME=<old-project>_prometheus-data
```

## First Run

From the repo root:

```bash
cp .env.example .env
# Fill required secrets and volume overrides if needed.

scripts/deploy-bluegreen.sh deploy blue
```

When no active color state exists, the script lets the first app color run
startup APISIX provisioning. That bootstraps the routes and initial upstreams.

## Normal Updates

After pulling new code:

```bash
scripts/deploy-bluegreen.sh deploy
```

The script chooses the inactive color, builds it, verifies health, updates
APISIX upstreams, reloads edge nginx, and records the active color in
`.deploy/bluegreen-active`.

By default, the old color remains running for rollback:

```bash
scripts/deploy-bluegreen.sh rollback
```

Stop the old color manually after the new version is accepted:

```bash
scripts/deploy-bluegreen.sh stop blue
# or
scripts/deploy-bluegreen.sh stop green
```

To stop the old color automatically after promotion:

```bash
STOP_OLD_AFTER_PROMOTE=true DRAIN_SECONDS=30 scripts/deploy-bluegreen.sh deploy
```

`rollback` re-promotes the previous color. If that color was stopped (e.g. via
`STOP_OLD_AFTER_PROMOTE=true` or a manual `stop`), `rollback` brings its
containers back up first, then waits for health and promotes. Rollback only
works to a color whose image still exists — it does not rebuild.

Only one mutating command (`deploy`/`promote`/`rollback`/`stop`) can run at a
time; the script takes a lock (`.deploy/bluegreen.lock`) and aborts if another
run holds it.

## Manual Promotion

If a color is already running and healthy:

```bash
scripts/deploy-bluegreen.sh promote green
```

This does not rebuild containers. It only verifies the target color, updates
APISIX upstreams, reloads the edge proxy, and updates the active state file.

## Important Limits

Schema changes still need backward-compatible migrations. Do expand/contract
changes across releases: add nullable/new columns first, deploy compatible code,
then remove old fields in a later release.

The inactive color starts with `APISIX_PROVISION_ON_START=false` during normal
updates. This prevents a warming container from changing APISIX before health
checks pass. If a release changes built-in APISIX route definitions, bootstrap
the route change intentionally before relying on normal blue/green promotion.

Stored API-key route restrictions are replayed from the database on **every**
boot regardless of `APISIX_PROVISION_ON_START`, so the database stays the source
of truth even on inactive colors. Route/upstream *provisioning* stays gated by
the flag, but if the script detects that APISIX has lost its core routes (e.g.
an etcd reset) it forces re-provisioning on the next `deploy` so the system
cannot silently come up with no routes.
