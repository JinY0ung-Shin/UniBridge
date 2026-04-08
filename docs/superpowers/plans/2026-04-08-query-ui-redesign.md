# Query UI Full Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle the entire query-ui from a generic light admin dashboard to a Vercel Dark themed SaaS interface with Outfit + JetBrains Mono typography and deduplicated CSS architecture.

**Architecture:** Extract duplicated component styles (buttons, tables, badges, modals, forms, states) into a single `src/styles/shared.css` with CSS custom properties for design tokens. Rewrite `index.css` for reset + font + body. Rewrite each page/component CSS to use tokens and contain only page-specific styles. No React logic changes — CSS-only transformation.

**Tech Stack:** React 19, vanilla CSS with CSS Custom Properties, Google Fonts (Outfit, JetBrains Mono), Vite

---

### Task 1: Add Google Fonts and create shared.css with design tokens and common components

**Files:**
- Modify: `query-ui/index.html`
- Create: `query-ui/src/styles/shared.css`
- Modify: `query-ui/src/index.css`
- Modify: `query-ui/src/main.tsx`

- [ ] **Step 1: Add Google Fonts to index.html**

In `query-ui/index.html`, add font preconnect and stylesheet links inside `<head>`, before the existing `<link rel="icon">`:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

- [ ] **Step 2: Create `src/styles/shared.css`**

Create `query-ui/src/styles/shared.css` with all design tokens and shared component styles:

