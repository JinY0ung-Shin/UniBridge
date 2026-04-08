# Gateway Management Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add APISIX Gateway Routes + Upstreams management with service key injection to the API Hub admin UI and backend.

**Architecture:** query-service (Python FastAPI) proxies APISIX Admin API calls, injecting X-API-KEY server-side. Frontend adds 3 new pages (GatewayRoutes list, GatewayRouteForm, GatewayUpstreams) using the existing Vercel Dark design system.

**Tech Stack:** Python FastAPI + httpx, React 19 + React Router + React Query, vanilla CSS with CSS custom properties

---

## File Structure

### Backend (new files)

```
query-service/
  app/
    config.py                     # MODIFY: add APISIX_ADMIN_URL, APISIX_ADMIN_KEY
    main.py                       # MODIFY: include gateway router
    routers/
      gateway.py                  # CREATE: /admin/gateway/* endpoints
    services/
      apisix_client.py            # CREATE: APISIX Admin API HTTP client
  requirements.txt                # MODIFY: add httpx
```

### Frontend (new files)

```
query-ui/src/
  api/
    client.ts                     # MODIFY: add gateway types + API functions
  components/
    Layout.tsx                    # MODIFY: add Gateway nav section
    Layout.css                    # MODIFY: add nav-divider style
  pages/
    GatewayRoutes.tsx             # CREATE: route list page
    GatewayRoutes.css             # CREATE: route list styles
    GatewayRouteForm.tsx          # CREATE: route create/edit form page
    GatewayRouteForm.css          # CREATE: route form styles
    GatewayUpstreams.tsx          # CREATE: upstream list + modal page
    GatewayUpstreams.css          # CREATE: upstream styles
  App.tsx                         # MODIFY: add new routes
```

### Config

```
docker-compose.yml                # MODIFY: add APISIX env vars to query-service
```

---

### Task 1: Add APISIX config and httpx dependency to backend

**Files:**
- Modify: `query-service/app/config.py`
- Modify: `query-service/requirements.txt`

- [ ] **Step 1: Add APISIX settings to config.py**

In `query-service/app/config.py`, add two fields to the `Settings` class:

```python
APISIX_ADMIN_URL: str = "http://apisix:9180"
APISIX_ADMIN_KEY: str = ""
```

Add after the `ENABLE_DEV_TOKEN_ENDPOINT` line.

- [ ] **Step 2: Add httpx to requirements.txt**

Append to `query-service/requirements.txt`:

```
httpx==0.28.1
```

- [ ] **Step 3: Add env vars to docker-compose.yml**

In `docker-compose.yml`, add to query-service environment section:

```yaml
- APISIX_ADMIN_URL=http://apisix:9180
- APISIX_ADMIN_KEY=edd1c9f034335f136f87ad84b625c8f1
```

- [ ] **Step 4: Commit**

```bash
git add query-service/app/config.py query-service/requirements.txt docker-compose.yml
git commit -m "feat(backend): add APISIX admin config and httpx dependency"
```

---

### Task 2: Create APISIX HTTP client service

**Files:**
- Create: `query-service/app/services/apisix_client.py`

- [ ] **Step 1: Create apisix_client.py**

```python
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

APISIX_TIMEOUT = 10.0


def _headers() -> dict[str, str]:
    return {"X-API-KEY": settings.APISIX_ADMIN_KEY}


def _base_url() -> str:
    return settings.APISIX_ADMIN_URL.rstrip("/")


async def list_resources(resource: str) -> dict[str, Any]:
    """List APISIX resources (routes, upstreams, etc.).

    Returns {"items": [...], "total": N} with flattened values.
    """
    url = f"{_base_url()}/apisix/admin/{resource}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    raw_list = data.get("list") or []
    items = [entry["value"] for entry in raw_list if "value" in entry]
    return {"items": items, "total": data.get("total", len(items))}


async def get_resource(resource: str, resource_id: str) -> dict[str, Any]:
    """Get a single APISIX resource by ID."""
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    return data.get("value", data)


async def put_resource(resource: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Create or update an APISIX resource via PUT."""
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.put(url, json=body, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    return data.get("value", data)


async def delete_resource(resource: str, resource_id: str) -> None:
    """Delete an APISIX resource."""
    url = f"{_base_url()}/apisix/admin/{resource}/{resource_id}"
    async with httpx.AsyncClient(timeout=APISIX_TIMEOUT) as client:
        resp = await client.delete(url, headers=_headers())
        resp.raise_for_status()
```

- [ ] **Step 2: Commit**

```bash
git add query-service/app/services/apisix_client.py
git commit -m "feat(backend): add APISIX Admin API HTTP client"
```

