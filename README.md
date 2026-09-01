# API Hub (UniBridge)

Internal API/DB gateway platform. Register multiple databases (PostgreSQL, MSSQL, ClickHouse), execute SQL through a single endpoint, manage API routes via APISIX, and control access with RBAC + API keys.

## Architecture

```
Browser ──HTTPS──> unibridge-ui (nginx)
                       │
                       ├── /_api/* ──> unibridge-service (FastAPI)
                       └── /api/*  ──> apisix (API Gateway)
                                          │
                                          ├── /api/query/*     → Registered databases (Postgres, MSSQL, ClickHouse)
                                          ├── /api/llm/v1/messages
                                          │                  → llm-converter → LiteLLM
                                          ├── /api/llm/v1/responses
                                          │                  → llm-converter → LiteLLM
                                          ├── /api/llm/v1/models
                                          │                  → llm-converter (listing + claude/ aliases)
                                          ├── /api/llm/metrics → LiteLLM raw Prometheus /metrics
                                          ├── /api/llm/*       → LiteLLM (LLM proxy)
                                          ├── /api/llm-admin/* → LiteLLM Admin UI/API
                                          ├── /api/s3/*        → S3 connections
                                          ├── /api/nas/*       → Mounted NAS/local files
                                          ├── /api/prometheus/* → Prometheus HTTP API (PromQL, read-only)
                                          └── Custom upstream services

Keycloak   ── OIDC auth
Prometheus ── APISIX/LiteLLM/FastAPI metrics + DB TCP probes
LiteLLM    ── Unified LLM proxy (+ Postgres)
```

**Services (11):** etcd, APISIX, Keycloak + Postgres, unibridge-service, Prometheus, Blackbox Exporter, Grafana, LiteLLM + Postgres, unibridge-ui

## Prerequisites

- Docker & Docker Compose v2
- TLS certificate pair (`tls.crt`, `tls.key`)

## Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/JinY0ung-Shin/UniBridge.git
cd UniBridge
cp .env.example .env
```

### 2. Edit `.env`

**Must set before first boot:**

`.env.example` intentionally leaves deployment secrets blank. After `cp .env.example .env`, fill in the values below before running `docker compose up`.

| Variable | Description |
|----------|-------------|
| `ENCRYPTION_KEY` | Fail-fast secret used to encrypt stored database credentials. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `KC_ADMIN_PASSWORD` | Fail-fast secret for the Keycloak admin console |
| `KC_DB_PASSWORD` | Fail-fast secret for the Keycloak database |
| `APISIX_ADMIN_KEY` | Fail-fast secret for the APISIX admin API |
| `APISIX_INTERNAL_PROXY_SECRET` | Fail-fast secret APISIX injects as `X-UniBridge-Internal-Proxy`; unibridge-service trusts gateway-set API-key identity headers only on requests carrying it. Generate a **dedicated** value with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` — never reuse `APISIX_ADMIN_KEY` |
| `KEYCLOAK_SERVICE_CLIENT_SECRET` | Fail-fast shared secret used by Keycloak and unibridge-service |
| `LITELLM_DB_PASSWORD` | Fail-fast secret for the LiteLLM database |
| `LITELLM_MASTER_KEY` | Fail-fast secret for LiteLLM admin/API access |
| `ETCD_ROOT_PASSWORD` | Set this unless `ETCD_ALLOW_NONE_AUTH=yes` for dev-only etcd without auth |
| `HOST_IP` | Server IP or hostname that browsers access (not `localhost` in production) |
| `JWT_SECRET` | Required when not using Keycloak-issued tokens; generate a separate strong value |

> **Upgrading an existing deployment:** `APISIX_INTERNAL_PROXY_SECRET` used to fall back to
> `APISIX_ADMIN_KEY`, so it may be missing from an older `.env` — set it before the next deploy or
> Compose refuses to start the service. No manual gateway work is needed: unibridge-service
> rewrites the header value on its system routes at boot, so a rotated secret reaches APISIX even
> on a color that boots with `APISIX_PROVISION_ON_START=false`.
>
> Because APISIX routes are shared between the blue and green colors, *changing* the value
> interrupts gateway API-key traffic from the moment the new color boots until it is promoted (the
> old color still expects the old value) — rotate in a maintenance window. Deploys that leave the
> value alone are no-ops, and JWT/UI traffic is never affected.

**Optional:**