```css
/* ══════════════════════════════════════
   API Hub — Design Tokens & Shared Styles
   ══════════════════════════════════════ */

:root {
  /* Background */
  --bg-root: #000000;
  --bg-primary: #0a0a0a;
  --bg-secondary: #111111;
  --bg-tertiary: #1a1a1a;
  --bg-hover: #1a1a1a;

  /* Border */
  --border-default: #222222;
  --border-subtle: #1a1a1a;
  --border-hover: #333333;

  /* Text */
  --text-primary: #ededed;
  --text-secondary: #a1a1a1;
  --text-tertiary: #666666;
  --text-inverse: #000000;

  /* Accent */
  --accent-blue: #0070f3;
  --accent-blue-hover: #0060d3;
  --accent-green: #50e3c2;
  --accent-red: #f31260;
  --accent-yellow: #f5a623;

  /* Radius */
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 10px;

  /* Font */
  --font-sans: 'Outfit', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;

  /* Shadow */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.5);
  --shadow-lg: 0 12px 40px rgba(0, 0, 0, 0.6);
}

/* ── Page Header ── */

.page-header h1 {
  font-size: 24px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.5px;
  margin-bottom: 4px;
}

.page-subtitle {
  color: var(--text-tertiary);
  font-size: 13px;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 16px;
}

/* ── Buttons ── */

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: var(--radius-md);
  font-weight: 600;
  font-size: 13px;
  font-family: var(--font-sans);
  cursor: pointer;
  padding: 8px 16px;
  transition: all 0.15s ease;
}

.btn-primary {
  background: var(--text-primary);
  color: var(--text-inverse);
}

.btn-primary:hover {
  background: #ffffff;
}

.btn-primary:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn-secondary {
  background: var(--bg-secondary);
  color: var(--text-primary);
  border: 1px solid var(--border-default);
}

.btn-secondary:hover {
  border-color: var(--border-hover);
  background: var(--bg-tertiary);
}

.btn-danger {
  background: transparent;
  color: var(--accent-red);
  border: 1px solid rgba(243, 18, 96, 0.3);
}

.btn-danger:hover {
  background: rgba(243, 18, 96, 0.08);
}

.btn-sm {
  padding: 5px 10px;
  font-size: 12px;
}

/* ── Table ── */

.table-container {
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  overflow-x: auto;
}

.data-table {
  width: 100%;
  font-size: 13px;
  border-collapse: collapse;
}

.data-table thead {
  background: var(--bg-secondary);
}

.data-table th {
  text-align: left;
  padding: 10px 14px;
  font-weight: 600;
  color: var(--text-tertiary);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  border-bottom: 1px solid var(--border-default);
}

.data-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}

.data-table tbody tr {
  transition: background 0.1s;
}

.data-table tbody tr:hover {
  background: var(--bg-hover);
}

.data-table tbody tr:last-child td {
  border-bottom: none;
}

.cell-alias {
  font-weight: 600;
  color: var(--text-primary);
}

.action-buttons {
  display: flex;
  gap: 6px;
}

/* ── Badges ── */

.badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
}

.badge-ok {
  background: rgba(80, 227, 194, 0.1);
  color: var(--accent-green);
}

.badge-error {
  background: rgba(243, 18, 96, 0.1);
  color: var(--accent-red);
}

.badge-unknown {
  background: var(--bg-tertiary);
  color: var(--text-tertiary);
}

.badge-type {
  display: inline-block;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: rgba(0, 112, 243, 0.1);
  color: var(--accent-blue);
}

/* ── Status Dot ── */

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.status-dot--green {
  background: var(--accent-green);
  box-shadow: 0 0 6px rgba(80, 227, 194, 0.4);
}

.status-dot--red {
  background: var(--accent-red);
  box-shadow: 0 0 6px rgba(243, 18, 96, 0.4);
}

/* ── Modal ── */

.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal {
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: 12px;
  width: 520px;
  max-width: 90vw;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: var(--shadow-lg);
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 24px 0;
}

.modal-header h2 {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
}

.modal-close {
  background: none;
  border: none;
  font-size: 20px;
  color: var(--text-tertiary);
  cursor: pointer;
  padding: 4px;
  line-height: 1;
}

.modal-close:hover {
  color: var(--text-primary);
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 24px 20px;
}

/* ── Forms ── */

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  padding: 20px 24px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.form-group--full {
  grid-column: 1 / -1;
}

.form-group label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.form-group .hint {
  text-transform: none;
  font-weight: 400;
  color: var(--text-tertiary);
}

.form-group input,
.form-group select {
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

.form-group input:focus,
.form-group select:focus {
  border-color: var(--text-tertiary);
}

.form-group input::placeholder {
  color: var(--text-tertiary);
}

.form-group input:disabled {
  background: var(--bg-tertiary);
  color: var(--text-tertiary);
}

.form-group select {
  appearance: none;
  cursor: pointer;
}

.form-error {
  background: rgba(243, 18, 96, 0.1);
  color: var(--accent-red);
  padding: 8px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  margin: 0 24px 8px;
}

/* ── States ── */

.loading-message {
  text-align: center;
  padding: 48px;
  color: var(--text-tertiary);
  font-size: 14px;
}

.error-banner {
  background: rgba(243, 18, 96, 0.08);
  color: var(--accent-red);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  font-size: 13px;
  margin-bottom: 24px;
  border: 1px solid rgba(243, 18, 96, 0.2);
}

.empty-state {
  text-align: center;
  padding: 64px 24px;
  color: var(--text-tertiary);
}

.empty-state h3 {
  font-size: 18px;
  color: var(--text-secondary);
  margin-bottom: 8px;
}

.empty-state p {
  font-size: 14px;
}
```

- [ ] **Step 3: Rewrite `src/index.css`**

Replace the entire contents of `query-ui/src/index.css` with:

```css
@import './styles/shared.css';

*,
*::before,
*::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html {
  font-size: 14px;
}

body {
  font-family: var(--font-sans);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  background: var(--bg-root);
  color: var(--text-primary);
  line-height: 1.5;
}

#root {
  min-height: 100vh;
}

a {
  text-decoration: none;
  color: inherit;
}

button {
  cursor: pointer;
  font-family: inherit;
  font-size: inherit;
}

input,
select,
textarea {
  font-family: inherit;
  font-size: inherit;
}

table {
  border-collapse: collapse;
  width: 100%;
}

code,
pre,
.mono {
  font-family: var(--font-mono);
}
```

- [ ] **Step 4: Import shared.css in main.tsx**

No change needed — `main.tsx` already imports `./index.css` which now `@import`s `shared.css`.

Verify the import chain: `main.tsx` → `index.css` → `shared.css` (via `@import`).

- [ ] **Step 5: Verify build compiles**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds (CSS changes only, no TS errors).

- [ ] **Step 6: Commit**

```bash
git add query-ui/index.html query-ui/src/styles/shared.css query-ui/src/index.css
git commit -m "feat(ui): add design tokens and shared.css with Vercel Dark theme"
```

---

### Task 2: Restyle Layout (sidebar + login + main shell)