---

### Task 3: Create gateway router with routes + upstreams endpoints

**Files:**
- Create: `query-service/app/routers/gateway.py`
- Modify: `query-service/app/main.py`

- [ ] **Step 1: Create gateway.py router**

```python
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from httpx import HTTPStatusError

from app.auth import CurrentUser, require_admin
from app.services import apisix_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/gateway", tags=["Gateway"])

MASK_KEEP = 4  # characters to keep when masking service key values


def _mask_value(value: str) -> str:
    if len(value) <= MASK_KEEP:
        return "***"
    return value[:MASK_KEEP] + "***"


def _extract_service_key(route: dict[str, Any]) -> dict[str, str] | None:
    """Extract service key info from proxy-rewrite plugin config."""
    plugins = route.get("plugins", {})
    pr = plugins.get("proxy-rewrite", {})
    headers_set = pr.get("headers", {}).get("set", {})
    if not headers_set:
        return None
    # Return the first header as service key (masked)
    for name, value in headers_set.items():
        return {"header_name": name, "header_value": _mask_value(value)}
    return None


def _inject_service_key(body: dict[str, Any], existing_plugins: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convert service_key field to proxy-rewrite plugin config, preserving other plugins."""
    service_key = body.pop("service_key", None)
    plugins = dict(existing_plugins or {})

    if service_key and service_key.get("header_name") and service_key.get("header_value"):
        plugins["proxy-rewrite"] = {
            "headers": {
                "set": {
                    service_key["header_name"]: service_key["header_value"]
                }
            }
        }
    elif service_key is None and "proxy-rewrite" in plugins:
        # Preserve existing proxy-rewrite if no new service_key provided
        pass

    if plugins:
        body["plugins"] = plugins
    return body


def _handle_apisix_error(exc: HTTPStatusError, resource: str) -> None:
    """Convert APISIX HTTP errors to FastAPI HTTP exceptions."""
    detail = f"APISIX error: {exc.response.text}"
    try:
        err_data = exc.response.json()
        detail = err_data.get("error_msg", detail)
    except Exception:
        pass

    if exc.response.status_code == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{resource} not found")
    if exc.response.status_code in (400, 409):
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("/routes")
async def list_routes(
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """List all gateway routes."""
    try:
        result = await apisix_client.list_resources("routes")
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Routes")
    # Add masked service_key to each route
    for item in result.get("items", []):
        item["service_key"] = _extract_service_key(item)
    return result


@router.get("/routes/{route_id}")
async def get_route(
    route_id: str,
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Get a single gateway route."""
    try:
        route = await apisix_client.get_resource("routes", route_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    route["service_key"] = _extract_service_key(route)
    return route


@router.put("/routes/{route_id}")
async def save_route(
    route_id: str,
    body: dict[str, Any],
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Create or update a gateway route. Preserves existing plugins."""
    # Fetch existing route to preserve plugins
    existing_plugins: dict[str, Any] | None = None
    try:
        existing = await apisix_client.get_resource("routes", route_id)
        existing_plugins = existing.get("plugins")
    except HTTPStatusError:
        pass  # New route, no existing plugins

    body = _inject_service_key(body, existing_plugins)

    try:
        result = await apisix_client.put_resource("routes", route_id, body)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")
    result["service_key"] = _extract_service_key(result)
    return result


@router.delete("/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_route(
    route_id: str,
    _admin: CurrentUser = Depends(require_admin),
) -> None:
    """Delete a gateway route."""
    try:
        await apisix_client.delete_resource("routes", route_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Route")


# ── Upstreams ───────────────────────────────────────────────────────────────


@router.get("/upstreams")
async def list_upstreams(
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """List all gateway upstreams."""
    try:
        return await apisix_client.list_resources("upstreams")
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstreams")


@router.get("/upstreams/{upstream_id}")
async def get_upstream(
    upstream_id: str,
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Get a single upstream."""
    try:
        return await apisix_client.get_resource("upstreams", upstream_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")


@router.put("/upstreams/{upstream_id}")
async def save_upstream(
    upstream_id: str,
    body: dict[str, Any],
    _admin: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Create or update an upstream."""
    try:
        return await apisix_client.put_resource("upstreams", upstream_id, body)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")


@router.delete("/upstreams/{upstream_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_upstream(
    upstream_id: str,
    _admin: CurrentUser = Depends(require_admin),
) -> None:
    """Delete an upstream. Fails if referenced by a route."""
    try:
        await apisix_client.delete_resource("upstreams", upstream_id)
    except HTTPStatusError as exc:
        _handle_apisix_error(exc, "Upstream")
```

- [ ] **Step 2: Register gateway router in main.py**

