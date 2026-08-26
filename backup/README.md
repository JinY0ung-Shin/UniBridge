# UniBridge Backup & Restore

Operator runbook for backing up and restoring UniBridge state.

**Directory naming**: this source directory is `backup/` and is tracked in git. Runtime snapshot output lands in `snapshots/` (gitignored) under the project root. The two names are intentionally distinct to prevent confusion between code and runtime artifacts.

## Backup coverage

Every stateful volume in the stack is listed here, backed up or not, so nothing is silently uncovered.

| Component | Source | Backed up? | Output / bound | Why |
|---|---|---|---|---|
| etcd | volume `etcd-data` | **Yes** | `etcd.snap` | APISIX routes, consumers, plugin configs |
| unibridge-service metadata | Postgres service `unibridge-db` by default, or legacy SQLite `unibridge-data` (`meta.db`) when `META_DB_URL=sqlite...` | **Yes** | `unibridge-meta.sql.gz` (Postgres) or `unibridge-meta.db.gz` (SQLite) | API keys, encrypted credentials, user settings |
| Keycloak Postgres | volume `keycloak-db-data` | **Yes** | `keycloak-db.sql.gz` | users, realms, clients |
| LiteLLM Postgres | volume `litellm-db-data` | **Yes** | `litellm-db.sql.gz` | LLM keys, budgets, usage history |
| LiteLLM conversation dataset | volume `litellm-dataset` | **No** | bounded by `LITELLM_DATASET_RETENTION_DAYS` / `LITELLM_DATASET_MAX_TOTAL_BYTES` | Fine-tuning capture (full prompts/responses); regenerable and can grow large — see README "LLM conversation capture" |
| Grafana | volume `grafana-data` | **No** | — | Grafana's own SQLite (sessions, ad-hoc UI edits). Dashboards, datasources, and provisioning live in `grafana/` in the repo, so nothing critical is here |
| Prometheus | volume `prometheus-data` | **No** | in-container retention | Time-series, regeneratable over time |

The three **No** rows are intentional. The LiteLLM conversation dataset is training data captured for fine-tuning: it is not operational state, it can be re-collected, and it grows without a natural ceiling, so folding it into snapshots would balloon them — it is size-bounded by its retention envs instead (both default `0` = unbounded; set them to cap it). `grafana-data` and `prometheus-data` hold only regenerable or repo-provisioned state. To back the dataset up anyway, `tar` the `litellm-dataset` volume out of band — `backup.sh` deliberately leaves it out and marks the seam.

## Blue-green deployments

The scripts run on either layout and detect which one they are on:

- **single stack** — everything from `docker-compose.yml` (compose project `unibridge`).
- **blue-green** (`scripts/deploy-bluegreen.sh`) — shared infra in project `unibridge-infra` (`docker-compose.infra.yml`) plus one app stack per color, `unibridge-blue` / `unibridge-green` (`docker-compose.app.yml`).

Detection picks blue-green when `.deploy/bluegreen-active` exists (override the path with `BLUEGREEN_STATE_FILE`) or the `unibridge-infra` project has containers; single stack otherwise. Set `BACKUP_STACK=single` or `BACKUP_STACK=bluegreen` to force it — useful when a blue-green host's infra project is fully down and no state file was ever written. Every run logs the mode it picked (`stack mode: ...`).

What the mode changes:

- **Infra-tier services** (`etcd`, `apisix`, `keycloak`, `keycloak-db`, `litellm`, `litellm-db`, `unibridge-db`) are addressed in the `unibridge-infra` project.
- **App-tier services** (`unibridge-service`, `llm-converter`, `unibridge-ui`) are addressed per color. A restore that has to stop `unibridge-service` stops it in every color currently running it and starts back exactly those colors — a color you deliberately left down stays down. Operations that only need one container (legacy SQLite `docker compose cp`) go through the active color from the state file.
- **Metadata store**: blue-green normally uses the bundled `unibridge-db` Postgres, so `unibridge-meta.sql.gz` is a Postgres dump like the others. SQLite metadata is legacy — under blue-green it only exists behind `ALLOW_SQLITE_BLUEGREEN`, where both colors share one data volume, so its restore stops every running color before swapping the file.

On a blue-green host, never bring services up with a plain `docker compose up -d`: `docker-compose.yml` would start a **second, single-stack instance on the same pinned `unibridge_*` volumes**. Use the infra project for infra services and `scripts/deploy-bluegreen.sh` for app colors.

## Layout

```
<project-root>/snapshots/<YYYY-MM-DD_HHMMSSZ>/
  etcd.snap
  keycloak-db.sql.gz
  litellm-db.sql.gz
  unibridge-meta.sql.gz  # or unibridge-meta.db.gz for legacy SQLite deployments
  manifest.json          # sizes + SHA256 of each file
```

File permissions are set to `600`, the per-run directory to `700`. Backups contain secrets (encrypted credentials, session data, LLM keys) — protect the host filesystem accordingly and do not world-share backups.

`.env` must be shell-sourceable (values with spaces or shell metacharacters must be quoted). The backup scripts source it to pick up DB passwords.

### Host prerequisites

