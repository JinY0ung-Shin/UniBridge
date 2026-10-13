# UniBridge (API Hub)

Internal API/DB gateway platform: register databases (Postgres/MSSQL/ClickHouse/Neo4j),
run SQL through one endpoint, manage APISIX routes, proxy LLMs via LiteLLM or Bifrost, browse
S3/NAS — all behind Keycloak OIDC + RBAC + API keys. See `README.md` for deployment.

## Repo layout (monorepo, 3 code services + infra config)
- `unibridge-service/` — FastAPI backend (Python 3.12). `app/{routers,services,middleware}`,
  SQLAlchemy async + Alembic. Meta store = Postgres `unibridge-db` in every compose layout
  (blue/green needs a networked DB); SQLite `data/meta.db` is only the bare-code default. See its CLAUDE.md.
- `unibridge-ui/`      — React 19 + TS + Vite, served by nginx. TanStack Query,
  react-router 7, keycloak-js, i18next, recharts. See its CLAUDE.md.
- `llm-converter/`     — FastAPI sidecar: translates Anthropic `/v1/messages` and
  OpenAI `/v1/responses` → chat/completions for LiteLLM or Bifrost. See its CLAUDE.md.
- `e2e/`               — live end-to-end tests (skipped unless `LLM_API_KEY` set).
- `apisix/ bifrost/ keycloak/ litellm/ prometheus/` — infra config. `docker-compose.yml` — full stack
  (single-stack/dev). **Production runs the split blue-green layout instead**:
  `docker-compose.infra.yml` (project `unibridge-infra`; APISIX/etcd, Keycloak, LiteLLM, Bifrost, monitoring, DBs) +
  `docker-compose.app.yml` (blue/green) + `docker-compose.edge.yml`, driven by
  `scripts/deploy-bluegreen.sh` (see `docs/blue-green-deploy.md`). Any compose change —
  volumes, mounts, env, images — must be applied to BOTH `docker-compose.yml` and the
  split files, or production silently misses it.

## Commands
Backend (`cd unibridge-service`):
```bash
ruff check app/                              # lint (release gate; scope is app/ only)
pytest tests/ -v --tb=short                  # tests (in-memory SQLite)
alembic -c alembic.ini upgrade head          # apply migrations
alembic -c alembic.ini check                 # release gate fails if models drifted from migrations
uvicorn app.main:app --reload                # run locally (listens on :8000 — UI dev proxy expects this)
```
Frontend (`cd unibridge-ui`):
```bash
npm ci                                       # install from package-lock
npm run dev                                  # dev server
npm run lint                                 # eslint . -- passes on warnings; the gate runs --max-warnings=0
npm run test                                 # vitest run
npm run build                                # tsc -b && vite build
```
Converter (`cd llm-converter`): `pytest`; `ruff check app tests` (the gate lints tests here)
LiteLLM config/callbacks (repo root): `python3 -m pytest litellm/tests/`
E2E (`cd e2e`): `pytest -v`   (needs `LLM_API_KEY`; see e2e/README.md)
Full stack: `docker compose up -d`
Deploy/infra: `unibridge-service/tests/test_deployment_security.py` (~10s alone) pins both
compose layouts (env parity, Bifrost, the LiteLLM gate), `scripts/deploy-bluegreen.sh`,
`backup/` and the nginx confs. Script tests `source` the script under `bash -c` with
`compose_infra`/`docker`/`curl` redefined as shell functions; one runs a real
`docker compose config --services` (skipped without the CLI).
Prometheus rules: tests only parse the YAML, so check PromQL with the compose-pinned image:
`docker run --rm -v "$PWD/prometheus":/w:ro -w /w --entrypoint promtool prom/prometheus:v3.11.1 check rules rules/unibridge-alerts.yml`
(`test rules <file>` for rule unit tests; mount a dir holding the rules and the test file).

Release gate — the only gate, there is no CI: the `unibridge-release` skill (canonical copy
`.claude/skills/unibridge-release/`; Codex: `$unibridge-release`); its
`scripts/run-local-release-checks.sh` is the step list. E2E runs live whenever `LLM_API_KEY`
is set, including from the gitignored `e2e/.env` that `e2e/conftest.py` auto-loads, so
`RUN_LIVE_E2E` only relabels that step; check skip health with `LLM_API_KEY= pytest`.