In `query-service/app/main.py`, add the import and include:

After `from app.routers import admin, query` add:
```python
from app.routers import admin, gateway, query
```

After `app.include_router(admin.router)` add:
```python
app.include_router(gateway.router)
```

- [ ] **Step 3: Verify backend starts**

Run: `cd /home/jinyoung/apihub && docker compose build query-service && docker compose up -d query-service`

Expected: query-service starts without errors.

- [ ] **Step 4: Commit**

```bash
git add query-service/app/routers/gateway.py query-service/app/main.py
git commit -m "feat(backend): add gateway routes and upstreams proxy endpoints"
```

---

### Task 4: Add gateway types and API functions to frontend client

**Files:**
- Modify: `query-ui/src/api/client.ts`

- [ ] **Step 1: Add gateway types and functions to client.ts**

Append to the end of `query-ui/src/api/client.ts` (before the `export default client;` line):

```typescript
/* ── Gateway Types ── */

export interface GatewayServiceKey {
  header_name: string;
  header_value: string;
}

export interface GatewayRoute {
  id: string;
  name?: string;
  uri: string;
  methods?: string[];
  upstream_id?: string;
  status: number;
  service_key?: GatewayServiceKey | null;
  plugins?: Record<string, unknown>;
}

export interface GatewayUpstreamNode {
  host: string;
  port: number;
  weight: number;
}

export interface GatewayUpstream {
  id: string;
  name?: string;
  type: string;
  nodes: Record<string, number>;
}

export interface GatewayListResponse<T> {
  items: T[];
  total: number;
}

/* ── Gateway: Routes ── */

export async function getGatewayRoutes(): Promise<GatewayListResponse<GatewayRoute>> {
  const { data } = await client.get('/admin/gateway/routes');
  return data;
}

export async function getGatewayRoute(id: string): Promise<GatewayRoute> {
  const { data } = await client.get(`/admin/gateway/routes/${id}`);
  return data;
}

export async function saveGatewayRoute(id: string, route: Record<string, unknown>): Promise<GatewayRoute> {
  const { data } = await client.put(`/admin/gateway/routes/${id}`, route);
  return data;
}

export async function deleteGatewayRoute(id: string): Promise<void> {
  await client.delete(`/admin/gateway/routes/${id}`);
}

/* ── Gateway: Upstreams ── */

export async function getGatewayUpstreams(): Promise<GatewayListResponse<GatewayUpstream>> {
  const { data } = await client.get('/admin/gateway/upstreams');
  return data;
}

export async function getGatewayUpstream(id: string): Promise<GatewayUpstream> {
  const { data } = await client.get(`/admin/gateway/upstreams/${id}`);
  return data;
}

export async function saveGatewayUpstream(id: string, upstream: Record<string, unknown>): Promise<GatewayUpstream> {
  const { data } = await client.put(`/admin/gateway/upstreams/${id}`, upstream);
  return data;
}

export async function deleteGatewayUpstream(id: string): Promise<void> {
  await client.delete(`/admin/gateway/upstreams/${id}`);
}
```

- [ ] **Step 2: Verify build**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b`

Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add query-ui/src/api/client.ts
git commit -m "feat(ui): add gateway API types and client functions"
```

---

### Task 5: Update Layout — add Gateway nav section with divider

**Files:**
- Modify: `query-ui/src/components/Layout.tsx`
- Modify: `query-ui/src/components/Layout.css`

- [ ] **Step 1: Add nav-divider style to Layout.css**

Append before `/* ── Login overlay ── */` in `query-ui/src/components/Layout.css`:

```css
.nav-divider {
  height: 1px;
  background: var(--border-default);
  margin: 8px 12px;
}
```

- [ ] **Step 2: Update Layout.tsx nav items and sidebar**

In `query-ui/src/components/Layout.tsx`, replace the `navItems` array (lines 6-12) with:

```tsx
const navItems = [
  { to: '/', label: 'Dashboard', section: 'data' },
  { to: '/connections', label: 'Connections', section: 'data' },
  { to: '/permissions', label: 'Permissions', section: 'data' },
  { to: '/audit-logs', label: 'Audit Logs', section: 'data' },
  { to: '/query', label: 'Query Playground', section: 'data' },
  { to: '/gateway/routes', label: 'Gateway Routes', section: 'gateway' },
  { to: '/gateway/upstreams', label: 'Gateway Upstreams', section: 'gateway' },
];
```

Then in the sidebar nav section, replace the `{navItems.map(...)}` block (lines 97-143 approximately) with:

