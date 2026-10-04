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
| `S3_LIST_ALL_MAX_KEYS` | 100000 | Max entries one `objects?all=true` response returns; the rest resumes via `next_continuation_token` (~550 B of service memory per entry while building the response) |
| `S3_LIST_ALL_TIME_BUDGET_SECONDS` | 20 | `all=true` stops paging after this long (time spent queueing for a slot included) and returns a resumable page. Budget plus one in-flight page (≤ `S3_OP_TIMEOUT_SECONDS`) must stay under the 60s APISIX / UI-nginx proxy timeouts |
| `S3_LIST_ALL_MAX_CONCURRENT` | 2 | Process-wide cap on concurrent `all=true` listings. Extra requests queue in FIFO order within the time budget and get `429` + `Retry-After` only if no slot frees up in time |
| `S3_LIST_ALL_TOKENLESS_MAX_KEYS` | 30000 | Size of the one larger page `all=true` requests when a backend truncates a listing without a continuation token (helps where the backend honours `MaxKeys` > 1000; ≤ 1000 disables). That page is parsed in one go: ~1.6 KB of memory per object, ~0.5 KB per folder |
| `ALERTMANAGER_WEBHOOK_TOKEN` | empty | Bearer token Alertmanager uses to POST fired Prometheus rules into the app's alert pipeline. **Empty means infra alerts send no mail** — the receiver answers 503. See [Infra alerting](#infra-alerting-prometheus--alertmanager) |
| `ALERTMANAGER_SMTP_HOST`, `ALERTMANAGER_SMTP_TO` | empty | Set both to add a direct-SMTP mail path for the service-down alerts, so that mail does not depend on the app being up. Empty = off. Companions: `ALERTMANAGER_SMTP_PORT` (25), `_FROM`, `_USERNAME`, `_PASSWORD`, `_REQUIRE_TLS` (true) |

### 3. TLS certificates

Place cert files in `certs/`:

```bash
# Self-signed (dev/test only). The SAN and CA:FALSE are required: strict TLS
# clients such as Codex CLI (rustls) ignore the CN and reject leaf certificates
# that carry CA:TRUE, which is what `openssl req -x509` emits by default.
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout certs/tls.key -out certs/tls.crt \
  -subj "/CN=${HOST_IP}" \
  -addext "subjectAltName=IP:${HOST_IP}" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth"
```

Or copy your real certificates:

```bash
cp /path/to/your/cert.crt certs/tls.crt
cp /path/to/your/cert.key certs/tls.key
```

#### Client trust for self-signed certificates

- If the gateway is reached by a DNS name, add it to the SAN:
  `-addext "subjectAltName=IP:${HOST_IP},DNS:gateway.example.internal"`.
- CLI clients on other machines must trust the certificate explicitly. Codex CLI
  honours `SSL_CERT_FILE`, so run `export SSL_CERT_FILE=/path/to/tls.crt` in the
  shell that starts Codex — it has no option to skip verification.
- With a private CA instead, put the full chain (leaf first, then CA) in
  `certs/tls.crt` so every service that mounts `TLS_CERT_PATH` sees the chain,
  and point the clients' `SSL_CERT_FILE` at the CA certificate.