**Files:**
- Modify: `query-ui/src/components/Layout.css`
- Modify: `query-ui/src/components/Layout.tsx` (minor: update class names for logo)

- [ ] **Step 1: Rewrite `Layout.css`**

Replace the entire contents of `query-ui/src/components/Layout.css` with:

```css
.layout {
  display: flex;
  min-height: 100vh;
}

/* ── Sidebar ── */

.sidebar {
  width: 220px;
  min-width: 220px;
  background: var(--bg-root);
  border-right: 1px solid var(--border-default);
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  z-index: 100;
}

.sidebar-header {
  padding: 20px 16px 16px;
}

.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sidebar-logo-icon {
  width: 28px;
  height: 28px;
  border-radius: var(--radius-md);
  background: var(--text-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.sidebar-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.3px;
}

.sidebar-nav {
  flex: 1;
  padding: 8px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nav-link {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 500;
  color: var(--text-tertiary);
  transition: all 0.15s ease;
}

.nav-link:hover {
  background: var(--bg-hover);
  color: var(--text-secondary);
}

.nav-link--active {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.nav-link--active:hover {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.nav-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  flex-shrink: 0;
}

.sidebar-footer {
  padding: 16px;
  border-top: 1px solid var(--border-default);
}

.sidebar-version {
  font-size: 11px;
  color: var(--text-tertiary);
}

/* ── Login overlay ── */

.login-overlay {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--bg-root);
}

.login-form {
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  padding: 32px;
  border-radius: 12px;
  width: 340px;
  box-shadow: var(--shadow-lg);
}

.login-form h2 {
  margin: 0 0 24px;
  font-size: 20px;
  font-weight: 700;
  color: var(--text-primary);
  text-align: center;
}

.login-field {
  margin-bottom: 16px;
}

.login-field label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.login-field input,
.login-field select {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  font-size: 14px;
  outline: none;
  box-sizing: border-box;
  background: var(--bg-secondary);
  color: var(--text-primary);
  font-family: var(--font-sans);
  transition: border-color 0.15s;
}

.login-field input::placeholder {
  color: var(--text-tertiary);
}

.login-field input:focus,
.login-field select:focus {
  border-color: var(--text-tertiary);
}

.login-field select {
  appearance: none;
  cursor: pointer;
}

.login-error {
  background: rgba(243, 18, 96, 0.08);
  color: var(--accent-red);
  padding: 8px 12px;
  border-radius: var(--radius-md);
  font-size: 13px;
  margin-bottom: 16px;
  border: 1px solid rgba(243, 18, 96, 0.2);
}

.login-btn {
  width: 100%;
  padding: 10px;
  background: var(--text-primary);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius-md);
  font-size: 14px;
  font-weight: 600;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: background 0.15s;
}

.login-btn:hover {
  background: #ffffff;
}

.login-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* ── Main content ── */

.main-content {
  flex: 1;
  margin-left: 220px;
  padding: 32px 40px;
  min-height: 100vh;
  background: var(--bg-root);
}
```

- [ ] **Step 2: Update logo in Layout.tsx**

In `query-ui/src/components/Layout.tsx`, change the logo `<svg>` wrapper to use the new inverted style. Replace the sidebar-logo section (lines 84-92):

Replace:
```tsx
<div className="sidebar-logo">
  <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
    <rect width="28" height="28" rx="6" fill="#4361ee" />
    <path
      d="M7 10h14M7 14h14M7 18h10"
      stroke="#fff"
      strokeWidth="2"
      strokeLinecap="round"
    />
  </svg>
  <span className="sidebar-title">API Hub Admin</span>
</div>
```

With:
```tsx
<div className="sidebar-logo">
  <div className="sidebar-logo-icon">
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M3 5h10M3 8h10M3 11h7" stroke="#000" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  </div>
  <span className="sidebar-title">API Hub</span>
</div>
```

- [ ] **Step 3: Verify build compiles**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/components/Layout.css query-ui/src/components/Layout.tsx
git commit -m "feat(ui): restyle sidebar, login, and main shell to Vercel Dark"
```

---

### Task 3: Restyle Dashboard page

**Files:**
- Modify: `query-ui/src/pages/Dashboard.css`
- Modify: `query-ui/src/pages/Dashboard.tsx` (minor: remove `summary-card--success`/`summary-card--danger` classes, use inline style for colored values)

- [ ] **Step 1: Rewrite `Dashboard.css`**

Replace the entire contents of `query-ui/src/pages/Dashboard.css` with only page-specific styles:

```css
.dashboard {
  max-width: 1000px;
}