```tsx
{navItems.map((item, index) => {
  const prevItem = navItems[index - 1];
  const showDivider = prevItem && prevItem.section !== item.section;
  return (
    <span key={item.to}>
      {showDivider && <div className="nav-divider" />}
      <NavLink
        to={item.to}
        end={item.to === '/'}
        className={({ isActive }) =>
          `nav-link ${isActive ? 'nav-link--active' : ''}`
        }
      >
        <span className="nav-icon">
          {item.label === 'Dashboard' && (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <rect x="1" y="1" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
              <rect x="10" y="1" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
              <rect x="1" y="10" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
              <rect x="10" y="10" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          )}
          {item.label === 'Connections' && (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <circle cx="5" cy="5" r="3" stroke="currentColor" strokeWidth="1.5" />
              <circle cx="13" cy="13" r="3" stroke="currentColor" strokeWidth="1.5" />
              <path d="M7.5 7.5l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          )}
          {item.label === 'Permissions' && (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <rect x="3" y="8" width="12" height="8" rx="2" stroke="currentColor" strokeWidth="1.5" />
              <path d="M6 8V5a3 3 0 016 0v3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          )}
          {item.label === 'Audit Logs' && (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <path d="M5 1h8l4 4v12H1V1h4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" fill="none" />
              <path d="M5 7h8M5 10h8M5 13h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          )}
          {item.label === 'Query Playground' && (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <path d="M2 4l5 4-5 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M9 14h7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          )}
          {item.label === 'Gateway Routes' && (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <path d="M1 9h16M9 1v16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              <circle cx="9" cy="9" r="3" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          )}
          {item.label === 'Gateway Upstreams' && (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <rect x="1" y="3" width="16" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
              <rect x="1" y="11" width="16" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
              <circle cx="4" cy="5" r="1" fill="currentColor" />
              <circle cx="4" cy="13" r="1" fill="currentColor" />
            </svg>
          )}
        </span>
        {item.label}
      </NavLink>
    </span>
  );
})}
```

- [ ] **Step 3: Verify build**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/components/Layout.tsx query-ui/src/components/Layout.css
git commit -m "feat(ui): add Gateway section to sidebar navigation"
```

---

### Task 6: Create GatewayRoutes list page

**Files:**
- Create: `query-ui/src/pages/GatewayRoutes.tsx`
- Create: `query-ui/src/pages/GatewayRoutes.css`

- [ ] **Step 1: Create GatewayRoutes.css**

```css
.gateway-routes {
  max-width: 1000px;
}

.gateway-routes .page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
}

.method-badges {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.method-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
}

.method-badge--get {
  background: rgba(0, 112, 243, 0.1);
  color: var(--accent-blue);
}

.method-badge--post {
  background: rgba(80, 227, 194, 0.1);
  color: var(--accent-green);
}

.method-badge--put {
  background: rgba(245, 166, 35, 0.1);
  color: var(--accent-yellow);
}

.method-badge--delete {
  background: rgba(243, 18, 96, 0.1);
  color: var(--accent-red);
}

.method-badge--patch {
  background: rgba(161, 161, 161, 0.1);
  color: var(--text-secondary);
}

.cell-uri {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
}

.cell-service-key {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
}
```

- [ ] **Step 2: Create GatewayRoutes.tsx**

```tsx
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { getGatewayRoutes, deleteGatewayRoute, type GatewayRoute } from '../api/client';
import './GatewayRoutes.css';

const METHOD_COLORS: Record<string, string> = {
  GET: 'get', POST: 'post', PUT: 'put', DELETE: 'delete', PATCH: 'patch',
};

