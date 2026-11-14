# UniBridge (API Hub)

Internal API/DB gateway platform: register databases (Postgres/MSSQL/ClickHouse/Neo4j),
run SQL through one endpoint, manage APISIX routes, proxy LLMs via Bifrost, browse
S3/NAS — all behind Keycloak OIDC + RBAC + API keys. See `README.md` for deployment.

## Repo layout (monorepo, 3 code services + infra config)
- `unibridge-service/` — FastAPI backend (Python 3.12). `app/{routers,services,middleware}`,
  SQLAlchemy async + Alembic. Meta store = Postgres `unibridge-db` in every compose layout
  (blue/green needs a networked DB); SQLite `data/meta.db` is only the bare-code default. See its CLAUDE.md.
- `unibridge-ui/`      — React 19 + TS + Vite, served by nginx. TanStack Query,
  react-router 7, keycloak-js, i18next, recharts. See its CLAUDE.md.
- `llm-converter/`     — FastAPI sidecar: translates Anthropic `/v1/messages` and
  OpenAI `/v1/responses` → chat/completions for Bifrost. See its CLAUDE.md.
- `e2e/`               — live end-to-end tests (skipped unless `LLM_API_KEY` set).
- `apisix/ bifrost/ keycloak/ prometheus/` — infra config. `docker-compose.yml` — full stack
  (single-stack/dev). **Production runs the split blue-green layout instead**:
  `docker-compose.infra.yml` (project `unibridge-infra`; APISIX/etcd, Keycloak, Bifrost, monitoring, DBs) +
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
E2E (`cd e2e`): `pytest -v`   (needs `LLM_API_KEY`; see e2e/README.md)
Full stack: `docker compose up -d`
Deploy/infra: `unibridge-service/tests/test_deployment_security.py` (~10s alone) pins both
compose layouts (env parity, Bifrost), `scripts/deploy-bluegreen.sh`,
`backup/` and the nginx confs. Script tests `source` the script under `bash -c` with
`compose_infra`/`docker`/`curl` redefined as shell functions; one runs a real
`docker compose config --services` (skipped without the CLI).
Prometheus rules: `test_deployment_security.py` runs `promtool check rules` and the rule unit
tests in `prometheus/tests/unibridge-alerts.test.yml` (kept outside `rules/` so Prometheus does
not load them) with the compose-pinned image, skipped without Docker or the image. By hand:
`docker run --rm -v "$PWD/prometheus":/w:ro -w /w --entrypoint promtool prom/prometheus:v3.11.1 check rules rules/unibridge-alerts.yml`
(`test rules tests/unibridge-alerts.test.yml` for the unit tests).

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
  Keycloak, Bifrost, Grafana and DB secrets); `validate_settings()` separately rejects a
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
- **Upload relay** (`app/routers/upload_relay.py`, page `/upload-relay`; README "Upload relay for
  Noah"): Noah users upload on UniBridge's address and the Noah server PULLS the file; UniBridge
  never calls Noah. The ticket and request-signature formats in `app/services/upload_relay.py` are
  a wire contract with the Noah deployment (fixed vectors in `tests/test_upload_relay.py`): do not
  change a prefix, encoding or field; additions only. The browser endpoints sit behind the
  ordinary `get_current_user` (an APPROVED account, any app role; user decision 2026-11-12, the
  earlier role-less allowance was withdrawn: do not bring it back), the Noah-server ones behind
  the request signature. Auth first, then the ticket (header `X-Upload-Relay-Ticket`) is
  verified BEFORE the body is read, and an attempt without a valid ticket is logged, never
  audited. The limits are counted and inserted inside `relay.storing()` (asyncio lock + Postgres
  advisory lock); a new limit goes in `_no_room_for`. Files wait in the meta DB
  (`upload_relay_files`), never in process memory, so both colors serve them, and
  `backup/lib/meta.sh` leaves that table's rows out of every backup. The page renders outside
  `AuthProvider` (`unibridge-ui/src/Root.tsx`) and shows its own pending-approval state.
- **MCP artifacts** (`app/services/mcp_artifacts.py`; README "MCP server", File artifacts):
  `download_artifact` stages a copy in the meta DB (`mcp_artifacts`, both colors serve it;
  `backup/lib/meta.sh` skips its rows) and returns an id; the bytes cross on
  `/api/mcp/artifacts`, extra paths of the `mcp-api` route (a route without them makes the
  deploy re-provision). `return_mode=resource` instead returns a small file (unstaged) as a
  standard MCP `EmbeddedResource`; its shape (`artifact://<sha256>/<name>`, `mimeType`,
  `blob`, `_meta.size`/`_meta.sha256`) is what pi-gateway saves into a sandbox, so change it
  only together with that host. Only that block is standard MCP: staging by id and
  `upload_staged_artifacts` are UniBridge extensions, and docs must keep saying so.
  `upload_staged_artifacts` sends multipart parts or, with `json`, a JSON body whose
  `{"$artifact": id}` objects become the file's base64: that body is streamed
  (`_json_with_artifacts`) — built whole it costs over four times the file in memory —
  and `call_api`'s `body` stays literal (no references there). `stage_artifacts` takes a workspace file
  through the MCP connection: its `file` argument carries `"x-workspace-file": true` (`mcp_tools.WORKSPACE_FILE_KEYWORD`),
  pi-gateway's mark for "the host fills this in" — the object's keys (`filename`, `mimeType`, `size`, `sha256`, `data`) are
  what pi-gateway sends, so change them only together with that host. Keep it that way round: sources are read through
  the gateway REST routes as the caller and destinations are paths on an exposed route
  (`mcp_tools._resolve_api_call`), never a URL or a local path; an id answers only its own
  key, and every miss is the same 404. Limits live in the MCP settings, not env.
- **Template samples** (`query_templates.sample_params`; README "Query template sample
  checks"): run once on save (`query.verify_template_sample`: refused on a failed run or
  0 rows) as the CALLER, through `_run_query` — never as the service, or a key without
  `query-api` gets a query oracle; `_run_query` rolls the session back, so reload ORM rows
  after it (`_reload_template`). Then the alert checker runs them as rule
  `template_health`, its own task on its own interval (`template_check_interval_minutes`,
  0 = off; laid out from 08:00 KST, so the default 1440 = daily at 08:00 KST; due per
  template from the persisted `checked_at`: `_template_check_due`), never inside the
  health cycle. Those runs go through
  `query.run_template_sample` as the service itself: no audit row, no query metric, or
  they would count as the template's usage. Template alert state is dropped only by the
  checker (`_forget_unchecked_template_states`, which mails the closing notice), not by
  the template routes. The param names a sample
  must carry come from `openapi_export.extract_template_params(sql, db_type)`, the same
  call MCP `run_template` checks with: a new placeholder syntax goes there.
- **Bifrost** (`bifrost`, `bifrost-tls`; identical in both compose layouts, tested) is the
  only LLM gateway: it serves `/api/llm` (next bullet). The old `/api/llm-bi` side-by-side
  routes are gone: every provisioning boot deletes the
  `llm-bi-*` routes and the `llm-converter-bi` upstream (`main.py::_retire_llm_bi_routes`)
  and keeps only `llm-bi-not-found`, a 404 pointing to `/api/llm`; its ids stay protected
  and in the LLM-route metric exclusions for old series. `BIFROST_TEST_VK` keeps its name
  but is the virtual key the `/api/llm` routes inject as `x-bf-vk`; the deploy script
  string-matches it in route JSON, hence the URL-safe-only key rule shared by
  `llm_routes.usable_virtual_key` and the deploy's `bifrost_vk_usable`.
  `bifrost/tls-proxy.conf` (the LLM Monitoring page's Bifrost button) is an allowlist:
  UI + management API only, never inference or `/metrics`. With `BIFROST_UI_HOSTNAME` the UI nginx serves the same
  allowlist on the UniBridge port under that DNS name (`unibridge-ui/nginx-bifrost-ui.conf`,
  test-pinned to match). Bifrost's UI only works at the root of an origin: never a path.
  The name may have its own certificate (`TLS_BIFROST_CERT_PATH`/`_KEY_PATH`, both or
  neither; compose falls back to the UniBridge pair). Whatever ends TLS presents it, so on
  blue-green the deploy appends a server for the name to the edge config
  (`deploy/edge/bifrost-ui.conf.template`, same hop as the default server but without its
  `add_header`s) while `BIFROST_UI_HOSTNAME` is set. `check_bifrost_ui_host` refuses a bad
  pair on `deploy`; `promote`/`rollback` refuse only what the edge cannot start with.
  Under that name admins are signed in by `app/routers/bifrost_sso.py` (one-time code →
  Bifrost session cookie, set on that host only; OSS Bifrost has no SSO). Bifrost keeps
  sessions 30 days, so `app/services/bifrost_sessions.py` logs each handed-out one out
  after `BIFROST_SSO_SESSION_HOURS` (tokens encrypted in `bifrost_sso_sessions`).
- **LiteLLM is gone** (removed after production moved to Bifrost on 2026-10-18). `/api/llm`
  route ids stayed so grants carried over (`app/services/llm_routes.py`); boot provisioning
  deletes the `llm-admin` route and the `litellm` upstream (`main.py::_retire_litellm_routing`)
  and the deploy re-provisions while either is left. `llm-proxy` is exact paths +
  `llm-not-found` 404s the rest (Bifrost's UI/management API must never be reachable).
  Rollback guard, one release only: compose passes a literal `LLM_GATEWAY=bifrost` to the
  service, converter and UI so an older image recreated on rollback doesn't default to litellm;
  nothing current reads it, and the deploy refuses any other `.env` value. Leftover
  containers, volumes, dumps and the Keycloak client are cleaned up by hand (README
  "Upgrading past the LiteLLM removal"), never by code.
- **`unibridge_*` metrics are scraped by two jobs**: `unibridge-service` is a DNS alias both
  blue/green colors answer on (round-robin → counters alternate → `increase()` inflates);
  `unibridge-service-colors` is per-color. PromQL over them (backend, Grafana or alert
  rules) must use the gated form in `app/routers/query_metrics.py::_gated` — never sum the
  two jobs.
  A gauge that reports a color's setting must count only the active color
  (`and on(job, instance) unibridge_active_instance == 1`): a standby or demoted color
  reports stale values.
- **Prometheus does not pick up a pulled change under `prometheus/` by itself**:
  `prometheus.yml` is a single-file bind mount (the container keeps the old file), there is
  no reload API (no `--web.enable-lifecycle`), and deploys never touch infra. Ship such a
  change with a README step, ahead of any deploy that relies on it:
  `docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --force-recreate prometheus`
  (data volume stays). Grafana polls `grafana/dashboards/` every 30s and needs nothing.
- **Network map** (`app/routers/network.py`, rules in `app/services/network_map.py`, guide
  `docs/network-map.md`): same region = open, cross-region only through a recorded open
  opening. API keys write through `network-write-api` and land as `source=agent`, unverified;
  regions, verify, deleting a verified record, settings and import stay people-only
  (`network_person_writer`) — keep new agent-reachable writes behind that review.
  Discovered usages name UniBridge's own config, so non-master keys get them redacted.
- **TZ=UTC everywhere**; timestamps stored UTC (legacy SQLite rows: `unibridge-service/scripts/backfill_utc_timestamps.py`).
- `.omc/`, `docs/superpowers/`, `certs/`, `unibridge-service/data/` are gitignored — tooling
  artifacts / local state, not app code.
