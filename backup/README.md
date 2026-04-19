# UniBridge Backup & Restore

Operator runbook for backing up and restoring UniBridge state.

**Directory naming**: this source directory is `backup/` and is tracked in git. Runtime snapshot output lands in `snapshots/` (gitignored) under the project root. The two names are intentionally distinct to prevent confusion between code and runtime artifacts.

## What's backed up

| Component | Source | Output | Why critical |
|---|---|---|---|
| etcd | volume `etcd-data` | `etcd.snap` | APISIX routes, consumers, plugin configs |
| unibridge-service SQLite | volume `unibridge-data` (`meta.db`) | `unibridge-meta.db.gz` | API keys, encrypted credentials, user settings |
| Keycloak Postgres | volume `keycloak-db-data` | `keycloak-db.sql.gz` | users, realms, clients |
| LiteLLM Postgres | volume `litellm-db-data` | `litellm-db.sql.gz` | LLM keys, budgets, usage history |

Prometheus time-series data is intentionally **not** backed up — retention is already configured in the Prometheus container and the data is regeneratable over time.

## Layout

```
<project-root>/snapshots/<YYYY-MM-DD_HHMMSSZ>/
  etcd.snap
  keycloak-db.sql.gz
  litellm-db.sql.gz
  unibridge-meta.db.gz
  manifest.json          # sizes + SHA256 of each file
```

File permissions are set to `600`, the per-run directory to `700`. Backups contain secrets (encrypted credentials, session data, LLM keys) — protect the host filesystem accordingly and do not world-share backups.

`.env` must be shell-sourceable (values with spaces or shell metacharacters must be quoted). The backup scripts source it to pick up DB passwords.

### Host prerequisites

- `docker` + `docker compose` plugin (obviously)
- `bash`, `flock`, `find`, `sha256sum`, `gzip`, `sqlite3`-in-container (all standard)
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
- Stops the consumer service (Keycloak / LiteLLM / unibridge-service / apisix) before touching its backing store, then restarts it.

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
4. **Restore unibridge-service metadata** (this stops/starts the service on its own):
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

- **`docker compose exec` fails with "no container"**: a service is down. Start it (`docker compose up -d <svc>`) before running backup.
- **`cannot resolve volume for '<service>'`**: the service's container has never been created in this project. Run `docker compose up -d` first so compose materializes the volume, then retry.
- **etcd snapshot size is suspiciously small (<10KB)**: snapshot likely failed silently. Check that `ETCD_ROOT_PASSWORD` matches `.env` and that the `etcd` container is healthy. An empty-but-valid etcd snapshot is ~20KB.
- **Postgres restore hangs on `DROP TABLE`**: the consumer service is still connected. The restore script stops the known consumers automatically; if you invoked the library function directly, pass the consumer service name.
- **SQLite restore leaves APISIX serving with stale consumer cache**: unibridge-meta restore does not restart APISIX. If API keys were changed, `docker compose restart apisix` to clear its in-memory consumer cache as well.
- **`another backup/restore is already running`**: flock is held by an in-flight run. Check for orphan processes if you're sure none is running, then remove `.backup.lock`.