| Variable | Default | Description |
|----------|---------|-------------|
| `UNIBRIDGE_UI_PORT` | 3000 | HTTPS port for the web UI |
| `KEYCLOAK_PORT` | 8443 | Keycloak OIDC port |
| `KEYCLOAK_EXTERNAL_URL` | derived | Browser-facing Keycloak base URL for UI runtime config |
| `KEYCLOAK_DEV_MODE` | false | Set `true` to run Keycloak in dev mode (relaxed security) |
| `ETCD_ALLOW_NONE_AUTH` | no | Set `yes` to disable etcd authentication (dev only) |
| `ENABLE_DEV_TOKEN_ENDPOINT` | false | Set `true` only for local dev |
| `SSL_VERIFY` | true | Set `false` if using self-signed certs |
| `RATE_LIMIT_PER_MINUTE` | 60 | Per-user query rate limit |
| `MAX_CONCURRENT_QUERIES` | 5 | Per-user concurrent query limit |
| `NAS_HOST_PATH` | `/mnt/nas` | Host path bind-mounted read-only into `unibridge-service` for NAS browsing |
| `NAS_CONTAINER_PATH` | `/mnt/nas` | Container path where the NAS host path appears |
| `NAS_ALLOWED_ROOTS` | `NAS_CONTAINER_PATH` | Comma-separated container paths allowed as NAS connection `base_path` roots |
| `NODE_EXPORTER_DISK_MOUNTPOINTS` | empty | Optional global comma-separated disk mountpoint default for server monitoring; per-server settings override it |
| `S3_OP_TIMEOUT_SECONDS` | 30 | Per-operation timeout for S3-compatible storage calls |
| `ALERTMANAGER_WEBHOOK_TOKEN` | empty | Bearer token Alertmanager uses to POST fired Prometheus rules into the app's alert pipeline. **Empty means infra alerts send no mail** — the receiver answers 503. See [Infra alerting](#infra-alerting-prometheus--alertmanager) |
| `ALERTMANAGER_SMTP_HOST`, `ALERTMANAGER_SMTP_TO` | empty | Set both to add a direct-SMTP mail path for the service-down alerts, so that mail does not depend on the app being up. Empty = off. Companions: `ALERTMANAGER_SMTP_PORT` (25), `_FROM`, `_USERNAME`, `_PASSWORD`, `_REQUIRE_TLS` (true) |

### 3. TLS certificates

Place cert files in `certs/`:

```bash
# Self-signed (dev/test only)
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/tls.key -out certs/tls.crt \
  -days 365 -subj "/CN=${HOST_IP}"
```

Or copy your real certificates:

```bash
cp /path/to/your/cert.crt certs/tls.crt
cp /path/to/your/cert.key certs/tls.key
```

### 4. Start

```bash
docker compose up -d
```

First boot takes ~2 minutes (Keycloak initialization).

For near-zero-downtime updates with Docker Compose, use the split blue/green
stack instead of recreating the public UI container directly:

```bash
scripts/deploy-bluegreen.sh deploy blue   # first bootstrap
scripts/deploy-bluegreen.sh deploy        # later updates
```

See [`docs/blue-green-deploy.md`](docs/blue-green-deploy.md) for the split
Compose files, volume-name migration notes, rollback, and APISIX promotion
details.

### 5. Access

| Service | URL |
|---------|-----|
| Web UI | `https://<HOST_IP>:<UNIBRIDGE_UI_PORT>` |
| Keycloak Admin | `https://<HOST_IP>:<KEYCLOAK_PORT>/admin` |
| API Gateway | `https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/*` |
| LiteLLM | `https://<HOST_IP>:<LITELLM_PORT>` (admin UI at `/ui` signs in via UniBridge SSO, admins only) |
| Prometheus | `https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/prometheus/*` (API-key auth via gateway; direct `:9090` is localhost-only, `PROMETHEUS_BIND` overrides) |
| Grafana | `https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/grafana` (same-origin behind the UI) |

Default login: Keycloak admin console (`KC_ADMIN_USER` / `KC_ADMIN_PASSWORD`). No human users are seeded into the `apihub` realm by default. After first boot, sign in to the admin console and create the first `admin` user in the `apihub` realm (assign the `admin` realm role), then manage further users from the UI **Users** page.

### Codex through UniBridge

Codex can use UniBridge through the OpenAI-compatible Responses endpoint exposed at `/api/llm/v1/responses`. The gateway authenticates the caller with APISIX `key-auth`, injects the LiteLLM master key internally, and sends the request through `llm-converter`, which translates Responses API traffic to LiteLLM's `/v1/chat/completions` shape and translates the result back.

Configure Codex in your user-level `~/.codex/config.toml`:

```toml
model_provider = "unibridge"
model = "<LiteLLM model id>"

[model_providers.unibridge]
name = "UniBridge"
base_url = "https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/llm/v1"
wire_api = "responses"
env_http_headers = { "apikey" = "UNIBRIDGE_API_KEY" }
stream_idle_timeout_ms = 300000
```

Then export an API key that has LLM access:

```bash
export UNIBRIDGE_API_KEY="<UniBridge API key>"
```

Requirements and behavior:

