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