- Browsers also require the SAN once the certificate is trusted; the `CA:FALSE`
  constraint is what strict non-browser clients such as Codex additionally enforce.

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
model_supports_reasoning_summaries = true   # required for Codex to send reasoning.effort for unknown model ids
model_reasoning_effort = "medium"           # low | medium | high; forwarded as reasoning_effort
show_raw_agent_reasoning = true             # optional: llm-converter streams raw reasoning text, not summaries

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
- Use a certificate Codex trusts: it needs a SAN and `CA:FALSE` (see [TLS certificates](#3-tls-certificates)), and for self-signed certificates export `SSL_CERT_FILE=/path/to/tls.crt` (or the CA certificate) in the shell that runs Codex. Codex has no option to skip TLS verification.
- Codex sends `reasoning.effort` only for models it has metadata for. For LiteLLM model ids Codex does not know, set `model_supports_reasoning_summaries = true` together with `model_reasoning_effort` in `config.toml`; `llm-converter` maps the effort to Chat Completions `reasoning_effort` and attaches `allowed_openai_params` so LiteLLM forwards it to the backend instead of dropping it. Because the value now reaches the backend verbatim, effort levels outside its vocabulary (`xhigh`, `max`) are clamped to the nearest accepted level — vLLM/SGLang reject anything but `low`/`medium`/`high` — configurable via `CONVERTER_REASONING_EFFORT_LEVELS`.
- Do not name the provider `OpenAI` or `azure` (or use an Azure-style base URL): Codex then switches to remote compaction items the converter does not implement. `name = "UniBridge"` keeps compaction local.
- With the default `CONVERTER_LENGTH_AS_COMPLETED=auto`, a `finish_reason=length` truncation is reported to Codex as `response.completed` instead of the spec-correct `response.incomplete`: Codex reads `incomplete` as a failed stream and re-sends the whole turn up to `stream_max_retries` (5) times, so one truncated generation costs six. Other clients keep the spec behaviour.
- Codex's `multi_agent_v1` sub-agent tools (spawn/send_input/wait/close/resume) and `image_gen` arrive as a Responses `namespace` tool, a shape Chat Completions cannot carry; `llm-converter` flattens them to top-level functions on the request and re-stamps the namespace on the returned tool calls so Codex can route them back to its client-side tools — so sub-agents and image generation work through the gateway. Toggle `CONVERTER_FLATTEN_NAMESPACE_TOOLS=false` to disable.
- Codex's `stream_idle_timeout_ms` (default 300000) counts SSE events, not bytes; the converter's `: ping` heartbeat does not reset it, so raise it for backends with long time-to-first-token.
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

### S3 object listing

S3 returns at most 1000 entries per `ListObjectsV2` call, so `GET /api/s3/{alias}/objects` is paged: `max_keys` (1–1000, default 200) sets the page size, and while `is_truncated` is `true` the next page comes from passing `next_continuation_token` back as `continuation_token`. With `all=true` the service follows the tokens itself and returns the whole listing in one response (`max_keys` has no effect then, though it is still validated as 1–1000):

```http
GET /api/s3/my-s3/objects?bucket=data&prefix=reports/&max_keys=1000
GET /api/s3/my-s3/objects?bucket=data&prefix=reports/&continuation_token=<next_continuation_token>
GET /api/s3/my-s3/objects?bucket=data&prefix=reports/&all=true
GET /api/s3/my-s3/objects?bucket=data&prefix=reports/&delimiter=&all=true
```

`delimiter=` (empty) lists every key under the prefix recursively instead of rolling sub-folders up into `folders`. One `all=true` response is bounded by `S3_LIST_ALL_MAX_KEYS` entries and `S3_LIST_ALL_TIME_BUDGET_SECONDS`; a page failing after the first also ends it early. In every case the response is still a valid page, so a client that wants everything calls again with `continuation_token` together with `all=true` for as long as `is_truncated` is `true` **and** `next_continuation_token` is set. A truncated page without a token (a storage backend that cannot resume, or whose token stopped advancing) cannot be continued.

Some S3-compatible backends report a truncated listing with no continuation token (and no `NextMarker`) yet honour a `MaxKeys` above S3's 1000. For those, `all=true` asks once more for that page with up to `S3_LIST_ALL_TOKENLESS_MAX_KEYS` entries, which returns the whole folder in one go when it fits. Page mode (`max_keys` ≤ 1000) cannot get past the first page there, so use `all=true`; the S3 browser's **Load All** restarts the folder with it. If even the larger page comes back truncated, the response stays truncated without a token. Retrying helps when the larger request was skipped or failed (time budget, timeout); otherwise only a larger `S3_LIST_ALL_TOKENLESS_MAX_KEYS` gets further, and that page is still capped by `S3_LIST_ALL_MAX_KEYS` and costs memory as listed above.

At most `S3_LIST_ALL_MAX_CONCURRENT` full listings run at once. Further requests queue in FIFO order, the wait counting against the same time budget, and get `429` with `Retry-After` only if no slot frees up in time. A client that disconnects cancels its listing (or leaves the queue) right away, so an abandoned request does not hold a slot. The S3 browser's **Load All** button uses the same call and aborts it when you leave the folder.

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

### API key expiry at the gateway

Self-service keys carry a 30-day TTL (`expires_at`); admin-created keys default
to no expiry. The app rejects an expired key with `401 API key expired`, but the
LLM routes (`llm-proxy`, `llm-messages`, `llm-responses`, `llm-models`) go APISIX
→ llm-converter → LiteLLM without ever reaching the app, so expiry is enforced at
the gateway instead: every `key-auth` route's `consumer-restriction` whitelist is
reconciled against the database at boot and then every 5 minutes on the active
blue/green color, and an expired key is dropped from all of them. It then gets
`403` from APISIX on every gateway route. The APISIX consumer itself is kept —
it holds the only copy of the key value — so **Renew** (same key value) and
**Regenerate** (new key value) both restore gateway access immediately rather
than waiting for the next reconcile pass. Consumers that the database does not
know about are never touched, and a key whose stored grants fail to parse keeps
its current whitelist membership instead of being revoked over a storage bug.

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
Overview, Gateway, LLM, DB Queries, External APIs, and Servers — running the same PromQL
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
per-range windows, the rate-based trend panels (request rate, latency
percentiles) use `$__rate_interval` — about 1m at short ranges — where the UI
keeps a 5-minute minimum window, so short-range curves look less smoothed, and
the Servers disk panels ignore the
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

### LiteLLM request timeout and retries

`litellm/config.yaml` caps a single upstream call at `request_timeout: 600`,
matching the 600s read timeout the APISIX LLM routes already enforce — LiteLLM
waiting longer than the gateway it sits behind only holds a worker open. The
timeout is the provider's httpx timeout, so it bounds a connect or an idle read
against a wedged vLLM/SGLang backend without cutting a stream that keeps
emitting chunks. `num_retries: 2` still covers connection-level failures, but
`router_settings.retry_policy.TimeoutErrorRetries: 0` stops a request that
already timed out from being re-sent, which would otherwise put three copies of
the same work on a backend that is already saturated. The file is bind-mounted,
so restart LiteLLM to pick up a change.

### Upgrading LiteLLM

The image tag is pinned in both `docker-compose.infra.yml` and
`docker-compose.yml`; change it in both. LiteLLM runs its own Prisma migrations
against `litellm-db` on boot, and it is a single infra instance, so an upgrade
is a short LLM outage that blue/green does not cover:

```bash
./backup/backup.sh                     # litellm-db.sql.gz is part of the snapshot
docker compose -p unibridge-infra -f docker-compose.infra.yml pull litellm
docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --wait litellm
```

To roll back, put the previous tag back and run the same `up -d --wait`. In the
1.83 → 1.102 rehearsal, 1.102 applied 53 migrations and was healthy in about
17s. 1.83 then booted on the migrated database with nothing pending and served
traffic, so a rollback only needed the tag. Keep the backup anyway, since a
downgraded proxy can't use data written to the newer columns.

Notes for the 1.83 → 1.102 jump:

- 1.102 requires auth on `/metrics` by default; 1.83 didn't. The `litellm`
  Prometheus scrape job sends no credentials, so `litellm/config.yaml` sets
  `require_auth_for_metrics_endpoint: false`, and `litellm/tests` fails if that
  line is dropped.
- Requests made with the master key (every APISIX-routed call) are recorded
  under the alias `litellm_proxy_master_key` instead of a key hash, both in
  spend logs and in the `hashed_api_key` metric label. Per-key attribution uses
  the `end_user` label, which is unchanged.
- 1.83.14 never emitted `litellm_input_cached_tokens_metric`, so the
  cached-token cards and Grafana panels stayed empty. 1.102 emits it, and they
  start filling after the upgrade.

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

### Bifrost side-by-side test (`/api/llm-bi`)

An opt-in way to evaluate [Bifrost](https://github.com/maximhq/bifrost) as a
LiteLLM replacement on real clients without touching `/api/llm`, which keeps
running on LiteLLM exactly as before:

```
/api/llm/*                         → unchanged (LiteLLM / llm-converter)
/api/llm-bi/v1/messages          ┐
/api/llm-bi/v1/responses         ├→ llm-converter-bi → Bifrost /v1/chat/completions
/api/llm-bi/v1/models            ┘
/api/llm-bi/v1/chat/completions  ┐
/api/llm-bi/v1/completions       ├→ Bifrost /v1/…
/api/llm-bi/v1/embeddings        ┘
```

`llm-converter-bi` is the same converter image with only its upstream changed,
so Claude Code and Codex get the same translation on both paths. Only those six
exact paths are routed. Nothing else of Bifrost is reachable through the
gateway: not its UI at `/` (also served as a 200 fallback for unknown paths),
not its management API under `/api/*`, not MCP under `/v1/mcp/*`, not
`/metrics`. Off the gateway, Bifrost answers inference only with the virtual
key APISIX injects — just as LiteLLM answers only the master key APISIX
injects.

1. **Secrets** — set these in `.env`; the container refuses to start without
   them ([`bifrost/entrypoint.sh`](./bifrost/entrypoint.sh)):
   - `BIFROST_ENCRYPTION_KEY` (≥ 16 chars) encrypts provider keys. It cannot be
     changed once data exists.
   - `BIFROST_ADMIN_PASSWORD` is the single admin login, enforced from the first
     boot ([`bifrost/config.json`](./bifrost/config.json)).
   - `BIFROST_TEST_VK` is the gateway virtual key:
     `python3 -c "import secrets; print('sk-bf-' + secrets.token_urlsafe(32))"`.
     It must start with `sk-bf-`, because Bifrost silently replaces a value
     without the prefix. `config.json` declares it with `allow_all_providers`,
     so providers added later need no change, and `enforce_auth_on_inference`
     makes it mandatory. To rotate it, change the value, recreate the container
     with the start command in step 2 (`docker compose restart` keeps the old
     environment, so every request would get a 401), then re-run
     `scripts/bifrost-test.sh up`.
2. **Start** — the services sit behind the `bifrost-test` compose profile, so a
   plain `up` and the blue-green deploy never start or health-check them. **Do
   not put `COMPOSE_PROFILES` in `.env`**: every deploy would then wait on
   Bifrost.

   On a host where the June 2026 Bifrost cutover ran, check
   `docker ps -a --filter name=bifrost` before the first start. Remove any
   leftover `bifrost-tls` nginx or old `bifrost` container with `docker rm -f`.
   The June `bifrost-tls` published `${BIFROST_PORT:-${LITELLM_PORT}}:443` →
   `bifrost:8080`, so it would front the new container. Compose's "Found orphan
   containers" warning on the infra project is the signal.

   ```bash
   # blue-green host (shared infra project)
   docker compose -p unibridge-infra -f docker-compose.infra.yml --profile bifrost-test \
     up -d --build --wait bifrost llm-converter-bi
   # single stack
   docker compose --profile bifrost-test up -d --build --wait bifrost llm-converter-bi
   ```

   Booting needs no internet: `config.json` loads the (intentionally empty)
   pricing and model-parameter datasheets in `bifrost/` over `file://`, since
   self-hosted models have no list price. On an air-gapped host, first import
   `maximhq/bifrost:v2.2.4` with `docker save`/`docker load`.
   `llm-converter-bi` builds from `./llm-converter` with the same mirror
   settings as `llm-converter`.
3. **Register providers** — in the Bifrost UI over an SSH tunnel
   (`ssh -L 18080:127.0.0.1:18080 <host>`, then `http://localhost:18080`; the
   port is bound to loopback only, `BIFROST_TEST_ADMIN_PORT`) or through the
   management API. Register one OpenAI-compatible custom provider per
   vLLM/SGLang server. For a fair comparison with LiteLLM, match its settings
   ([LiteLLM request timeout and retries](#litellm-request-timeout-and-retries)).
   `curl -u admin` prompts for the password, which keeps it out of the process
   list:

   ```bash
   curl -u admin http://127.0.0.1:18080/api/providers \
     -H 'Content-Type: application/json' -d '{
       "provider": "vllm-qwen",
       "network_config": {
         "base_url": "http://10.0.0.11:8000",
         "allow_private_network": true,
         "default_request_timeout_in_seconds": 600,
         "stream_idle_timeout_in_seconds": 600,
         "max_retries": 2, "retry_backoff_initial": "500ms", "retry_backoff_max": "5s"
       },
       "custom_provider_config": {
         "base_provider_type": "openai",
         "allowed_requests": {
           "chat_completion": true, "chat_completion_stream": true, "list_models": true,
           "text_completion": true, "text_completion_stream": true, "embedding": true}
       }}'
   curl -u admin http://127.0.0.1:18080/api/providers/vllm-qwen/keys \
     -H 'Content-Type: application/json' -d '{
       "name": "default", "value": "<the server'\''s --api-key, or any placeholder>", "weight": 1,
       "models": ["qwen3.5-32b"], "aliases": {"qwen3.5-32b": "Qwen/Qwen3.5-32B"}}'
   ```

   - `allow_private_network` defaults to false, which blocks every RFC 1918
     address, Docker's 172.x included.
   - `base_url` has no `/v1`; Bifrost appends `/v1/chat/completions`.
   - Bifrost's defaults are a 300s request timeout, a 120s stream-idle cut and no
     retries. With retries on, it retries connection errors and upstream
     5xx/429 but never its own request timeout — the same effect as LiteLLM's
     `TimeoutErrorRetries: 0`.
   - List the LiteLLM model name in `models` and map it in `aliases` to the id
     the server serves. Clients can then use the **same model names on both
     paths**. `/v1/models` lists them as `provider/name`, and both forms work.
4. **Routes** — once both containers are healthy, run `scripts/bifrost-test.sh up`.
   It installs the `bifrost` and `llm-converter-bi` upstreams and four key-auth
   routes, all deny-all by default: `llm-bi-proxy`, `llm-bi-messages`,
   `llm-bi-responses` and `llm-bi-models`. Each route injects `BIFROST_TEST_VK`
   as `x-bf-vk`.
   - **Master keys** (`*`) are whitelisted on all four automatically by the
     consumer-restriction reconciler.
   - **Every other test key** needs all four granted on the **API Keys** page.
     An `llm-proxy` grant does not imply them.
   - `up` is idempotent and keeps those grants; `status` shows what is installed.
   - Don't edit these routes in the Gateway UI: its strip-prefix toggle rewrites
     their path regex. Change the script and re-run `up` instead.
   - The alert checker probes every APISIX upstream. From here on, a crashed
     Bifrost or converter mails `upstream_health` alerts to the admins.
5. **Try it** — the live E2E suite runs unchanged against the new prefix:

   ```bash
   cd e2e
   LLM_BASE_URL=https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/llm-bi \
     LLM_API_KEY=<test key> LLM_MODEL=qwen3.5-32b pytest -q
   ```

   Claude Code takes `ANTHROPIC_BASE_URL=https://<HOST_IP>:<UNIBRIDGE_UI_PORT>/api/llm-bi`,
   and Codex takes `base_url = ".../api/llm-bi/v1"`, as in
   [Codex through UniBridge](#codex-through-unibridge).
6. **Tear down** — order matters: run `down` first, then stop the containers.
   `scripts/bifrost-test.sh down` removes exactly those four routes and two
   upstreams. Only then stop the services
   (`docker compose … --profile bifrost-test rm -sf bifrost llm-converter-bi`).
   Stopping them while the upstreams are still installed mails
   `upstream_health` alerts. If the data is no longer wanted, also remove the
   `unibridge_bifrost-test-data` and `unibridge_llm-converter-bi-state` volumes.
   If etcd still holds the orphaned `bifrost` upstream from the June 2026
   cutover, `up` replaces it (and says so) and `down` deletes it.

Differences and limits to keep in mind while comparing:

- **Outside UniBridge's LLM views.** `/api/llm-bi` traffic does not appear on
  the LLM monitoring or usage pages, and is not written to
  [LLM conversation capture](#llm-conversation-capture); those read LiteLLM's
  metrics and callbacks. The gateway metrics for the `llm-bi-*` routes do
  appear, under their route names.
- **Bifrost's own observability.** Bifrost serves Prometheus metrics at
  `bifrost:8080/metrics`. They are unauthenticated inside the Docker network and
  not scraped by default. APISIX stamps every request with
  `x-bf-dim-consumer` and `x-bf-lh-consumer` set to `$consumer_name`, so the
  `consumer` metrics label and the log metadata attribute traffic per API key.
  Request logs, bodies included, are kept in `logs.db` on the `bifrost-test`
  volume for 14 days. Set `client.disable_content_logging: true` in
  `config.json` to keep bodies out.
- **Client headers.** APISIX strips `Authorization`, `x-api-key`, `api-key` and
  `x-goog-api-key`, which Bifrost reads as virtual-key selectors. It also strips
  `x-bf-api-key` and `x-bf-api-key-id`, which pin a stored provider key. It
  overwrites `x-bf-vk` and the consumer headers. Key-auth runs before that
  rewrite, so it still reads the caller's own `apikey`.
- **Request rewriting.** Bifrost forwards `max_tokens` as
  `max_completion_tokens`, and was seen raising values below 16 to 16. Check
  that your backends honour `max_completion_tokens`.
- **Extra parameters.** Non-OpenAI fields such as `chat_template_kwargs` pass
  through on `llm-bi-proxy` only (`x-bf-passthrough-extra-params`). On the
  converter routes Bifrost drops the LiteLLM-only `allowed_openai_params`, and
  the converter has already clamped `reasoning_effort`.
- **Admin access.** Bifrost OSS has a single admin login and no SSO. Treat that
  login as gateway-admin level: switching `enforce_auth_on_inference` off in the
  Bifrost UI sticks across restarts until `config.json` itself changes.
- **`allowed_requests` is an allowlist.** A provider registered with it serves
  only the operations set to `true`, so leave out `text_completion*` or
  `embedding` and `/api/llm-bi/v1/completions` or `/v1/embeddings` is refused
  for that provider.

### DB query monitoring

**Query Monitoring** (data section of the sidebar) shows query count, error rate
(errors + timeouts), timeouts, execution time and rows returned per API key and
per database, over the same ranges and calendar buckets as the gateway page.
The two tables cross-filter: pick a key (or click its row) and the database
table narrows to that key, and vice versa. The page reads UniBridge's own
`unibridge_query_duration_seconds` / `unibridge_query_rows_returned` histograms,
whose `consumer` label is the API key's consumer name, or `__ui__` for queries
run from the UI (JWT callers, shown as "(UI / direct)"). Series recorded before
the label existed appear as "(before per-key tracking)" until Prometheus
retention (60d) drops them, so per-key history starts at the upgrade while
per-database totals keep their full history. Execution time is measured in-app
(connection acquire → result fetched) and counts successful queries only.
Requests rejected before execution (permission 403, unknown database 404, rate
limit 429) are not counted; API-key traffic through the gateway still shows
them per key under the `query-api` route on the gateway page. Access follows
gateway monitoring: `gateway.monitoring.read` sees every key and the UI row,
`gateway.monitoring.self` only the caller's own key. The Grafana mirror is the
"DB Query Monitoring" dashboard (`unibridge-queries`).

Writing PromQL over `unibridge_*` metrics yourself: they are scraped by two
jobs. `unibridge-service-colors` scrapes each blue/green color separately;
`unibridge-service` hits a DNS alias both colors answer on, so on a blue-green
host its counters alternate between two processes and `increase()`/`rate()`
inflate by orders of magnitude. The backend and the dashboard read the colors
job whenever any of its targets is up and fall back to `unibridge-service` only
on single-stack hosts (`_gated` in
[`unibridge-service/app/routers/query_metrics.py`](./unibridge-service/app/routers/query_metrics.py));
never sum the two jobs.

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

## etcd 인증 및 이미지 마이그레이션 가이드

etcd는 APISIX의 설정 저장소입니다. 라우트·업스트림·컨슈머 전체가 여기에만 저장되므로 기본적으로
인증이 활성화되어 있습니다.

이미지는 업스트림 `quay.io/coreos/etcd:v3.5.33`을 사용합니다. 이전에 쓰던 `bitnamilegacy/etcd:3.5.11`은
Bitnami가 2025년 8월 무료 이미지를 아카이브로 옮기면서 동결된 태그라, 보안 업데이트가 더 이상
제공되지 않습니다.

업스트림 이미지는 distroless(셸·`rm`·`curl` 없음)이고 Bitnami의 `ALLOW_NONE_AUTHENTICATION` /
`ETCD_ROOT_PASSWORD` 부트스트랩이 없습니다. 그 역할은 일회성 서비스 `etcd-init`이 대신합니다
(`etcd/init-auth.sh`, 이미지 `unibridge-etcd-tools:3.5.33`, `etcd/Dockerfile`에서 빌드). 이 서비스는
멱등이라 `up`할 때마다 다시 실행되고, APISIX는 `service_completed_successfully`로 부트스트랩 완료를
기다린 뒤에 기동합니다. 같은 이미지를 `backup/lib/etcd.sh`의 스냅샷 저장·복구에도 사용합니다.

> 일회성 서비스에 대한 `--wait` 지원은 **Docker Compose 2.20 이상**이 필요합니다.
> `docker compose version`으로 확인하세요.

프로젝트 이름은 배치에 따라 다릅니다.

| 배치 | 명령 접두사 |
|---|---|
| 블루-그린(프로덕션) | `docker compose -p unibridge-infra -f docker-compose.infra.yml` |
| 단일 스택(dev) | `docker compose` |

아래 명령은 블루-그린 기준입니다. 단일 스택이면 접두사만 바꿔서 실행하세요.

### 신규 설치

`.env`에 `ETCD_ROOT_PASSWORD`만 설정하면 자동으로 적용됩니다.

```bash
# .env
ETCD_ROOT_PASSWORD=your-strong-password-here
```

### Bitnami 이미지 → 업스트림 이미지 전환 (기존 호스트)

데이터는 **그대로 두고 in-place로 전환**합니다. `ETCD_DATA_DIR=/bitnami/etcd/data`와 `etcd-data` 볼륨의
마운트 경로 `/bitnami/etcd`를 의도적으로 유지하기 때문에, 3.5 마이너가 같은 업스트림 etcd가 기존 데이터
디렉터리를 그대로 읽습니다. export/import나 라우트 재프로비저닝이 필요 없습니다.

**1. 먼저 백업**

```bash
./backup/backup.sh
```

**2. 이미지 준비** (etcd가 내려가기 전에 미리 받아두면 중단 시간이 줄어듭니다)

```bash
docker compose -p unibridge-infra -f docker-compose.infra.yml build etcd-init
docker compose -p unibridge-infra -f docker-compose.infra.yml pull etcd
```

**3. 전환** (같은 볼륨, 같은 자리)

```bash
docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --wait etcd etcd-init apisix
```

etcd 컨테이너는 이미지가 바뀌었으므로 재생성되지만, APISIX 컨테이너는 `depends_on`만 바뀌었으므로
**재생성되지 않습니다**. APISIX는 그동안 캐시된 라우팅 설정으로 계속 프록시합니다(데이터 플레인 9080은
무중단).

> **APISIX Admin API는 약 1분간 503입니다.** etcd의 인증 토큰은 메모리에만 있어서 재기동하면
> 무효화되고, APISIX가 캐시된 토큰을 쓰는 동안 `{"error_msg":"etcdserver: invalid auth token"}`을
> 반환합니다. 토큰이 갱신되면 저절로 복구됩니다. 이 구간에 `unibridge-service`를 재기동하면 부팅 시
> 라우트 프로비저닝이 실패하므로, 앱 배포는 Admin API가 200으로 돌아온 뒤에 진행하세요.

**4. 검증**

```bash
# etcd: 자격증명으로 health (인증이 켜져 있으면 무자격 호출은 실패하는 게 정상)
docker compose -p unibridge-infra -f docker-compose.infra.yml exec etcd etcdctl endpoint health
docker compose -p unibridge-infra -f docker-compose.infra.yml exec etcd etcdctl endpoint status -w table

# etcd-init: 멱등 재실행 로그 ("already enabled")
docker compose -p unibridge-infra -f docker-compose.infra.yml logs etcd-init

# APISIX: 전환 전과 라우트 개수가 같은지 (503이면 1분 기다렸다 재시도)
curl -s -H "X-API-KEY: $APISIX_ADMIN_KEY" http://127.0.0.1:9180/apisix/admin/routes | jq '.total'
```

**5. 롤백**

compose 변경을 되돌린 뒤, Bitnami 이미지는 uid 1001로 실행되므로 업스트림 etcd가 root로 쓴 파일의
소유권을 되돌려야 합니다. 전환은 이 한 가지 이유로 **단방향**입니다.

```bash
git checkout -- docker-compose.yml docker-compose.infra.yml
docker compose -p unibridge-infra -f docker-compose.infra.yml stop etcd
docker run --rm -v unibridge_etcd-data:/bitnami/etcd --entrypoint chown \
  unibridge-etcd-tools:3.5.33 -R 1001:0 /bitnami/etcd
docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --wait etcd apisix
```

볼륨 이름은 `.env`의 `ETCD_DATA_VOLUME`(기본 `unibridge_etcd-data`)을 따릅니다. 데이터가 손상된
최후의 경우에는 1단계 백업으로 복구합니다.

```bash
./backup/restore.sh etcd ./snapshots/<stamp>
```

### 인증 없이 운영 (개발/테스트 전용)

```bash
# .env
ETCD_ALLOW_NONE_AUTH=yes
# ETCD_ROOT_PASSWORD는 비워두거나 생략
```

`ETCD_ROOT_PASSWORD`가 비어 있는데 `ETCD_ALLOW_NONE_AUTH`가 `no`이면 `etcd-init`이 종료 코드 1로
중단하고 APISIX도 기동하지 않습니다. 인증 없는 etcd로 실수로 뜨는 것을 막기 위한 의도된 동작입니다.

### 인증 없는 기존 볼륨을 초기화하고 다시 시작 (선택)

기존에 인증 없이 운영하던 볼륨에 인증을 켜려면 `etcd-init`이 자동으로 root 사용자를 만들고 인증을
활성화하므로 보통 아무것도 할 필요가 없습니다. 상태를 완전히 비우고 싶을 때만 아래를 사용하세요.

```bash
docker compose -p unibridge-infra -f docker-compose.infra.yml down
docker volume rm unibridge_etcd-data
# .env에 ETCD_ROOT_PASSWORD 설정 후
docker compose -p unibridge-infra -f docker-compose.infra.yml up -d --wait
```

> APISIX 라우트와 업스트림은 `unibridge-service` 시작 시 자동으로 재생성됩니다. 수동으로 추가한
> 커스텀 라우트/업스트림만 다시 등록하면 됩니다.

## Key Features

- **Multi-DB support** — PostgreSQL, MSSQL, ClickHouse via a single query endpoint
- **LLM Proxy** — Unified LiteLLM gateway with centralized auth, usage metrics, and per-model analytics
- **S3 Connections** — Register S3-compatible storage and browse objects through the gateway
- **Alerts** — Rule-based alerting with webhook delivery and history
- **APISIX Gateway** — Route management, upstream config, API key auth
- **RBAC** — 22 granular permissions, dynamic role management
- **API Keys** — External access with per-database/route restrictions
- **Monitoring** — Prometheus metrics, request trends, latency percentiles, per-API-key × per-database query stats
- **User Management** — Keycloak integration, role assignment
- **Audit Logging** — Full query history with filters
- **i18n** — Korean / English
