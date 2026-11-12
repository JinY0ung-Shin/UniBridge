# unibridge-ui (React 19 + TS + Vite)

See the repo-root `CLAUDE.md` for cross-service context. Served by nginx in prod
(`nginx.conf`); `entrypoint.sh` injects runtime config.

## Layout (`src/`)
- `main.tsx` / `Root.tsx` / `App.tsx` — entry, app-or-relay switch, router (react-router 7).
  `/upload-relay` (`pages/UploadRelay.tsx`, the popup the Noah app opens) renders OUTSIDE
  `AuthProvider`/`Layout`: it is a popup that signs in on its own (same rule as the app:
  an account without an app role gets its pending-approval state, never the file picker), uses
  plain `fetch`, and
  must move its `#t=` ticket to sessionStorage before `keycloak.init`. Noah reuses the popup, so
  a new `#t=` arrives as a `hashchange` (store it, reload); the expiry countdown uses the
  server's `expiresInSeconds`, never the PC clock.
- `keycloak.ts`             — keycloak-js init (OIDC login/token).
- `i18n.ts` + `locales/`    — i18next; keep both locale files in sync when adding strings.
- `runtime-config.d.ts`     — types for runtime config injected at container start (not build time).
- `api/`                    — axios clients + TanStack Query hooks.
- `pages/` (incl. `alerts/`), `components/`, `styles/`, `utils/`, `test/`.

## Gotchas
- **Dev API proxy**: `vite.config.ts` proxies `/_api` → `http://localhost:8000`, so run
  unibridge-service locally on :8000 for `npm run dev`. The proxy strips `X-Consumer-*`
  headers (parity with `nginx.conf`) to prevent identity spoofing — don't remove that.
- **Runtime config**: Keycloak settings and the Bifrost admin URL/host name come from
  runtime-injected config, with Vite env only as a local-dev fallback. API base
  is the same-origin `/_api` path. Build output is static; the container fills
  config at start.
- **Bifrost host name**: `nginx-bifrost-ui.conf` is a second server block that
  `entrypoint.sh` renders into conf.d only when `BIFROST_UI_HOSTNAME` is set (a validated
  DNS name); `nginx.conf`'s server stays `default_server` for every other host name.
  It presents `tls-bifrost.crt` (the name's own pair, else compose mounts the UniBridge one;
  `entrypoint.sh` switches it to `tls.crt` when nginx cannot load the pair, so a bad path
  or half of two pairs cannot take the whole UI down).
  Its `/_unibridge/sso` goes to unibridge-service: the Bifrost button's sign-in handoff.
- `build` runs `tsc -b` first — a type error fails the build (and the release gate). Manual chunking for
  recharts/keycloak/tanstack/i18n/react is configured in `vite.config.ts`.
- Tests: vitest + Testing Library + jsdom, in `src/test/*.test.ts(x)`.
- `src/content/api-metrics-convention.md` is a committed mirror of the canonical
  `docs/api-metrics-convention.md` (repo root is outside this package's docker build
  context, so it can't be imported directly). `MetricsGuide.test.tsx` fails when they
  diverge — edit both together (`cp docs/api-metrics-convention.md src/content/`).