- `docker` + `docker compose` plugin (obviously)
- `bash`, `flock`, `find`, `sha256sum`, `gzip` (all standard)
- `pg_dump`/`psql` in the bundled Postgres containers for the default metadata store; `sqlite3` in `unibridge-service` only for legacy SQLite metadata deployments.
- **`jq` or `python3`** on the host — `restore.sh` uses one of them to verify `manifest.json` SHA256 before destructive actions. If neither is installed, restore will refuse to run.

## Scheduling (cron)

On the deploy host, add to the operator's crontab:

```
0 3 * * * cd /opt/unibridge && ./backup/backup.sh >> /var/log/unibridge-backup.log 2>&1
```

Retention is **14 days**, enforced at the end of every run. Override with `RETENTION_DAYS=<n>` or `SNAPSHOTS_ROOT=/other/path` if needed.

Concurrent runs are prevented by `flock` on `.backup.lock` in the project root — a second invocation will exit immediately rather than stomp on the first.

## Manual backup

```
./backup/backup.sh
```

Exits non-zero on any failure; cron will surface the failure through mail or the log file.

## Restore

Restore is **per-component and destructive**. Each `restore.sh` invocation:

- Verifies the backup dir has a `manifest.json` and the needed file before doing anything.
- Prints a plan of what will change.
- Requires a typed confirmation phrase (`RESTORE ETCD`, `RESTORE PG`, `RESTORE META`).
- Stops the consumer service (Keycloak / LiteLLM / unibridge-service / apisix) before touching its backing store, then restarts it — under blue-green, in every color that was running it. The plan it prints names those colors.

```
./backup/restore.sh etcd           ./snapshots/2026-04-19_030000Z
./backup/restore.sh keycloak-db    ./snapshots/2026-04-19_030000Z
./backup/restore.sh litellm-db     ./snapshots/2026-04-19_030000Z
./backup/restore.sh unibridge-meta ./snapshots/2026-04-19_030000Z
```

### Full-disaster recovery order

If the host is wiped and you're restoring from backup onto a fresh checkout, **do not `docker compose up -d` the whole stack first** — Keycloak's entrypoint bootstraps its realm into `keycloak-db` on first start, and restoring on top of a bootstrapped schema leaves caches inconsistent.

Correct order:

1. **Bring up only the stateful stores**:
   ```
   docker compose up -d --wait keycloak-db litellm-db etcd
   ```
   On a blue-green host, bring them up in the infra project instead:
   ```
   docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --wait keycloak-db litellm-db etcd
   ```
2. **Restore the data stores** (each script stops/starts the relevant consumer):
   ```
   ./backup/restore.sh keycloak-db    ./snapshots/<stamp>
   ./backup/restore.sh litellm-db     ./snapshots/<stamp>
   ./backup/restore.sh etcd           ./snapshots/<stamp>
   ```
3. **Bring up the rest** with restored data:
   ```
   docker compose up -d --wait
   ```
   On a blue-green host, bring up the infra stack and then an app color through the deploy script — a plain `docker compose up -d` here would start a second single-stack instance on the same volumes:
   ```
   docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --wait
   scripts/deploy-bluegreen.sh deploy blue
   ```
4. **Restore unibridge-service metadata** (Postgres default; the script also accepts legacy SQLite snapshots):
   ```
   ./backup/restore.sh unibridge-meta ./snapshots/<stamp>
   ```
5. **Smoke test**: log in via Keycloak, call a known API key endpoint, verify a dynamic route works, hit `/metrics` on APISIX.

## Verifying backups

A backup you haven't tested restoring is a wish, not a backup. Recommended drill (quarterly):

1. Spin up a disposable environment from the same compose file.
2. Follow the full-disaster recovery order above with the latest backup.
3. Log in, exercise one endpoint of each kind (query, llm, s3).
4. Record the date of the last successful drill.

## Troubleshooting

- **`docker compose exec` fails with "no container"**: a service is down. Start it (`docker compose up -d <svc>`) before running backup. On a blue-green host, start infra services with `docker compose -p unibridge-infra -f docker-compose.infra.yml up -d <svc>` and app colors with `scripts/deploy-bluegreen.sh` — a plain `docker compose up -d` would boot a second single-stack instance onto the same volumes.
- **`cannot resolve volume for '<service>'`**: the service's container has never been created in this project. Materialize the volume first, then retry: `docker compose up -d` on a single stack, or `docker compose -p unibridge-infra -f docker-compose.infra.yml up -d` on a blue-green host (stateful services live in the infra stack).
- **etcd snapshot size is suspiciously small (<10KB)**: snapshot likely failed silently. Check that `ETCD_ROOT_PASSWORD` matches `.env` and that the `etcd` container is healthy. An empty-but-valid etcd snapshot is ~20KB.
- **Postgres restore hangs on `DROP TABLE`**: the consumer service is still connected. The restore script stops the known consumers automatically; if you invoked the library function directly, pass the consumer service name.
- **Metadata restore leaves APISIX serving with stale consumer cache**: unibridge-meta restore does not restart APISIX. If API keys were changed, restart it to clear its in-memory consumer cache: `docker compose restart apisix`, or `docker compose -p unibridge-infra -f docker-compose.infra.yml restart apisix` on a blue-green host.
- **`another backup/restore is already running`**: flock is held by an in-flight run. Check for orphan processes if you're sure none is running, then remove `.backup.lock`.