.dashboard .page-header {
  margin-bottom: 28px;
}

/* ── Summary Cards ── */

.summary-cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 32px;
}

.summary-card {
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  padding: 20px 24px;
  transition: border-color 0.15s;
}

.summary-card:hover {
  border-color: var(--border-hover);
}

.summary-card__value {
  font-size: 32px;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1;
  margin-bottom: 4px;
}

.summary-card__label {
  font-size: 13px;
  color: var(--text-tertiary);
  font-weight: 500;
}

/* ── DB Grid ── */

.db-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}

.db-card {
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
  transition: border-color 0.15s;
}

.db-card:hover {
  border-color: var(--border-hover);
}

.db-card--error {
  border-color: rgba(243, 18, 96, 0.3);
}

.db-card__header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.db-card__alias {
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary);
}

.db-card__type {
  margin-left: auto;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: rgba(0, 112, 243, 0.1);
  color: var(--accent-blue);
}

.pool-stats {
  display: flex;
  gap: 20px;
}

.pool-stat__label {
  font-size: 11px;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.pool-stat__value {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
}

.db-card__connected {
  font-size: 12px;
  color: var(--accent-green);
}

.db-card__error {
  font-size: 12px;
  color: var(--accent-red);
  background: rgba(243, 18, 96, 0.06);
  padding: 8px 10px;
  border-radius: var(--radius-sm);
}
```

- [ ] **Step 2: Update Dashboard.tsx — remove old modifier classes, use color tokens**

In `query-ui/src/pages/Dashboard.tsx`, update the summary cards section (lines 46-58).

Replace:
```tsx
<div className="summary-cards">
  <div className="summary-card">
    <div className="summary-card__value">{totalDbs}</div>
    <div className="summary-card__label">Total Databases</div>
  </div>
  <div className="summary-card summary-card--success">
    <div className="summary-card__value">{connectedCount}</div>
    <div className="summary-card__label">Connected</div>
  </div>
  <div className="summary-card summary-card--danger">
    <div className="summary-card__value">{errorCount}</div>
    <div className="summary-card__label">Errors</div>
  </div>
</div>
```

With:
```tsx
<div className="summary-cards">
  <div className="summary-card">
    <div className="summary-card__value">{totalDbs}</div>
    <div className="summary-card__label">Total Databases</div>
  </div>
  <div className="summary-card">
    <div className="summary-card__value" style={{ color: 'var(--accent-green)' }}>{connectedCount}</div>
    <div className="summary-card__label">Connected</div>
  </div>
  <div className="summary-card">
    <div className="summary-card__value" style={{ color: 'var(--accent-red)' }}>{errorCount}</div>
    <div className="summary-card__label">Errors</div>
  </div>
</div>
```

- [ ] **Step 3: Verify build compiles**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/pages/Dashboard.css query-ui/src/pages/Dashboard.tsx
git commit -m "feat(ui): restyle Dashboard page to Vercel Dark"
```

---

### Task 4: Restyle Connections page

**Files:**
- Modify: `query-ui/src/pages/Connections.css`
- Modify: `query-ui/src/pages/Connections.tsx` (minor: rename `btn-outline` → `btn-secondary`, `btn-danger-outline` → `btn-danger`, `status-badge` → `badge`)

- [ ] **Step 1: Rewrite `Connections.css`**

Replace the entire contents of `query-ui/src/pages/Connections.css` with only page-specific styles (all shared styles now come from `shared.css`):

```css
.connections {
  max-width: 1000px;
}

.connections .page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
}
```

That's it — all other styles (btn, table, badge, modal, form, states) are in `shared.css`.

- [ ] **Step 2: Update class names in Connections.tsx**

In `query-ui/src/pages/Connections.tsx`, update the following class names to match the new shared styles:

1. Line 174: `status-badge status-badge--ok` → `badge badge-ok`
2. Line 174: `status-badge--error` → `badge-error` (with base `badge`)
3. Line 178: `status-badge status-badge--unknown` → `badge badge-unknown`
4. Line 187: `btn-outline` → `btn-secondary`
5. Line 193: `btn-outline` → `btn-secondary`
6. Line 199: `btn-danger-outline` → `btn-danger`

Full replacement for the Status `<td>` (lines 173-180):
```tsx
<td>
  {testResult ? (
    <span className={`badge ${testResult.status === 'error' ? 'badge-error' : 'badge-ok'}`}>
      {testResult.status === 'error' ? 'Error' : 'OK'}
    </span>
  ) : (
    <span className="badge badge-unknown">--</span>
  )}
</td>
```

Full replacement for the Actions `<td>` (lines 182-204):
```tsx
<td>
  <div className="action-buttons">
    <button
      className="btn btn-sm btn-secondary"
      onClick={() => handleTest(db.alias)}
      disabled={testMutation.isPending}
    >
      Test
    </button>
    <button
      className="btn btn-sm btn-secondary"
      onClick={() => openEdit(db)}
    >
      Edit
    </button>
    <button
      className="btn btn-sm btn-danger"
      onClick={() => handleDelete(db.alias)}
      disabled={deleteMutation.isPending}
    >
      Delete
    </button>
  </div>
</td>
```

- [ ] **Step 3: Verify build compiles**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/pages/Connections.css query-ui/src/pages/Connections.tsx
git commit -m "feat(ui): restyle Connections page, deduplicate CSS"
```

---

### Task 5: Restyle Permissions page

**Files:**
- Modify: `query-ui/src/pages/Permissions.css`
- Modify: `query-ui/src/pages/Permissions.tsx` (minor: rename `btn-danger-outline` → `btn-danger`)

- [ ] **Step 1: Rewrite `Permissions.css`**

Replace the entire contents of `query-ui/src/pages/Permissions.css`:

```css
.permissions {
  max-width: 1000px;
}

.permissions .page-header {
  margin-bottom: 28px;
}

/* ── Add permission row ── */

.add-perm-row {
  display: flex;
  gap: 10px;
  margin-bottom: 24px;
  align-items: center;
}

.perm-input,
.perm-select {
  padding: 8px 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  background: var(--bg-secondary);
  color: var(--text-primary);
  transition: border-color 0.15s;
}

.perm-input::placeholder {
  color: var(--text-tertiary);
}

.perm-input:focus,
.perm-select:focus {
  border-color: var(--text-tertiary);
}

.perm-input {
  width: 180px;
}

.perm-select {
  width: 200px;
  appearance: none;
  cursor: pointer;
}

/* ── Table tweaks ── */

.th-center,
.td-center {
  text-align: center !important;
}

.perm-checkbox {
  width: 16px;
  height: 16px;
  accent-color: var(--accent-blue);
  cursor: pointer;
}
```

- [ ] **Step 2: Update Permissions.tsx — rename `btn-danger-outline` → `btn-danger`**

In `query-ui/src/pages/Permissions.tsx` line 152, replace:
```tsx
className="btn btn-sm btn-danger-outline"
```
With:
```tsx
className="btn btn-sm btn-danger"
```

- [ ] **Step 3: Verify build compiles**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/pages/Permissions.css query-ui/src/pages/Permissions.tsx
git commit -m "feat(ui): restyle Permissions page, deduplicate CSS"
```

---

### Task 6: Restyle Audit Logs page

**Files:**
- Modify: `query-ui/src/pages/AuditLogs.css`
- Modify: `query-ui/src/pages/AuditLogs.tsx` (minor: rename `status-badge` → `badge`, `btn-outline` → `btn-secondary`)

- [ ] **Step 1: Rewrite `AuditLogs.css`**

Replace the entire contents of `query-ui/src/pages/AuditLogs.css`:

```css
.audit-logs {
  max-width: 1000px;
}

.audit-logs .page-header {
  margin-bottom: 28px;
}

/* ── Filter bar ── */

.filter-bar {
  display: flex;
  gap: 10px;
  margin-bottom: 20px;
  flex-wrap: wrap;
  align-items: center;
}

.filter-select,
.filter-input {
  padding: 8px 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  background: var(--bg-secondary);
  color: var(--text-primary);
  transition: border-color 0.15s;
}

.filter-select:focus,
.filter-input:focus {
  border-color: var(--text-tertiary);
}

.filter-select {
  min-width: 160px;
  appearance: none;
  cursor: pointer;
}

.filter-input {
  width: 150px;
}

.filter-input::placeholder {
  color: var(--text-tertiary);
}

/* ── Audit table tweaks ── */

.audit-table .audit-row {
  cursor: pointer;
}

.audit-table .audit-row:hover {
  background: var(--bg-hover) !important;
}

.audit-row--expanded {
  background: var(--bg-secondary) !important;
}

.cell-timestamp {
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-tertiary) !important;
}

.cell-sql {
  font-family: var(--font-mono);
  font-size: 12px;
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

/* ── Detail row ── */

.audit-detail-row td {
  padding: 0 !important;
  background: var(--bg-secondary);
}

.audit-detail {
  padding: 16px 20px;
  border-top: 1px solid var(--border-default);
}

.detail-section {
  margin-bottom: 12px;
}

.detail-section h4 {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--text-tertiary);
  margin-bottom: 6px;
}

.detail-sql {
  background: var(--bg-tertiary);
  color: var(--text-primary);
  padding: 12px 14px;
  border-radius: var(--radius-md);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.detail-error {
  background: rgba(243, 18, 96, 0.06);
  color: var(--accent-red);
  padding: 12px 14px;
  border-radius: var(--radius-md);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre-wrap;
}

.detail-meta {
  display: flex;
  gap: 20px;
  font-size: 12px;
  color: var(--text-tertiary);
  padding-top: 8px;
}

/* ── Pagination ── */

.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  margin-top: 20px;
}

.page-info {
  font-size: 13px;
  color: var(--text-tertiary);
}
```

- [ ] **Step 2: Update class names in AuditLogs.tsx**

In `query-ui/src/pages/AuditLogs.tsx`:

1. Lines 158-161 — replace status badges:
```tsx
<span
  className={`badge ${log.status === 'error' ? 'badge-error' : 'badge-ok'}`}
>
  {log.status}
</span>
```

2. Line 209 — replace Previous button:
```tsx
className="btn btn-sm btn-secondary"
```

3. Line 219 — replace Next button:
```tsx
className="btn btn-sm btn-secondary"
```

- [ ] **Step 3: Verify build compiles**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/pages/AuditLogs.css query-ui/src/pages/AuditLogs.tsx
git commit -m "feat(ui): restyle Audit Logs page, deduplicate CSS"
```

---

### Task 7: Restyle Query Playground page

**Files:**
- Modify: `query-ui/src/pages/QueryPlayground.css`
- Modify: `query-ui/src/pages/QueryPlayground.tsx` (add terminal chrome to editor)

- [ ] **Step 1: Rewrite `QueryPlayground.css`**

Replace the entire contents of `query-ui/src/pages/QueryPlayground.css`:

```css
.query-playground {
  max-width: 1000px;
}

.query-playground .page-header {
  margin-bottom: 20px;
}

/* ── Controls ── */

.playground-controls {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 16px;
}

.db-selector {
  padding: 8px 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
  background: var(--bg-secondary);
  color: var(--text-primary);
  min-width: 200px;
  transition: border-color 0.15s;
  appearance: none;
  cursor: pointer;
}

.db-selector:focus {
  border-color: var(--text-tertiary);
}

.shortcut-hint {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-left: 4px;
  font-family: var(--font-mono);
}

/* ── SQL Editor ── */

.editor-container {
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  overflow: hidden;
  margin-bottom: 16px;
}

.editor-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-default);
}