## Gotchas
- **Migrations auto-apply at boot**: `app.main` lifespan → `init_db()` → `alembic upgrade head`.
  Adding/changing a model REQUIRES a new `alembic/versions/NNNN_*.py`, or `alembic check`
  (release gate) fails and real DBs miss the column. Tests skip this (in-memory SQLite → `create_all`
  + stamp head), so a missing migration passes tests but breaks deploys.
- **Test env is set in `tests/conftest.py` before app import** (in-memory SQLite, dev JWT,
  `ENABLE_DEV_TOKEN_ENDPOINT=true`). Don't rely on a real `.env` in tests.
- **Fail-fast secrets**: compose `${VAR:?}` refuses to start without its secrets (APISIX,
  Keycloak, LiteLLM, Bifrost, Grafana and DB secrets); `validate_settings()` separately rejects a
  missing/placeholder ENCRYPTION_KEY (must yield a Fernet key), APISIX_ADMIN_KEY,
  APISIX_INTERNAL_PROXY_SECRET, and JWT_SECRET in dev HS256 mode. Keycloak/CORS URLs
  auto-derive from `HOST_IP`; Keycloak URLs derive only when `KEYCLOAK_JWT_AUDIENCE` is set
  (empty = dev HS256 mode).
- **RBAC**: flat string permissions in `app/auth.py::ALL_PERMISSIONS`; roles→permissions with
  a 60s in-process cache (`invalidate_permission_cache()` after changes).
- **APISIX consumer-restriction is preserved** on route updates for the fixed system routes
  in `main.py::_preserve_consumer_restriction`'s id set (constants in
  `app/services/apisix_system_resources.py`) — don't clobber it; a new fixed route joins that set.
