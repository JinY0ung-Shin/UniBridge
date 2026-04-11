# API Hub (UniBridge)

Internal API/DB gateway platform. Register multiple databases (PostgreSQL, MSSQL), execute SQL through a single endpoint, manage API routes via APISIX, and control access with RBAC + API keys.

## Architecture

```
Browser ──HTTPS──> query-ui (nginx)
                       │
                       ├── /_api/* ──> query-service (FastAPI)
                       └── /api/*  ──> apisix (API Gateway)
                                          │
                                          ├── Registered databases
                                          └── Upstream services

Keycloak ── OIDC auth
Prometheus ── APISIX metrics
```

**Services (7):** etcd, APISIX, Keycloak + Postgres, query-service, Prometheus, query-ui

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

**Must change:**

| Variable | Description |
|----------|-------------|
| `HOST_IP` | Server IP or hostname that browsers access (not `localhost` in production) |
| `ENCRYPTION_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `JWT_SECRET` | Same command as above, different value |
| `APISIX_ADMIN_KEY` | Random string for APISIX admin API |
| `KC_ADMIN_PASSWORD` | Keycloak admin console password |
| `KC_DB_PASSWORD` | Keycloak database password |
| `KEYCLOAK_SERVICE_CLIENT_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

**Optional:**

| Variable | Default | Description |
|----------|---------|-------------|
| `QUERY_UI_PORT` | 3000 | HTTPS port for the web UI |
| `KEYCLOAK_PORT` | 8443 | Keycloak OIDC port |
| `ENABLE_DEV_TOKEN_ENDPOINT` | false | Set `true` only for local dev |
| `SSL_VERIFY` | true | Set `false` if using self-signed certs |
| `RATE_LIMIT_PER_MINUTE` | 60 | Per-user query rate limit |
| `MAX_CONCURRENT_QUERIES` | 5 | Per-user concurrent query limit |

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

### 5. Access

| Service | URL |
|---------|-----|
| Web UI | `https://<HOST_IP>:<QUERY_UI_PORT>` |
| Keycloak Admin | `https://<HOST_IP>:<KEYCLOAK_PORT>/admin` |
| API Gateway | `https://<HOST_IP>:<QUERY_UI_PORT>/api/*` |
| Prometheus | `http://<HOST_IP>:9090` (localhost only) |

Default login: Keycloak admin console (`KC_ADMIN_USER` / `KC_ADMIN_PASSWORD`), then create a user in the `apihub` realm.

## Service Ports (default)

| Port | Service | Binding |
|------|---------|---------|
| 3000 | query-ui (HTTPS) | public |
| 8443 | Keycloak (HTTPS) | public |
| 8000 | query-service | localhost only |
| 9180 | APISIX admin | localhost only |
| 9090 | Prometheus | localhost only |

## Local Development (without Docker)

### Backend

```bash
cd query-service
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov

# Minimal .env for dev
export META_DB_URL="sqlite+aiosqlite:///data/meta.db"
export ENCRYPTION_KEY="dev-key-change-in-prod-32chars!"
export JWT_SECRET="dev-jwt-secret"
export ENABLE_DEV_TOKEN_ENDPOINT=true

uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd query-ui
npm install
npm run dev   # http://localhost:5173, proxies /_api to :8000
```

### Tests

```bash
# Backend
cd query-service && pytest tests/ -v

# Frontend
cd query-ui && npx vitest run
```

## Common Operations

```bash
# Restart all
docker compose restart

# Rebuild after code change
docker compose up -d --build

# View logs
docker compose logs -f query-service
docker compose logs -f keycloak

# Stop
docker compose down

# Stop and remove volumes (DESTROYS DATA)
docker compose down -v
```

## Key Features

- **Multi-DB support** — PostgreSQL, MSSQL via a single query endpoint
- **APISIX Gateway** — Route management, upstream config, API key auth
- **RBAC** — 17 granular permissions, dynamic role management
- **API Keys** — External access with per-database/route restrictions
- **Monitoring** — Prometheus metrics, request trends, latency percentiles
- **User Management** — Keycloak integration, role assignment
- **Audit Logging** — Full query history with filters
- **i18n** — Korean / English