.editor-topbar-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.editor-topbar-dots {
  display: flex;
  gap: 6px;
}

.editor-topbar-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--bg-tertiary);
  border: 1px solid var(--border-default);
}

.sql-editor {
  width: 100%;
  padding: 16px 18px;
  border: none;
  background: transparent;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-primary);
  resize: vertical;
  outline: none;
  min-height: 160px;
  display: block;
}

.sql-editor::placeholder {
  color: var(--text-tertiary);
}

/* ── Error ── */

.query-error {
  background: rgba(243, 18, 96, 0.08);
  color: var(--accent-red);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  font-size: 13px;
  margin-bottom: 16px;
  border: 1px solid rgba(243, 18, 96, 0.2);
}

.query-error strong {
  font-weight: 700;
}

/* ── Truncated warning ── */

.truncated-warning {
  background: rgba(245, 166, 35, 0.12);
  color: var(--accent-yellow);
  padding: 10px 16px;
  border-radius: var(--radius-md);
  font-size: 13px;
  margin-bottom: 10px;
  border: 1px solid rgba(245, 166, 35, 0.3);
  font-weight: 500;
}

/* ── Results ── */

.query-results {
  margin-top: 8px;
}

.results-meta {
  display: flex;
  gap: 20px;
  font-size: 12px;
  color: var(--text-tertiary);
  margin-bottom: 10px;
  padding: 0 2px;
  font-family: var(--font-mono);
}