- Put provider/auth settings in user config (`~/.codex/config.toml`), not project `.codex/config.toml`; Codex ignores provider and auth redirects from project config.
- Grant the API key LLM access. Granting the `llm-proxy` route also whitelists the converter routes it fronts — `llm-messages`, `llm-responses`, and `llm-models` (the model listing; see [Model discovery for Claude Code](#model-discovery-for-claude-code)). It does **not** cover `llm-metrics`, which exposes every key's usage and stays an explicit grant.
- Use a certificate Codex trusts. For self-signed dev certificates, install the CA locally or use a trusted certificate for the UniBridge UI endpoint.
- Codex reasoning effort is forwarded as Responses `reasoning.effort`; `llm-converter` maps it to upstream Chat Completions `reasoning_effort`.
- Streaming Responses events include `response.created`, `response.output_text.delta`, function-call argument deltas, terminal `response.completed` / `response.failed`, and monotonic `sequence_number` values.

### NAS mount

UniBridge does not mount SMB/NFS itself. Mount the NAS on the Docker host first, then point `NAS_HOST_PATH` at that mounted directory. Docker Compose exposes it read-only inside `unibridge-service` at `NAS_CONTAINER_PATH`.

```env
NAS_HOST_PATH=/srv/company-nas
NAS_CONTAINER_PATH=/mnt/nas
NAS_ALLOWED_ROOTS=/mnt/nas
```

After changing these values, recreate the service so Docker applies the bind mount:

```bash
docker compose up -d --force-recreate unibridge-service
```

In the UI, add a NAS connection with `base_path` set to `/mnt/nas` or a child directory such as `/mnt/nas/reports`. External API-key access then uses alias-relative paths. The browse APIs are read-only:

```http
GET /api/nas/company-nas/entries?path=reports&limit=100
GET /api/nas/company-nas/entries?path=reports&q=invoice&limit=100
GET /api/nas/company-nas/metadata?path=reports/2026/a.csv
GET /api/nas/company-nas/download?path=reports/2026/a.csv
POST /api/nas/company-nas/download-zip
     {"paths": ["reports/2026/a.csv", "reports/2026/b.csv"]}
```

The `q` parameter searches only the immediate `path` directory by case-insensitive file or folder name substring. It is not recursive.

`download-zip` streams the requested files as a single ZIP archive (`{alias}-files.zip`, no compression). Limits: at most `NAS_MAX_BATCH_FILES` paths per request (default 100), combined size capped by the connection's `max_download_bytes` and the global `NAS_MAX_DOWNLOAD_BYTES` (413 when exceeded). Any invalid or missing path fails the whole request before streaming starts, naming the offending relative path.

### Self-service registration (approval-gated)

The `apihub` realm has registration enabled (`registrationAllowed: true`), but registration is **approval-gated**: a new person can click **Register** on the Keycloak login page, but the new account receives **no application role** and cannot use the service. They see a "pending approval" screen; the backend rejects role-less tokens with 401. An **admin approves** the account by assigning the `user` role from the UI **Users** page (pending accounts show a *Pending* badge there).

Flow: `register → pending (no role) → admin assigns a role → access`.

> **Security note:** registration is open, so anyone who can reach the Keycloak login page can create a *pending* account. Pending accounts have no access and cannot mint API keys, so the blast radius of mass/bot registration is limited to Keycloak user-row growth (admins simply never approve them). `bruteForceProtected` is enabled (login lockout). For a fully public surface you may still want reCAPTCHA on the registration flow or network-restricting Keycloak, but neither is required for access control here — approval is the gate.

How it works (two realm settings, both applied by the helper):

1. `registrationAllowed = true` — shows the **Register** link.
2. The `user` role is **not** in the `default-roles-apihub` composite — so new users are role-less (pending) until an admin assigns a role. (Default roles only apply at user-creation time; existing users are unaffected.)

`registrationAllowed` ships in `keycloak/realm-export.json`, but that template is only read when Keycloak **first creates** the realm. Run the idempotent helper once on the Docker host to apply it to a running deployment (it also removes `user` from the default roles if present, guaranteeing the approval gate):

```bash
./keycloak/enable-self-registration.sh
# override container/realm if needed:
# KC_CONTAINER=unibridge-keycloak-1 KC_REALM=apihub ./keycloak/enable-self-registration.sh
```

The helper authenticates as the Keycloak master admin (the service account lacks `manage-realm`), errors out clearly on zero/multiple container matches or auth failure (printing an admin-console fallback), and is safe to re-run.

**To disable registration entirely:** set `registrationAllowed=false` (admin console → Realm settings → Login → *User registration*, or `kcadm.sh update realms/apihub -s registrationAllowed=false` inside the Keycloak container as master admin).

## Service Ports (default)

| Port | Service | Binding |
|------|---------|---------|
| 3000 | unibridge-ui (HTTPS) | public |
| 8443 | Keycloak (HTTPS) | public |
| 4000 | LiteLLM (HTTPS) | public |
| 8000 | unibridge-service | localhost only |
| 9180 | APISIX admin | localhost only |
| 9090 | Prometheus | localhost only (`PROMETHEUS_BIND` overrides; external access via gateway `/api/prometheus/*`) |
| 3300 | Grafana | localhost only (debug; public access is `/grafana` on the UI port) |

## Local Development (without Docker)

### Backend

```bash
cd unibridge-service
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov

# Minimal .env for temporary backend-only dev
# Do not reuse these example values for Docker/production deployments.
export META_DB_URL="sqlite+aiosqlite:///data/meta.db"
export ENCRYPTION_KEY="dev-key-change-in-prod-32chars!"
export JWT_SECRET="dev-jwt-secret"
export ENABLE_DEV_TOKEN_ENDPOINT=true

uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd unibridge-ui
npm ci
npm run dev   # http://localhost:5173, proxies /_api to :8000
```

### Tests

```bash
# Backend
cd unibridge-service && pytest tests/ -v

# Frontend
cd unibridge-ui && npx vitest run

# Full production-code coverage (backend + converter + frontend)
./scripts/run-coverage.sh
# Reports are written to /tmp/unibridge-coverage by default.
```

## Common Operations

```bash
# Restart all
docker compose restart

# Rebuild after code change
docker compose up -d --build

# View logs
docker compose logs -f unibridge-service
docker compose logs -f keycloak

# Stop
docker compose down

# Stop and remove volumes (DESTROYS DATA)
docker compose down -v
```

Operational defaults in `docker-compose.yml`:

- All services use `restart: unless-stopped`, so containers come back after host/container restarts unless intentionally stopped.
- Docker `json-file` logs rotate at `50m` with `5` retained files per service.
- Each service has an initial `deploy.resources.limits` CPU/memory cap for Docker Compose v2, plus `mem_limit`/`cpus` fallbacks for older Compose compatibility. Treat these as conservative starting values and tune from `docker stats` on the deploy host.
- `unibridge-service` and `unibridge-ui` run with `init: true` for PID 1 signal handling and child process reaping.
- Prometheus scrapes APISIX, LiteLLM, unibridge-service `/metrics`, and Blackbox TCP probes for the Postgres-backed services. Alert rules live under `prometheus/rules/`.

### Prometheus query API through the gateway

Prometheus has no authentication of its own, so its port stays on loopback
(`PROMETHEUS_BIND`, default `127.0.0.1`) and external PromQL goes through the
fixed gateway route `prometheus-api` instead — APISIX `key-auth` plus the same
per-key route grants as every other built-in route (grant the `prometheus-api`
route to a key on the **API Keys** page). The path after `/api/prometheus` is
passed through unchanged, so any Prometheus HTTP API endpoint works:

```bash
curl -H "apikey: $UNIBRIDGE_API_KEY" \
  "https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/prometheus/api/v1/query?query=up"
```

The route accepts `GET` and `POST` (the latter for form-encoded `/api/v1/query`
bodies too long for a URL). Everything reachable through it is read-only: the
Prometheus container runs without `--web.enable-admin-api` and
`--web.enable-lifecycle`, so there is no delete-series, snapshot, or reload
endpoint to call. Note that a key with this route sees **every** metric in the
stack — there is no per-key metric scoping, unlike the in-app monitoring pages.

### Model discovery for Claude Code

`GET /api/llm/v1/models` lists every registered LiteLLM model, and lists each one
a second time under a `claude/` prefix:

```bash
curl -H "apikey: $UNIBRIDGE_API_KEY" \
  "https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/llm/v1/models"
```

The aliases exist because Claude Code's discovery keeps only model ids
containing `claude` or `anthropic` (case-insensitive substring since v2.1.223;
older builds require the id to *start* with one), and no LiteLLM deployment name
does — `qwen3.5-32b` is dropped by the client, `claude/qwen3.5-32b` is kept. The
aliases are not decorative: `claude/qwen3.5-32b` is callable at
`/api/llm/v1/messages` exactly like `qwen3.5-32b`, because `llm-converter` strips
the prefix on the way in. Every entry carries both the OpenAI and Anthropic field
sets — including the `display_name` Claude Code requires — so either client's
parser reads the same response. Set `CONVERTER_MODEL_ALIAS_PREFIX=""` to turn
aliasing off entirely.

**Enabling it in Claude Code.** Discovery is opt-in and needs two environment
variables:

```bash
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1      # v2.1.129+
export ANTHROPIC_BASE_URL="https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/llm"
```

Authenticate the same way as [Codex](#codex-through-unibridge) — the gateway
wants the key in an `apikey` header, which Claude Code sends via
`ANTHROPIC_CUSTOM_HEADERS`; discovery includes those custom headers, so one
setting covers both discovery and the Messages calls that follow. Without
`ANTHROPIC_BASE_URL` discovery does not run at all.

What the client does, and what it therefore needs from the deployment: it issues
`GET /v1/models?limit=1000` (the query param is accepted and ignored — the
listing is a single un-paginated page), gives up after a **3-second** timeout,
does **not** follow redirects, and does **not** send `anthropic-version` on this
request. So the endpoint must answer directly and quickly: it makes one hop to
LiteLLM and nothing else. A slow or redirecting front end breaks discovery even
though the same URL works fine under `curl`.

The route has its own **`llm-models`** grant, but any key already granted
`llm-proxy` gets it implicitly — the same way `llm-messages` and `llm-responses`
are implied — so existing LLM keys discover models with no re-grant. The
implication is one-directional: granting `llm-models` *alone* produces a
discovery-only key that can list models and invoke none of them.

### LiteLLM raw `/metrics` through the gateway

LiteLLM publishes its own Prometheus exposition, reachable through the fixed
gateway route `llm-metrics` (grant `llm-metrics` to a key on the **API Keys**
page):

```bash
curl -H "apikey: $UNIBRIDGE_API_KEY" \
  "https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/llm/metrics"
```

This is the raw exposition format — counters and histograms accumulated since
the LiteLLM process started, meant for an external Prometheus to scrape rather
than for reading by hand. It carries the per-model latency detail that the
aggregate views don't expose, including
`litellm_llm_api_time_to_first_token_metric` and
`litellm_deployment_latency_per_output_token`.

True inter-token latency is exposed here too, as
`litellm_inter_token_latency_seconds` — a per-model histogram recorded by this
stack's custom callback (`litellm/custom_callbacks.py`), streamed requests only.
LiteLLM itself publishes no real ITL: its
`litellm_deployment_latency_per_output_token` divides the whole request duration
by the output token count, so it folds TTFT into every sample and reads high for
short answers. This one measures only the interval after the first token.

Samples are token gaps, not requests: a call streaming N tokens contributes N−1
samples at that request's mean gap, so percentiles are token-weighted the way a
decode-latency SLO means them, `rate(..._sum[5m]) / rate(..._count[5m])` is the
true mean ITL, and `rate(..._count[5m])` is streamed output-token throughput.
Buckets are SGLang's `sglang:inter_token_latency_seconds` list verbatim, so
`histogram_quantile()` here is directly comparable side-by-side with the same
query against an SGLang backend. Per-chunk timestamps aren't available at the
callback, so within-request variance is smoothed to the mean — aggregates are
exact, a single request's spread is not.

The grant is deliberately separate from `llm-proxy`: `/api/llm/metrics` is
carved out of the `/api/llm/*` catch-all by route priority, so a scraper key can
read metrics without being able to invoke any model. Note the difference from
`/api/prometheus/*` above — that one runs PromQL over history already stored by
UniBridge's Prometheus (which scrapes LiteLLM internally anyway); this one is
LiteLLM's live process state, for a scraper of your own.

### Grafana dashboards

Grafana is served same-origin behind the UI/edge nginx at
`https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/grafana` so it shares the stack's TLS —
no plaintext HTTP origin (`GRAFANA_PORT`, default 3300, stays loopback-only for
debugging; set `GRAFANA_EXTERNAL_URL` to relocate the UI links if you front it
differently). It signs in with UniBridge accounts via
Keycloak SSO ("UniBridge SSO" on the login page), restricted to admins: only
users with the `admin` realm role can sign in (as Grafana Admins) — everyone
else is rejected at login (`GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_STRICT`),
because Grafana has no notion of the UI's per-key scoping
(`gateway.monitoring.self`) and any sign-in would expose every metric. The
in-app Grafana links are likewise rendered for admins only. To open read-only
access to every UniBridge account instead, append `|| 'Viewer'` to
`GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH` and drop the strict flag. Grafana
user records are created automatically on first
SSO login; the local `admin` / `GRAFANA_ADMIN_PASSWORD` login remains as a
fallback, and self-service (non-SSO) sign-up stays disabled. On a fresh install
the realm import creates the `grafana` OAuth client from
`GRAFANA_OAUTH_CLIENT_SECRET`; on a realm imported before this client existed,
the Keycloak entrypoint creates it automatically at the next boot (confidential
client, PKCE S256, realm-roles mapper), so restarting Keycloak once is all it
takes. The entrypoint also re-syncs the client secret and redirect URI from env
on every boot, which likewise converges hand-created clients (whose
console-generated secret would otherwise fail token exchange with
`invalid_client`) onto the `.env` values.
Grafana ships with provisioned dashboards that mirror the UniBridge monitoring UI —
Overview, Gateway, LLM, External APIs, and Servers — running the same PromQL
against the same Prometheus, so both show identical numbers. Dashboards are
code: JSON under [`grafana/dashboards/`](./grafana/dashboards/), datasource and
loader config under `grafana/provisioning/` (mounted read-only; UI edits are
lost on restart — persist changes by exporting back into the JSON files).
Dashboards are pinned to `Asia/Seoul`, which both displays KST and aligns
Prometheus query steps to KST — hourly/daily buckets match the UI's KST calendar
buckets exactly. Known deltas vs the in-app UI: weekly buckets align to
Thursday-start weeks (epoch-aligned) instead of the UI's Monday-start, the
Dashboard page's live per-database connection grid has no Prometheus equivalent,
"over time" panels plot every series (no top-12 + "(others)" grouping) and
their `auto` step follows Grafana's interval rather than the UI's fixed
per-range windows, and the Servers disk panels ignore the
`NODE_EXPORTER_DISK_MOUNTPOINTS` whitelist (they always show every real
filesystem).

The Gateway/LLM/Overview dashboards mirror the UI's calendar-bucket views
(hour/day/week) through a `Bucket` variable (auto/1h/1d/1w) wired into the
per-interval volume panels, and the in-app monitoring pages carry their bucket
selection into the Grafana deep link as `var-bucket`. Daily buckets align to
KST midnight (dashboards are pinned to Asia/Seoul); weekly buckets keep the
Thursday-start caveat above.

Routes appear by **name**, not id: unibridge-service sets the prometheus
plugin's `prefer_name` on the APISIX `prometheus` global rule at every boot
(APISIX only reads this flag from the plugin conf — a
`plugin_attr.prometheus` entry in `apisix/config.yaml` is silently ignored),
so the `route` label on `apisix_http_*` metrics carries the route's friendly
name (falling back to the id for unnamed routes). The backend therefore
requires a name when saving a route and rejects names already used by another
route's name or id (duplicate labels would merge their series); routes created
before this rule keep working but pick up the requirement on their next edit.
System routes are named identically to their ids (`query-api`,
`llm-proxy`, …), so fixed-id PromQL filters keep matching. The global rule is
re-applied on every unibridge-service boot (no APISIX restart involved, and it
runs even with `APISIX_PROVISION_ON_START=false`), and series recorded before
the switch (or before a route rename) keep their old label value — the in-app
monitoring bridges id↔name when filtering and merges id/name rows in the
usages and top-routes views, but Grafana panels (and any external PromQL
pinned to a route id) show the old and new label as separate series across
that boundary.
Avoid giving two routes the same name: their metrics would merge into one
series. API-key (`consumer`) labels are unaffected — admin-created keys already
carry their human-readable key name; personal self-service keys show as
`self_<id>`.

### LiteLLM admin UI SSO

The LiteLLM admin UI (`https://<HOST_IP>:<LITELLM_PORT>/ui`) signs in with
UniBridge accounts via Keycloak SSO, admins only — mirroring the Grafana setup.
Opening `/ui` redirects straight to Keycloak (`AUTO_REDIRECT_UI_LOGIN_TO_SSO`),
so an admin holding a live UniBridge session lands signed in without touching a
login form; unset that env var to get the manual login page (and its master-key
fallback) back, e.g. while debugging a broken Keycloak. The wiring: the realm
`admin` role is a composite granting the `litellm` client role `proxy_admin`, a
client-role mapper surfaces it as a single-valued `role` claim, and LiteLLM
adopts that claim as its own `proxy_admin` role; everyone else resolves to
`internal_user_viewer` and is rejected at the SSO callback by
`ui_access_mode: "admin_only"` (`litellm/config.yaml`). The in-app "LiteLLM
Admin" button is likewise rendered for admins only.

Caveats worth knowing:

- **`GENERIC_CLIENT_USE_PKCE=true` is load-bearing** (see the compose comment):
  Keycloak pins the token issuer to the browser-facing host, so the userinfo
  endpoint always rejects tokens presented over the in-network listener; the
  PKCE code path is what makes LiteLLM fall back to id_token claims instead of
  500ing on that userinfo reply.
- **LiteLLM SSO is free for up to 5 rows in its internal user table**;
  more needs an enterprise license. Rejected non-admin sign-ins still insert an
  `internal_user_viewer` row before the admin check runs, so prune strays from
  the LiteLLM UI (Internal Users) or via `POST /user/delete` if the table
  creeps toward the cap.
- Accounts need a syntactically valid email — reserved TLDs like `.local` fail
  LiteLLM's validation at the callback with a 500.
- On a fresh install the realm import creates everything — the `litellm`
  OAuth client (from `LITELLM_OAUTH_CLIENT_SECRET`, redirect
  `https://<HOST_IP>:<LITELLM_PORT>/sso/callback`, PKCE S256), its
  `proxy_admin` client role, the composite on the realm `admin` role, and the
  user-client-role mapper emitting a single-valued `role` claim. On a realm
  imported before this client existed, the Keycloak entrypoint creates the
  missing pieces automatically at the next boot. The composite link needs
  realm-management rights beyond the `apihub-service` account, so that one
  step escalates to the bootstrap admin (`KC_ADMIN_USER`/`KC_ADMIN_PASSWORD`)
  and, if that account has been removed, logs a warning — add the composite in
  the Keycloak console then (realm role `admin` → Associated roles →
  `litellm` `proxy_admin`). The entrypoint also re-syncs the client's secret
  and redirect URI from env on every boot, same as Grafana's client.

### LLM conversation capture

Every **successful** LLM call through LiteLLM is appended as one JSON line to a
fine-tuning dataset by [`litellm/custom_callbacks.py`](./litellm/custom_callbacks.py).
This is deliberate data collection, so know what it keeps and where:

- **What is stored**: the full request messages and the raw response, verbatim,
  plus token counts, cost, model, and per-user attribution (the
  `x-litellm-end-user-id` APISIX forwards — every call authenticates as the
  master key, so this is the only identity available). Prompts and completions
  are stored in the clear. Failed calls are not captured.
- **Where**: the `litellm-dataset` Docker volume (container path
  `LITELLM_DATASET_DIR`, default `/var/lib/litellm-dataset`), as
  `dataset-YYYYMMDD.jsonl` files rotated daily by UTC date. The same volume name
  is shared by the single-stack and blue-green layouts, so a capture started
  under one carries over to the other. `scripts/build_finetune_dataset.py` turns
  these into a training-ready dataset offline.
- **Not a backup target**: these files are regenerable training data and can
  grow large, so backups intentionally skip them (see
  [`backup/README.md`](./backup/README.md)). Retention is therefore their **only**
  size guard.
- **Retention** (opportunistic, enforced from the capture path itself — no cron):
  - `LITELLM_DATASET_RETENTION_DAYS` — delete files older than N days
    (`0` = keep forever, the default).
  - `LITELLM_DATASET_MAX_TOTAL_BYTES` — cap the combined size of all dataset
    files, deleting oldest-first past the cap (`0` = no cap, the default). The
    current day's file is never deleted, so the cap can be briefly exceeded by at
    most one day of capture.

  A full sweep runs at most once per process per day (at the UTC rollover); with
  a byte cap set, an extra size sweep may run every few minutes so a busy day
  cannot outrun the cap. Both default to `0`, preserving the original unbounded
  behaviour for anyone relying on full history. Cleanup never blocks or fails a
  request; deletions are logged to the LiteLLM container log.
- **To disable capture entirely**: remove the
  `callbacks: custom_callbacks.proxy_handler_instance` line from
  [`litellm/config.yaml`](./litellm/config.yaml) and restart LiteLLM
  (`docker compose restart litellm`, or restart it in the infra project on a
  blue-green host). Existing files remain on the volume until removed manually.

### Server (host) monitoring

Register Linux servers running `node_exporter` to monitor reachability, disk
(with a `predict_linear` disk-fill forecast), CPU, and memory, with proactive
alerts routed through the existing 담당자/관리자 alert pipeline. Install the agent
with [`scripts/install_node_exporter.sh`](./scripts/install_node_exporter.sh),
add the host in the UI under **Servers**, and tune thresholds globally (Alert
settings) or per host. Disk checks can also be limited to selected node_exporter
mountpoints globally with `NODE_EXPORTER_DISK_MOUNTPOINTS`, or per host in the
Servers UI; the server detail disk chart splits those selected mountpoints into
separate lines. GPU hosts can optionally run NVIDIA `dcgm-exporter` as well
([`deploy/dcgm-exporter/`](./deploy/dcgm-exporter/docker-compose.yml)) for GPU
down/utilisation/memory alerts and per-GPU charts. Full guide:
[`docs/server-monitoring.md`](./docs/server-monitoring.md).

### Infra alerting (Prometheus → Alertmanager)

Registered resources (DBs, hosts, routes, S3/NAS) are watched by the in-app
alert checker. The *platform's own* components are watched by Prometheus rules in
[`prometheus/rules/unibridge-alerts.yml`](./prometheus/rules/unibridge-alerts.yml)
instead — APISIX 5xx rate, unibridge-service reachability, the metadata DB, the
Keycloak/LiteLLM DB TCP probes, missing audit writes, and (blue-green only)
"no color reports itself active". Those go to Alertmanager, which POSTs them to
`/_api/internal/alertmanager`; the app turns each one into mail for the global
관리자 plus an entry in alert history (rule type `prometheus_alert`).

**Set `ALERTMANAGER_WEBHOOK_TOKEN` or this path sends nothing.** It is empty by
default, and the receiver then rejects every delivery with 503: the alerts still
appear in the Alertmanager UI, but no mail goes out. The app logs the reason once
per process when the first delivery is rejected. Use the same value on both
sides — one `.env` variable feeds the app and the rendered Alertmanager config.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> .env
```

For `UniBridgeServiceDown` and `UniBridgeNoActiveInstance` the receiver is the
service that is down, so the mail only lands once the app is back. Setting
`ALERTMANAGER_SMTP_HOST` + `ALERTMANAGER_SMTP_TO` (see `.env.example` for the
full set) adds a direct-SMTP copy of just those two alerts that does not involve
the app at all. It is off by default, and when off the fallback route and
receiver are stripped from the rendered config entirely.

## Backups

Stateful components (etcd, Keycloak DB, LiteLLM DB, unibridge-service meta DB) are backed up by scripts in [`backup/`](./backup/README.md). Snapshots land in `./snapshots/` (gitignored) with 14-day retention, and manifest SHA256s are verified before any destructive restore.

### Deploy-time setup

1. **Pull the latest tree** on the deploy host:
   ```bash
   git pull
   ```
2. **Install host-side prerequisites** (needed by `restore.sh` for manifest verification):
   ```bash
   apt-get install -y jq        # or python3 — either one works
   ```
3. **Schedule the nightly backup** via cron (`crontab -e`):
   ```
   0 3 * * * cd /opt/unibridge && ./backup/backup.sh >> /var/log/unibridge-backup.log 2>&1
   ```
4. **Run a restore drill before relying on it.** Follow the "Full-disaster recovery order" in [`backup/README.md`](./backup/README.md) against a disposable environment, end-to-end at least once. A backup you haven't tested restoring is a wish, not a backup.

Retention, path overrides, and the full restore runbook live in [`backup/README.md`](./backup/README.md).

## Timezone Migration (one-time, after upgrading to the UTC-aware timestamp fix)

The timezone-consistency fix changes the on-disk format of every `DateTime` column in the meta DB. Pre-fix rows were stored at second precision without an offset; post-fix rows use microsecond precision. SQLite compares TEXT columns lexicographically, so the older shorter strings get excluded from boundary `>=` filters until they are normalized.

Run the following sequence on the deploy host **once** when upgrading past this change:

```bash
git pull
docker compose up -d --build unibridge-service unibridge-ui
docker compose exec unibridge-service python -m scripts.backfill_utc_timestamps
```

The third command introspects every `UtcDateTime` column, skips tables that don't yet exist in the DB, and rewrites legacy values to the canonical microsecond form. It is idempotent — re-running it finds 0 rows to update.

> **SQLite-only.** The lexicographic-compare bug fixed by this script is specific to SQLite. PostgreSQL / MSSQL deployments store datetimes as native timestamp types and do not need this step; the script will hard-stop with `RuntimeError` if run against a non-SQLite backend.

## etcd Authentication Migration Guide

etcd는 APISIX의 설정 저장소로, 기본적으로 인증이 활성화되어 있습니다. 기존 환경에서 업그레이드하는 경우 아래 절차를 따라주세요.

### 신규 설치

`.env`에 `ETCD_ROOT_PASSWORD`만 설정하면 자동으로 적용됩니다.

```bash
# .env
ETCD_ROOT_PASSWORD=your-strong-password-here
```

### 기존 환경에서 마이그레이션

기존에 인증 없이 운영하던 etcd 볼륨이 있는 경우, 두 가지 방법 중 택일합니다.

**방법 1: 볼륨 초기화 (권장, 설정 데이터 재생성)**

```bash
# 1. 서비스 중지
docker compose down

# 2. etcd 볼륨 삭제 (APISIX 라우트/업스트림 설정이 초기화됩니다)
docker volume rm unibridge_etcd-data

# 3. .env에 패스워드 설정
#    ETCD_ROOT_PASSWORD=your-strong-password-here

# 4. 재시작 (APISIX 라우트는 unibridge-service 기동 시 자동 재프로비저닝)
docker compose up -d
```

> APISIX 라우트(query-api, llm-proxy, llm-admin)와 업스트림은 `unibridge-service` 시작 시 자동으로 재생성됩니다. 수동으로 추가한 커스텀 라우트/업스트림만 다시 등록하면 됩니다.

**방법 2: 인증 없이 유지 (개발/테스트 전용)**

```bash
# .env
ETCD_ALLOW_NONE_AUTH=yes
# ETCD_ROOT_PASSWORD는 비워두거나 생략
```

## Key Features

- **Multi-DB support** — PostgreSQL, MSSQL, ClickHouse via a single query endpoint
- **LLM Proxy** — Unified LiteLLM gateway with centralized auth, usage metrics, and per-model analytics
- **S3 Connections** — Register S3-compatible storage and browse objects through the gateway
- **Alerts** — Rule-based alerting with webhook delivery and history
- **APISIX Gateway** — Route management, upstream config, API key auth
- **RBAC** — 22 granular permissions, dynamic role management
- **API Keys** — External access with per-database/route restrictions
- **Monitoring** — Prometheus metrics, request trends, latency percentiles
- **User Management** — Keycloak integration, role assignment
- **Audit Logging** — Full query history with filters
- **i18n** — Korean / English
