# unibridge-ui (React 19 + TS + Vite)

See the repo-root `CLAUDE.md` for cross-service context. Served by nginx in prod
(`nginx.conf`); `entrypoint.sh` injects runtime config. Commands: `npm run dev|lint|test|build`.

## Layout (`src/`)
- `main.tsx` / `App.tsx`     — entry + router (react-router 7).
- `keycloak.ts`             — keycloak-js init (OIDC login/token).
- `i18n.ts` + `locales/`    — i18next; keep both locale files in sync when adding strings.
- `runtime-config.d.ts`     — types for runtime config injected at container start (not build time).
- `api/`                    — axios clients + TanStack Query hooks.
- `pages/` (incl. `alerts/`), `components/`, `styles/`, `utils/`, `test/`.

## Gotchas
- **Dev API proxy**: `vite.config.ts` proxies `/_api` → `http://localhost:8000`, so run
  unibridge-service locally on :8000 for `npm run dev`. The proxy strips `X-Consumer-*`
  headers (parity with `nginx.conf`) to prevent identity spoofing — don't remove that.
- **Runtime config**: Keycloak settings and the LiteLLM admin URL come from
  runtime-injected config, with Vite env only as a local-dev fallback. API base
  is the same-origin `/_api` path. Build output is static; the container fills
  config at start.
- `build` runs `tsc -b` first — a type error fails the build (and CI). Manual chunking for
  recharts/keycloak/tanstack/i18n/react is configured in `vite.config.ts`.
- Tests: vitest + Testing Library + jsdom, in `src/test/*.test.tsx`.
- `src/content/api-metrics-convention.md` is a committed mirror of the canonical
  `docs/api-metrics-convention.md` (repo root is outside this package's docker build
  context, so it can't be imported directly). `MetricsGuide.test.tsx` fails when they
  diverge — edit both together (`cp docs/api-metrics-convention.md src/content/`).