- **Per-node path prefixes run on a custom APISIX plugin**
  (`apisix/plugins/unibridge-node-path-prefix.lua`, prefix in a node's `metadata.path_prefix`):
  listed in `apisix/config.yaml`, mounted by both compose files that run APISIX, switched on
  by a global rule the service reconciles at boot and before saving a prefixed upstream.
  A changed mount or plugin file needs a new APISIX container (a single-file bind mount
  keeps the old file) and normal deploys leave infra alone:
  `docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --force-recreate apisix`.
  nginx fixes the path before the first attempt, so a retry onto a node with another prefix
  ends in 502; upstreams that mix prefixes are therefore saved with TCP health checks with
  explicit intervals (`node_path_prefix.MIXED_PREFIX_CHECKS`), plus `retries: 0` when no two
  nodes share a prefix. Deleting the global rule is no off switch (the service puts it
  back); drop the plugin from `config.yaml` instead. It reads APISIX internals: run
  `scripts/test-apisix-node-path-prefix.sh` after changing it or upgrading APISIX.
- **Request routing**: UI nginx → `/_api/*` = unibridge-service, `/api/*` = APISIX gateway.
- **LiteLLM admin UI = Keycloak SSO, admins only** (`ui_access_mode: admin_only` +
  realm `admin` → `litellm:proxy_admin` composite). `GENERIC_CLIENT_USE_PKCE=true` is
  load-bearing, not hardening — without it logins 500 on Keycloak's userinfo issuer
  check. Free tier caps LiteLLM's user table at 5 rows and rejected non-admin logins
  still insert rows. See README "LiteLLM admin UI SSO".
- **Bifrost runs side by side with LiteLLM on `/api/llm-bi`** (`bifrost`, `bifrost-tls`,
  `llm-converter-bi`; identical in both compose layouts, tested). Its routes are system
  routes provisioned by `main.py::_provision_bifrost_routes` from
  `app/services/bifrost_routes.py` while `BIFROST_GATEWAY_ROUTES` is on (false removes
  them; `llm-bi-not-found` explains the 404 in every state). The deploy script's
  `apisix_has_core_routes` holds them to that switch and `BIFROST_TEST_VK`, so a flip or
  key rotation re-provisions on the next deploy; it string-matches the key in route JSON,
  hence the URL-safe-only key rule shared by `usable_virtual_key` and `bifrost_routes_state`.
  `bifrost/tls-proxy.conf` (the LLM Monitoring page's Bifrost button) is an allowlist:
  UI + management API only, never inference or `/metrics`. With `BIFROST_UI_HOSTNAME` the UI nginx serves the same
  allowlist on the UniBridge port under that DNS name (`unibridge-ui/nginx-bifrost-ui.conf`,
  test-pinned to match). Bifrost's UI only works at the root of an origin: never a path.
  Under that name admins are signed in by `app/routers/bifrost_sso.py` (one-time code →
  Bifrost session cookie, set on that host only; OSS Bifrost has no SSO). Bifrost keeps
  sessions 30 days, so `app/services/bifrost_sessions.py` logs each handed-out one out
  after `BIFROST_SSO_SESSION_HOURS` (tokens encrypted in `bifrost_sso_sessions`).
- **`LLM_GATEWAY=litellm|bifrost` picks the gateway behind `/api/llm`** (service, per-color
  converter and deploy gate read the same `.env` value; default litellm). Route ids stay the
  same so grants carry over (`app/services/llm_routes.py`). The converter routes carry BOTH
  gateways' credentials and the converter forwards only its own: a deploy rewrites routes at
  the new color's boot but moves the converter upstream only at promotion. On Bifrost,
  `llm-proxy` is exact paths + `llm-not-found` 404s the rest (Bifrost's UI/management API
  must never be reachable). README "Switching /api/llm to Bifrost".
  LiteLLM and `litellm-db` run only on `litellm`: both `extends` the `litellm/compose-gate.yml`
  entry named by the raw value (bifrost = never-enabled profile; any other spelling breaks
  every compose command, and the deploy refuses it and bifrost without a usable VK), so the
  infra health gate drops them. A `depends_on` on either needs `required: false` (Compose
  2.20+), or under bifrost every compose command fails ("depends on undefined service");
  with `service_healthy` it still waits, and an unhealthy one only warns. The deploy stops
  them only once no running color's converter
  forwards to LiteLLM (asked via `docker exec`, since a recreated standby keeps its old image)
  and APISIX has no `litellm` upstream; provisioning on Bifrost deletes `llm-admin` + that
  upstream, the UI hides the LiteLLM button, and `LiteLLMDbDown`, Grafana's gateway/DB panels
  (via `unibridge_llm_gateway`) leave them out; `backup.sh` carries the last dump forward.
- **`unibridge_*` metrics are scraped by two jobs**: `unibridge-service` is a DNS alias both
  blue/green colors answer on (round-robin → counters alternate → `increase()` inflates);
  `unibridge-service-colors` is per-color. PromQL over them (backend or Grafana) must use
  the gated form in `app/routers/query_metrics.py::_gated` — never sum the two jobs.
  A gauge that reports a color's setting (`unibridge_llm_gateway{gateway}`) counts only from
  the active color (`and on(job, instance) unibridge_active_instance == 1`: a standby or
  demoted color reports stale values); a rule that goes quiet on it uses
  `unless on() (max(...) == 1)`, so an image without the gauge still alerts (`LiteLLMDbDown`).
- **Prometheus does not pick up a pulled change under `prometheus/` by itself**:
  `prometheus.yml` is a single-file bind mount (the container keeps the old file), there is
  no reload API (no `--web.enable-lifecycle`), and deploys never touch infra. Ship such a
  change with a README step, ahead of any deploy that relies on it:
  `docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --force-recreate prometheus`
  (data volume stays). Grafana polls `grafana/dashboards/` every 30s and needs nothing.
- **TZ=UTC everywhere**; timestamps stored UTC (legacy SQLite rows: `unibridge-service/scripts/backfill_utc_timestamps.py`).
- `.omc/`, `docs/superpowers/`, `certs/`, `unibridge-service/data/` are gitignored — tooling
  artifacts / local state, not app code.
