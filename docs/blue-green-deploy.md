# Blue/Green Deployment with Docker Compose

UniBridge can run blue/green deployments without Kubernetes by splitting the
stack into three Compose files:

- `docker-compose.infra.yml`: persistent services, APISIX, Keycloak, Bifrost,
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
     `BIFROST_UI_PORT`, `BIFROST_TEST_ADMIN_PORT`,
     `PROMETHEUS_PORT`, `APISIX_ADMIN_PORT`.
   - If you changed the APISIX admin port, also set `APISIX_ADMIN_PORT` (or
     `APISIX_ADMIN_HOST_URL`) — promotion calls the admin API there.

3. **Know what is and isn't zero-downtime.** Only UniBridge (the edge port) is
   rotated blue/green. Keycloak, Bifrost, APISIX and the databases live
   in the **infra** stack as single instances: they stay up *during* an app deploy, but
   updating/restarting them is a normal restart with downtime — blue/green does
   not cover them. After the first bootstrap, normal app deploys verify the
   existing infra without reconciling it. Set `RECONCILE_INFRA_ON_DEPLOY=true`
   only for a reviewed deployment that is allowed to update/restart infra.

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

The stack ships a bundled `unibridge-db` Postgres service and uses it by default:
leave `META_DB_URL` **unset** and just set `UNIBRIDGE_DB_PASSWORD` in `.env`. The
compose files then resolve `META_DB_URL` to:

```
postgresql+asyncpg://unibridge:<UNIBRIDGE_DB_PASSWORD>@unibridge-db:5432/unibridge
```

To use your own external Postgres instead, set `META_DB_URL` explicitly:

```env
META_DB_URL=postgresql+asyncpg://unibridge:<password>@<host>:5432/unibridge
```

`scripts/deploy-bluegreen.sh` refuses to deploy when `META_DB_URL` is SQLite, or
when it is unset and `UNIBRIDGE_DB_PASSWORD` is also empty (the bundled Postgres
would have no password). To force a shared SQLite store anyway — single-color
use, or you accept the risk — set `ALLOW_SQLITE_BLUEGREEN=true`.

### Migrating existing SQLite data to Postgres

If you already run a single-stack deployment on the legacy SQLite meta store,
copy that data into the bundled `unibridge-db` Postgres before the first
blue-green deploy. The migration runs inside the Compose network so it can reach
`unibridge-db` by name; the SQLite source file lives in the mounted data volume
at `/app/data/meta.db`.

> **Stop the old stack (or quiesce writes) first.** This is a one-shot snapshot
> copy, not a live replica — any row written to SQLite after the copy begins is
> lost. Bring the single-stack app down, run the migration, then cut over.

Make sure the SQLite source has already been upgraded by the current app (or by
running `alembic upgrade head`) before copying. The migration script verifies
that the source `alembic_version` matches the current code's head and stops if
it does not.

With `UNIBRIDGE_DB_PASSWORD` set in `.env` (and the **same `ENCRYPTION_KEY`** as
the source — encrypted credentials are copied verbatim and stay valid only under
the same key):

```bash
# 1. Start only the bundled meta-store Postgres
docker compose up -d unibridge-db

# 2. Copy SQLite -> Postgres (ENCRYPTION_KEY is inherited from .env)
docker compose run --rm --no-deps unibridge-service \
  python -m scripts.migrate_sqlite_to_postgres \
    --source sqlite+aiosqlite:///data/meta.db \
    --target "postgresql+asyncpg://unibridge:${UNIBRIDGE_DB_PASSWORD}@unibridge-db:5432/unibridge"
```

It creates the schema on the empty Postgres target, copies every table, and
resets sequences so the live app does not collide with copied primary keys. Use
`--dry-run` to preview row counts; it refuses a non-empty target unless
`--truncate` is given. After it succeeds, leave `META_DB_URL` unset (the stack
defaults to `unibridge-db`) and deploy.

> Using your own external Postgres instead of the bundled service? Point
> `--target` (and afterward `META_DB_URL`) at it, and you can run the script
> directly from `unibridge-service/` without Compose.

## Existing Volume Names

The split Compose files use explicit volume names so existing single-stack
deployments can keep their data. Defaults match Docker Compose's normal names
when this repo runs as project `unibridge`:

- `unibridge_unibridge-data`
- `unibridge_unibridge-db-data`
- `unibridge_etcd-data`
- `unibridge_keycloak-db-data`
- `unibridge_prometheus-data`
- `unibridge_prometheus-file-sd`
- `unibridge_llm-converter-state`
- `unibridge_bifrost-test-data`

If the old deployment used a different `COMPOSE_PROJECT_NAME`, set these in
`.env` before running the split stack:

```env
UNIBRIDGE_DATA_VOLUME=<old-project>_unibridge-data
UNIBRIDGE_DB_DATA_VOLUME=<old-project>_unibridge-db-data
ETCD_DATA_VOLUME=<old-project>_etcd-data
KEYCLOAK_DB_DATA_VOLUME=<old-project>_keycloak-db-data
PROMETHEUS_DATA_VOLUME=<old-project>_prometheus-data
PROMETHEUS_FILE_SD_VOLUME=<old-project>_prometheus-file-sd
LLM_CONVERTER_STATE_VOLUME=<old-project>_llm-converter-state
BIFROST_TEST_DATA_VOLUME=<old-project>_bifrost-test-data
```