.results-table-container {
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  overflow-x: auto;
  max-height: 500px;
  overflow-y: auto;
}

.results-table {
  width: 100%;
  font-size: 12px;
  border-collapse: collapse;
}

.results-table thead {
  background: var(--bg-secondary);
  position: sticky;
  top: 0;
  z-index: 1;
}

.results-table th {
  text-align: left;
  padding: 8px 12px;
  font-weight: 600;
  color: var(--text-tertiary);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  border-bottom: 1px solid var(--border-default);
  white-space: nowrap;
  font-family: var(--font-mono);
}

.results-table td {
  padding: 7px 12px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 12px;
}

.results-table tbody tr {
  transition: background 0.1s;
}

.results-table tbody tr:hover {
  background: var(--bg-hover);
}

.results-table tbody tr:last-child td {
  border-bottom: none;
}

.null-value {
  color: var(--text-tertiary);
  font-style: italic;
}

.no-rows {
  text-align: center;
  padding: 32px;
  color: var(--text-tertiary);
  font-size: 14px;
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
}
```

- [ ] **Step 2: Update QueryPlayground.tsx — add terminal chrome to editor**

In `query-ui/src/pages/QueryPlayground.tsx`, replace the editor area (lines 83-93):

Replace:
```tsx
<div className="editor-area">
  <textarea
    className="sql-editor mono"
    placeholder="SELECT * FROM users LIMIT 10;"
    value={sql}
    onChange={(e) => setSql(e.target.value)}
    onKeyDown={handleKeyDown}
    rows={10}
    spellCheck={false}
  />