function GatewayRoutes() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const routesQuery = useQuery({
    queryKey: ['gateway-routes'],
    queryFn: getGatewayRoutes,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteGatewayRoute(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-routes'] });
    },
  });

  const routes = routesQuery.data?.items ?? [];

  function handleDelete(route: GatewayRoute) {
    const name = route.name || route.uri;
    if (window.confirm(`Delete route "${name}"? This cannot be undone.`)) {
      deleteMutation.mutate(route.id);
    }
  }

  return (
    <div className="gateway-routes">
      <div className="page-header">
        <div>
          <h1>Gateway Routes</h1>
          <p className="page-subtitle">Manage API gateway routing rules</p>
        </div>
        <button className="btn btn-primary" onClick={() => navigate('/gateway/routes/new')}>
          + Add Route
        </button>
      </div>

      {routesQuery.isLoading && <div className="loading-message">Loading routes...</div>}

      {routesQuery.isError && (
        <div className="error-banner">Failed to load gateway routes.</div>
      )}

      {routes.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>URI</th>
                <th>Methods</th>
                <th>Upstream</th>
                <th>Service Key</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {routes.map((route) => (
                <tr key={route.id}>
                  <td className="cell-alias">{route.name || '—'}</td>
                  <td className="cell-uri">{route.uri}</td>
                  <td>
                    <div className="method-badges">
                      {(route.methods || ['ALL']).map((m) => (
                        <span key={m} className={`method-badge method-badge--${METHOD_COLORS[m] || 'patch'}`}>
                          {m}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td>{route.upstream_id || '—'}</td>
                  <td className="cell-service-key">
                    {route.service_key ? `${route.service_key.header_name}: ${route.service_key.header_value}` : '—'}
                  </td>
                  <td>
                    <span className={`badge ${route.status === 1 ? 'badge-ok' : 'badge-unknown'}`}>
                      {route.status === 1 ? 'Active' : 'Disabled'}
                    </span>
                  </td>
                  <td>
                    <div className="action-buttons">
                      <button
                        className="btn btn-sm btn-secondary"
                        onClick={() => navigate(`/gateway/routes/${route.id}/edit`)}
                      >
                        Edit
                      </button>
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => handleDelete(route)}
                        disabled={deleteMutation.isPending}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!routesQuery.isLoading && routes.length === 0 && !routesQuery.isError && (
        <div className="empty-state">
          <h3>No gateway routes</h3>
          <p>Click "Add Route" to create your first API route.</p>
        </div>
      )}
    </div>
  );
}

export default GatewayRoutes;
```

- [ ] **Step 3: Commit**

```bash
git add query-ui/src/pages/GatewayRoutes.tsx query-ui/src/pages/GatewayRoutes.css
git commit -m "feat(ui): add GatewayRoutes list page"
```

---

### Task 7: Create GatewayRouteForm page (create/edit)

**Files:**
- Create: `query-ui/src/pages/GatewayRouteForm.tsx`
- Create: `query-ui/src/pages/GatewayRouteForm.css`

- [ ] **Step 1: Create GatewayRouteForm.css**

```css
.route-form {
  max-width: 720px;
}

.route-form .page-header {
  margin-bottom: 28px;
}

.form-section {
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  padding: 20px 24px;
  margin-bottom: 16px;
}

.form-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  margin-bottom: 16px;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 14px;
}

.form-row--full {
  grid-template-columns: 1fr;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.field input,
.field select {
  padding: 8px 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  background: var(--bg-secondary);
  color: var(--text-primary);
  transition: border-color 0.15s;
}

.field input:focus,
.field select:focus {
  border-color: var(--text-tertiary);
}

.field input::placeholder {
  color: var(--text-tertiary);
}

.methods-group {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.method-check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
}

.method-check input {
  accent-color: var(--accent-blue);
  width: 16px;
  height: 16px;
  cursor: pointer;
}

.form-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  padding-top: 8px;
}
```

- [ ] **Step 2: Create GatewayRouteForm.tsx**

```tsx
import { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getGatewayRoute,
  saveGatewayRoute,
  getGatewayUpstreams,
} from '../api/client';
import './GatewayRouteForm.css';

const ALL_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'];

function GatewayRouteForm() {
  const { id } = useParams<{ id: string }>();
  const isEdit = !!id;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [name, setName] = useState('');
  const [uri, setUri] = useState('');
  const [methods, setMethods] = useState<string[]>(['GET', 'POST']);
  const [upstreamId, setUpstreamId] = useState('');
  const [statusVal, setStatusVal] = useState(1);
  const [keyHeader, setKeyHeader] = useState('');
  const [keyValue, setKeyValue] = useState('');
  const [error, setError] = useState('');

  const routeQuery = useQuery({
    queryKey: ['gateway-route', id],
    queryFn: () => getGatewayRoute(id!),
    enabled: isEdit,
  });

  const upstreamsQuery = useQuery({
    queryKey: ['gateway-upstreams'],
    queryFn: getGatewayUpstreams,
  });

  useEffect(() => {
    if (routeQuery.data) {
      const r = routeQuery.data;
      setName(r.name || '');
      setUri(r.uri || '');
      setMethods(r.methods || ['GET', 'POST']);
      setUpstreamId(r.upstream_id || '');
      setStatusVal(r.status ?? 1);
      if (r.service_key) {
        setKeyHeader(r.service_key.header_name || '');
        // Don't pre-fill masked value — leave empty to keep existing
      }
    }
  }, [routeQuery.data]);

  const saveMutation = useMutation({
    mutationFn: (data: { routeId: string; body: Record<string, unknown> }) =>
      saveGatewayRoute(data.routeId, data.body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-routes'] });
      navigate('/gateway/routes');
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? 'Failed to save route');
      } else {
        setError('Failed to save route');
      }
    },
  });

  const upstreams = upstreamsQuery.data?.items ?? [];

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!uri.trim()) return;

    const routeId = id || Date.now().toString();
    const body: Record<string, unknown> = {
      name: name.trim() || undefined,
      uri: uri.trim(),
      methods,
      upstream_id: upstreamId || undefined,
      status: statusVal,
    };

    if (keyHeader.trim() && keyValue.trim()) {
      body.service_key = {
        header_name: keyHeader.trim(),
        header_value: keyValue.trim(),
      };
    }

    setError('');
    saveMutation.mutate({ routeId, body });
  }

  function toggleMethod(method: string) {
    setMethods((prev) =>
      prev.includes(method) ? prev.filter((m) => m !== method) : [...prev, method]
    );
  }

  if (isEdit && routeQuery.isLoading) {
    return <div className="loading-message">Loading route...</div>;
  }

  return (
    <div className="route-form">
      <div className="page-header">
        <h1>{isEdit ? 'Edit Route' : 'New Route'}</h1>
        <p className="page-subtitle">{isEdit ? `Editing route ${id}` : 'Create a new API gateway route'}</p>
      </div>

      <form onSubmit={handleSubmit}>
        {/* Basic Info */}
        <div className="form-section">
          <div className="form-section-title">Basic Info</div>
          <div className="form-row">
            <div className="field">
              <label>Name</label>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="My API Route" />
            </div>
            <div className="field">
              <label>Status</label>
              <select value={statusVal} onChange={(e) => setStatusVal(Number(e.target.value))}>
                <option value={1}>Active</option>
                <option value={0}>Disabled</option>
              </select>
            </div>
          </div>
          <div className="form-row form-row--full">
            <div className="field">
              <label>URI</label>
              <input value={uri} onChange={(e) => setUri(e.target.value)} placeholder="/api/service/*" required />
            </div>
          </div>
          <div className="field">
            <label>Methods</label>
            <div className="methods-group">
              {ALL_METHODS.map((m) => (
                <label key={m} className="method-check">
                  <input type="checkbox" checked={methods.includes(m)} onChange={() => toggleMethod(m)} />
                  {m}
                </label>
              ))}
            </div>
          </div>
        </div>

        {/* Upstream */}
        <div className="form-section">
          <div className="form-section-title">Upstream</div>
          <div className="form-row form-row--full">
            <div className="field">
              <label>Upstream</label>
              <select value={upstreamId} onChange={(e) => setUpstreamId(e.target.value)}>
                <option value="">Select upstream...</option>
                {upstreams.map((u) => (
                  <option key={u.id} value={u.id}>{u.name || u.id}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {/* Service Key */}
        <div className="form-section">
          <div className="form-section-title">Service Key (Optional)</div>
          <div className="form-row">
            <div className="field">
              <label>Header Name</label>
              <input value={keyHeader} onChange={(e) => setKeyHeader(e.target.value)} placeholder="Authorization" />
            </div>
            <div className="field">
              <label>Header Value</label>
              <input
                type="password"
                value={keyValue}
                onChange={(e) => setKeyValue(e.target.value)}
                placeholder={isEdit ? 'Leave empty to keep current' : 'Bearer sk-xxx...'}
              />
            </div>
          </div>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <div className="form-actions">
          <button type="button" className="btn btn-secondary" onClick={() => navigate('/gateway/routes')}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={saveMutation.isPending}>
            {saveMutation.isPending ? 'Saving...' : isEdit ? 'Update Route' : 'Create Route'}
          </button>
        </div>
      </form>
    </div>
  );
}

export default GatewayRouteForm;
```

- [ ] **Step 3: Commit**

```bash
git add query-ui/src/pages/GatewayRouteForm.tsx query-ui/src/pages/GatewayRouteForm.css
git commit -m "feat(ui): add GatewayRouteForm create/edit page"
```

---

### Task 8: Create GatewayUpstreams page (list + modal)

**Files:**
- Create: `query-ui/src/pages/GatewayUpstreams.tsx`
- Create: `query-ui/src/pages/GatewayUpstreams.css`

- [ ] **Step 1: Create GatewayUpstreams.css**

```css
.gateway-upstreams {
  max-width: 1000px;
}

.gateway-upstreams .page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
}

.cell-nodes {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
}

.nodes-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.node-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.node-row input {
  padding: 8px 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  background: var(--bg-secondary);
  color: var(--text-primary);
  transition: border-color 0.15s;
}

.node-row input:focus {
  border-color: var(--text-tertiary);
}

.node-row input::placeholder {
  color: var(--text-tertiary);
}

.node-host { flex: 2; }
.node-port { width: 80px; }
.node-weight { width: 80px; }

.node-remove {
  background: none;
  border: none;
  color: var(--accent-red);
  cursor: pointer;
  font-size: 18px;
  padding: 0 4px;
  line-height: 1;
}

.node-remove:hover {
  color: #ff3070;
}

.add-node-btn {
  margin-top: 8px;
}
```

- [ ] **Step 2: Create GatewayUpstreams.tsx**

```tsx
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getGatewayUpstreams,
  saveGatewayUpstream,
  deleteGatewayUpstream,
  type GatewayUpstream,
} from '../api/client';
import './GatewayUpstreams.css';