The deploy script creates the shared app-data and converter-state volumes if
they do not exist. The app Compose file treats them as external so blue and
green never compete for Compose ownership of the same volume.

## Backups

`backup/backup.sh` and `backup/restore.sh` work on this layout without extra
flags. They detect blue/green from `.deploy/bluegreen-active` (or a running
`unibridge-infra` project) and then address each service in the project that
owns it: infra services (`etcd`, `apisix`, `keycloak(-db)`,
`unibridge-db`, `bifrost`, plus the one-shot `etcd-init` auth bootstrap, which exits 0 after
every `up` and is expected to show as `Exited (0)`) through `unibridge-infra`,
app-tier services through the color projects. A restore that must stop `unibridge-service` stops it in every color
currently running it and starts back exactly those colors.

`BACKUP_STACK=single|bluegreen` forces the mode when detection cannot see the
stack — for example a restore onto a host whose infra project is still down and
that has no state file yet.

Do not recover a blue/green host with a plain `docker compose up -d`: the
single-stack `docker-compose.yml` pins the same `unibridge_*` volumes and would
run a second instance against them. Bring infra up with
`docker compose -p unibridge-infra -f docker-compose.infra.yml up -d` and app
colors with this script. See `backup/README.md` for the full runbook.

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

The edge keeps running across deploys and only reloads its config, unless its
definition changed: `docker-compose.edge.yml` itself, a `.env` value it reads
(`UNIBRIDGE_EDGE_PORT`, `TLS_CERT_PATH`/`TLS_KEY_PATH`,
`TLS_BIFROST_CERT_PATH`/`TLS_BIFROST_KEY_PATH`), or a newer local
`nginx:alpine` image. Then the switch recreates the container, on `promote` and
`rollback` too: nginx stops listening at once, so the public port stays closed
while it drains open requests (up to Docker's 10 s stop grace period) and the
new container starts, and requests still open at the end of the grace period,
LLM streams for example, are cut. The certificate mounts for
`BIFROST_UI_HOSTNAME` (README, Bifrost UI) were such a change.
The edge config itself is rendered from `deploy/edge/` on every switch, with a
server for `BIFROST_UI_HOSTNAME` while it is set.

Normal updates do not run `compose up` on the shared infra stack. If an infra
service is missing or unhealthy, deployment aborts before building or changing
traffic. To intentionally apply infra image/config changes, review their
downtime impact and run:

```bash
RECONCILE_INFRA_ON_DEPLOY=true scripts/deploy-bluegreen.sh deploy
```

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

`rollback` re-promotes the previous color only when all three of its containers
are already running and healthy. It intentionally does not run `compose up` on
the app colors or reconcile infra (the edge's own `compose up` can recreate the
edge, see [Normal Updates](#normal-updates)): after a forward database migration, recreating an old service
image can fail because its Alembic tree does not know the database's newer
revision. If the inactive color was stopped (for example by
`STOP_OLD_AFTER_PROMOTE=true`) or is unhealthy, rollback aborts without changing
APISIX, edge routing, or the active-color state. Use `deploy <color>` from a
reviewed, database-compatible checkout to build it as a new deployment, or use
a planned database/image recovery procedure for a true historical rollback.

Only one mutating command (`deploy`/`promote`/`rollback`/`stop`) can run at a
time; the script takes a lock (`.deploy/bluegreen.lock`) and aborts if another
run holds it.

## Manual Promotion

If a color is already running and healthy:

```bash
scripts/deploy-bluegreen.sh promote green
```

This does not rebuild containers or reconcile infra. It only verifies the
target color, updates APISIX upstreams, reloads the edge proxy (recreating it
when its definition changed, see [Normal Updates](#normal-updates)), and updates
the active state file.

## Important Limits

Schema changes still need backward-compatible migrations. Do expand/contract
changes across releases: add nullable/new columns first, deploy compatible code,
then remove old fields in a later release.

The inactive color starts with `APISIX_PROVISION_ON_START=false` during normal
updates. This prevents a warming container from changing APISIX before health
checks pass. Before each deploy, the script verifies the built-in APISIX route
shape, including the separately granted `query-template-write-api` lifecycle route
and the LLM routes. If those routes are missing or stale, for example
`llm-proxy` still points at an older gateway upstream, it forces
re-provisioning on the new color instead of promoting broken routing. The
`/api/llm` routes are held to Bifrost the same way: `llm-proxy` and
`llm-metrics` point at Bifrost, every route to Bifrost or the converter injects
the current `BIFROST_TEST_VK`, `llm-not-found` is there and LiteLLM's old
`llm-admin` route and `litellm` upstream are gone. So a rotated
`BIFROST_TEST_VK` takes effect on the next deploy. The deploy also warns,
without stopping, when the infra `bifrost` container still runs with another
`BIFROST_TEST_VK` than `.env`: it needs a recreate before it accepts the new
key. It refuses an `LLM_GATEWAY` in `.env` other than `bifrost`, since LiteLLM
support was removed ([README: Upgrading past the LiteLLM removal](../README.md#upgrading-past-the-litellm-removal)).

The old `/api/llm-bi` side-by-side routes are retired the same way: the deploy
re-provisions while any of `llm-bi-proxy`, `llm-bi-messages`,
`llm-bi-responses`, `llm-bi-models` or the `llm-converter-bi` upstream is still
in APISIX, or `llm-bi-not-found` does not yet say that `/api/llm-bi` has been
removed. Boot provisioning deletes them and leaves `llm-bi-not-found` behind, a
404 pointing callers at `/api/llm`. (Should APISIX ever refuse
`llm-bi-not-found`, which the service logs and boots past, or a route of your
own still use the `llm-converter-bi` upstream, which the service then keeps with
a warning, every deploy re-provisions until that is fixed.)

### Upgrading past the `/api/llm-bi` removal

The release that removed `/api/llm-bi` also dropped the `llm-converter-bi`
service from `docker-compose.infra.yml`. Deploys never run `compose up` on the
infra stack, so on a host that had it, its container keeps running as an
orphan. After the first deploy of that release (which takes the routes and the
upstream out of APISIX), remove it by name. Do not reach for `--remove-orphans`
on the infra project, and never `down -v`: the volume names are fixed, so that
would delete the shared data volumes.

```bash
# Expect unibridge-infra-llm-converter-bi-1
docker ps -a --filter label=com.docker.compose.project=unibridge-infra \
  --filter label=com.docker.compose.service=llm-converter-bi --format '{{.Names}}'
docker stop unibridge-infra-llm-converter-bi-1
docker rm unibridge-infra-llm-converter-bi-1
# Optional: its previous_response_id store and its image
docker volume rm unibridge_llm-converter-bi-state
docker image rm llm-converter:bifrost-test
```

`backup/lib/bifrost.sh` used to borrow `llm-converter:bifrost-test` for its
Python; it now takes the image of the app tier's `llm-converter` container
(`BIFROST_BACKUP_IMAGE` still overrides it), so the image can go. Remove
`BIFROST_GATEWAY_ROUTES` and `LLM_CONVERTER_BI_STATE_VOLUME` from `.env` if
set; nothing reads them any more.

Going back to a checkout from before the removal (a deploy, not an edge
`rollback`) brings `llm-converter-bi` back into its infra compose: that deploy
refuses to start while the service is not running (or recreates it with
`RECONCILE_INFRA_ON_DEPLOY=true`), and its boot reinstalls the `/api/llm-bi`
routes. Start it first:
`docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --build llm-converter-bi`
on that checkout.

Stored API-key route restrictions are replayed from the database on **every**
boot regardless of `APISIX_PROVISION_ON_START`, so the database stays the source
of truth even on inactive colors. Route/upstream *provisioning* stays gated by
the flag, but if the script detects that APISIX has lost or drifted from its
core routes (e.g. an etcd reset or a stale LLM route after gateway migration) it
forces re-provisioning on the next `deploy` so the system cannot silently come
up with no or broken routes.

Boot-time provisioning cannot demote the active color. `APISIX_PROVISION_ON_START`
and the pinned `APISIX_UNIBRIDGE_SERVICE_NODE` / `APISIX_LLM_CONVERTER_NODE`
values are baked into a container's environment when it is *created*, and the app
stack runs with `restart: unless-stopped`, so a container created with
provisioning armed re-runs it on every host reboot, dockerd restart, or OOM
restart. The service therefore skips a color-pinned upstream write, with a
warning, whenever that upstream already exists pointing at nodes other than its
own: `promote` is the only writer of the upstream-to-color mapping. A first
boot, a matching refresh, and a genuinely missing upstream still provision
normally. The colorless `bifrost` upstream and all routes are unaffected, since
routes reference `upstream_id` rather than nodes and so carry no color.

After a promotion the script also clears the flag on the standby it keeps alive,
but only when that standby is actually armed: it inspects the
`unibridge-service-<color>` container's environment and skips the recreate when
`APISIX_PROVISION_ON_START=false` is already set. Recreating (or simply
restarting, as a host reboot does) the standby re-runs the *old* image's
lifespan against the shared database. An image that has this guard sees a
database past its own Alembic head and boots without upgrading or rewriting the
system roles; it also skips the roles when a newer image last wrote them
(`ROLE_SEED_VERSION`). An image from before that guard instead crash-loops on
the unknown revision after a release with a migration, and resets the roles to
its own permissions otherwise, so the protection is complete once both colors
run an image with it. The active color is never recreated here.

One gap remains. If etcd is wiped **and** an armed container restarts, its
upstream is genuinely absent, so it writes its own baked color, which may no
longer be the active one. Nothing inside a container can do better, because the
active color lives in the host-side state file. The next `deploy` detects the
missing or drifted core routes and re-provisions on the color it promotes, which
repairs the mapping.