</div>
```

With:
```tsx
<div className="editor-container">
  <div className="editor-topbar">
    <span className="editor-topbar-label">SQL</span>
    <div className="editor-topbar-dots">
      <span className="editor-topbar-dot" />
      <span className="editor-topbar-dot" />
      <span className="editor-topbar-dot" />
    </div>
  </div>
  <textarea
    className="sql-editor"
    placeholder="SELECT * FROM users LIMIT 10;"
    value={sql}
    onChange={(e) => setSql(e.target.value)}
    onKeyDown={handleKeyDown}
    rows={10}
    spellCheck={false}
  />
</div>
```

- [ ] **Step 3: Verify build compiles**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds.

- [ ] **Step 4: Commit**

```bash
git add query-ui/src/pages/QueryPlayground.css query-ui/src/pages/QueryPlayground.tsx
git commit -m "feat(ui): restyle Query Playground with terminal chrome editor"
```

---

### Task 8: Final verification and cleanup

**Files:**
- Verify: all CSS and TSX files

- [ ] **Step 1: Full build check**

Run: `cd /home/jinyoung/apihub/query-ui && npx tsc -b && npx vite build`

Expected: Build succeeds with zero errors.

- [ ] **Step 2: Docker build check**

Run: `cd /home/jinyoung/apihub && docker compose build query-ui`

Expected: Docker image builds successfully.

- [ ] **Step 3: Visual smoke test**

Run: `cd /home/jinyoung/apihub && docker compose up -d`

Open http://localhost:3000 in a browser. Verify:
- Login page: dark background, white card, styled form inputs
- Dashboard: dark theme, summary cards with colored values, DB grid
- Connections: table with dark styling, modal with dark form
- Permissions: dark table with styled checkboxes
- Audit Logs: dark table with expandable rows, dark code blocks
- Query Playground: terminal-chrome editor, dark results table

- [ ] **Step 4: Commit (if any final tweaks)**

```bash
git add -A query-ui/
git commit -m "chore(ui): final cleanup after redesign"
```