interface NodeEntry {
  host: string;
  port: string;
  weight: string;
}

const emptyNode: NodeEntry = { host: '', port: '80', weight: '1' };

function nodesToEntries(nodes: Record<string, number>): NodeEntry[] {
  return Object.entries(nodes).map(([addr, weight]) => {
    const [host, port] = addr.split(':');
    return { host, port: port || '80', weight: String(weight) };
  });
}

function entriesToNodes(entries: NodeEntry[]): Record<string, number> {
  const nodes: Record<string, number> = {};
  for (const e of entries) {
    if (e.host.trim()) {
      nodes[`${e.host.trim()}:${e.port || '80'}`] = Number(e.weight) || 1;
    }
  }
  return nodes;
}

function GatewayUpstreams() {
  const queryClient = useQueryClient();

  const [showModal, setShowModal] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [type, setType] = useState('roundrobin');
  const [nodes, setNodes] = useState<NodeEntry[]>([{ ...emptyNode }]);
  const [error, setError] = useState('');

  const upstreamsQuery = useQuery({
    queryKey: ['gateway-upstreams'],
    queryFn: getGatewayUpstreams,
  });

  const saveMutation = useMutation({
    mutationFn: (data: { id: string; body: Record<string, unknown> }) =>
      saveGatewayUpstream(data.id, data.body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-upstreams'] });
      closeModal();
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? 'Failed to save upstream');
      } else {
        setError('Failed to save upstream');
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteGatewayUpstream(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-upstreams'] });
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        alert(axiosErr.response?.data?.detail ?? 'Failed to delete upstream');
      }
    },
  });

  const upstreams = upstreamsQuery.data?.items ?? [];

  function openCreate() {
    setEditingId(null);
    setName('');
    setType('roundrobin');
    setNodes([{ ...emptyNode }]);
    setError('');
    setShowModal(true);
  }

  function openEdit(u: GatewayUpstream) {
    setEditingId(u.id);
    setName(u.name || '');
    setType(u.type || 'roundrobin');
    setNodes(nodesToEntries(u.nodes || {}).length > 0 ? nodesToEntries(u.nodes) : [{ ...emptyNode }]);
    setError('');
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingId(null);
    setError('');
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const upstreamId = editingId || Date.now().toString();
    const body = {
      name: name.trim() || undefined,
      type,
      nodes: entriesToNodes(nodes),
    };
    setError('');
    saveMutation.mutate({ id: upstreamId, body });
  }

  function handleDelete(u: GatewayUpstream) {
    const label = u.name || u.id;
    if (window.confirm(`Delete upstream "${label}"?`)) {
      deleteMutation.mutate(u.id);
    }
  }

  function updateNode(index: number, field: keyof NodeEntry, value: string) {
    setNodes((prev) => prev.map((n, i) => (i === index ? { ...n, [field]: value } : n)));
  }

  function addNode() {
    setNodes((prev) => [...prev, { ...emptyNode }]);
  }

  function removeNode(index: number) {
    setNodes((prev) => prev.filter((_, i) => i !== index));
  }

  function formatNodes(nodesObj: Record<string, number>): string {
    return Object.entries(nodesObj)
      .map(([addr, w]) => `${addr} (w:${w})`)
      .join(', ');
  }

  return (
    <div className="gateway-upstreams">
      <div className="page-header">
        <div>
          <h1>Gateway Upstreams</h1>
          <p className="page-subtitle">Manage backend server groups</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>+ Add Upstream</button>
      </div>

      {upstreamsQuery.isLoading && <div className="loading-message">Loading upstreams...</div>}
      {upstreamsQuery.isError && <div className="error-banner">Failed to load upstreams.</div>}

      {upstreams.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Nodes</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {upstreams.map((u) => (
                <tr key={u.id}>
                  <td className="cell-alias">{u.name || u.id}</td>
                  <td><span className="badge badge-type">{u.type}</span></td>
                  <td className="cell-nodes">{formatNodes(u.nodes || {})}</td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-sm btn-secondary" onClick={() => openEdit(u)}>Edit</button>
                      <button className="btn btn-sm btn-danger" onClick={() => handleDelete(u)} disabled={deleteMutation.isPending}>Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!upstreamsQuery.isLoading && upstreams.length === 0 && !upstreamsQuery.isError && (
        <div className="empty-state">
          <h3>No upstreams</h3>
          <p>Click "Add Upstream" to register a backend server group.</p>
        </div>
      )}

      {/* Modal */}
      {showModal && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingId ? 'Edit Upstream' : 'Add Upstream'}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handleSubmit}>
              <div className="form-grid">
                <div className="form-group">
                  <label>Name</label>
                  <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-backend" />
                </div>
                <div className="form-group">
                  <label>Type</label>
                  <select value={type} onChange={(e) => setType(e.target.value)}>
                    <option value="roundrobin">Round Robin</option>
                    <option value="chash">Consistent Hash</option>
                    <option value="ewma">EWMA</option>
                    <option value="least_conn">Least Connections</option>
                  </select>
                </div>
                <div className="form-group form-group--full">
                  <label>Nodes</label>
                  <div className="nodes-list">
                    {nodes.map((node, idx) => (
                      <div key={idx} className="node-row">
                        <input
                          className="node-host"
                          placeholder="host"
                          value={node.host}
                          onChange={(e) => updateNode(idx, 'host', e.target.value)}
                          required
                        />
                        <input
                          className="node-port"
                          placeholder="port"
                          type="number"
                          value={node.port}
                          onChange={(e) => updateNode(idx, 'port', e.target.value)}
                        />
                        <input
                          className="node-weight"
                          placeholder="weight"
                          type="number"
                          value={node.weight}
                          onChange={(e) => updateNode(idx, 'weight', e.target.value)}
                        />
                        {nodes.length > 1 && (
                          <button type="button" className="node-remove" onClick={() => removeNode(idx)}>&times;</button>
                        )}
                      </div>
                    ))}
                    <button type="button" className="btn btn-sm btn-secondary add-node-btn" onClick={addNode}>
                      + Add Node
                    </button>
                  </div>
                </div>
              </div>

              {error && <div className="form-error">{error}</div>}

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={saveMutation.isPending}>
                  {saveMutation.isPending ? 'Saving...' : editingId ? 'Update' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default GatewayUpstreams;
```

- [ ] **Step 3: Commit**

```bash
git add query-ui/src/pages/GatewayUpstreams.tsx query-ui/src/pages/GatewayUpstreams.css
git commit -m "feat(ui): add GatewayUpstreams page with modal CRUD"
```

---

### Task 9: Register new routes in App.tsx and final build verification

**Files:**
- Modify: `query-ui/src/App.tsx`

- [ ] **Step 1: Update App.tsx with new routes**

Replace the entire contents of `query-ui/src/App.tsx`:

```tsx
import { Routes, Route, Navigate } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './pages/Dashboard';
import Connections from './pages/Connections';
import Permissions from './pages/Permissions';
import AuditLogs from './pages/AuditLogs';
import QueryPlayground from './pages/QueryPlayground';
import GatewayRoutes from './pages/GatewayRoutes';
import GatewayRouteForm from './pages/GatewayRouteForm';
import GatewayUpstreams from './pages/GatewayUpstreams';

function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/connections" element={<Connections />} />
        <Route path="/permissions" element={<Permissions />} />
        <Route path="/audit-logs" element={<AuditLogs />} />
        <Route path="/query" element={<QueryPlayground />} />
        <Route path="/gateway/routes" element={<GatewayRoutes />} />
        <Route path="/gateway/routes/new" element={<GatewayRouteForm />} />
        <Route path="/gateway/routes/:id/edit" element={<GatewayRouteForm />} />
        <Route path="/gateway/upstreams" element={<GatewayUpstreams />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

export default App;
```

- [ ] **Step 2: Verify frontend build**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds.

- [ ] **Step 3: Verify full Docker build**

Run: `cd /home/jinyoung/apihub && docker compose build`

Expected: Both query-service and query-ui build successfully.

- [ ] **Step 4: Smoke test**

Run: `cd /home/jinyoung/apihub && docker compose up -d`

Verify at http://localhost:3000:
- Sidebar shows Gateway Routes and Gateway Upstreams with divider
- Gateway Routes page loads (empty state)
- Gateway Upstreams page loads (empty state)
- Add Upstream → modal opens, create works
- Add Route → form page, select upstream, save works
- Edit/Delete route and upstream works

- [ ] **Step 5: Commit**

```bash
git add query-ui/src/App.tsx
git commit -m "feat(ui): register gateway routes in App.tsx"
```
