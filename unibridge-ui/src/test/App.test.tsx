import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

/* ── Mock the entire api/client module ── */

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getHealth: vi.fn().mockResolvedValue({
    status: 'ok',
    databases: {
      mydb: { status: 'ok', pool_active: 1, pool_idle: 4 },
    },
  }),
  getAdminDatabases: vi.fn().mockResolvedValue([
    { alias: 'mydb', db_type: 'postgres', host: 'localhost', port: 5432, database: 'test', username: 'u', pool_size: 5, max_overflow: 10, query_timeout: 30 },
  ]),
  getDatabases: vi.fn().mockResolvedValue([]),
  getAuthRoles: vi.fn().mockResolvedValue(['admin', 'developer', 'viewer']),
  getCurrentUser: vi.fn().mockResolvedValue({
    username: 'test',
    role: 'admin',
    permissions: [
      'query.databases.read',
      'query.execute',
      'query.permissions.read',
      'query.audit.read',
      'admin.roles.read',
      'gateway.routes.read',
      'gateway.upstreams.read',
      'apikeys.read',
      'gateway.monitoring.read',
    ],
  }),
  getToken: vi.fn().mockResolvedValue({ access_token: 'fake-token' }),
  getPermissions: vi.fn().mockResolvedValue([]),
  getAuditLogs: vi.fn().mockResolvedValue([]),
  executeQuery: vi.fn().mockResolvedValue({ columns: [], rows: [], row_count: 0, elapsed_ms: 0, truncated: false }),
  createDatabase: vi.fn(),
  updateDatabase: vi.fn(),
  deleteDatabase: vi.fn(),
  testDatabase: vi.fn(),
  updatePermission: vi.fn(),
  deletePermission: vi.fn(),
  getGatewayRoutes: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  getGatewayRoute: vi.fn(),
  saveGatewayRoute: vi.fn(),
  deleteGatewayRoute: vi.fn(),
  getGatewayUpstreams: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  getGatewayUpstream: vi.fn(),
  saveGatewayUpstream: vi.fn(),
  deleteGatewayUpstream: vi.fn(),
  getApiKeys: vi.fn().mockResolvedValue([]),
  createApiKey: vi.fn(),
  updateApiKey: vi.fn(),
  deleteApiKey: vi.fn(),
  getMetricsSummary: vi.fn().mockResolvedValue({ total_requests: 0, error_rate: 0, avg_latency_ms: 0 }),
  getMetricsRequests: vi.fn().mockResolvedValue([]),
  getMetricsStatusCodes: vi.fn().mockResolvedValue([]),
  getMetricsLatency: vi.fn().mockResolvedValue({ p50: [], p95: [], p99: [] }),
  getMetricsTopRoutes: vi.fn().mockResolvedValue([]),
  getRoles: vi.fn().mockResolvedValue([]),
  getRole: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
  getAllPermissions: vi.fn().mockResolvedValue([]),
}));

/* ── Import App after mocking ── */

import App from '../App';

/* ── Helper: render with required providers ── */

function renderWithProviders(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{ui}</BrowserRouter>
    </QueryClientProvider>,
  );
}

/* ── Test suite ── */

describe('App', () => {
  beforeEach(() => {
    localStorage.setItem('auth_token', 'fake-token');
    // Reset URL to root before each test
    window.history.pushState({}, '', '/');
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('renders without crashing', () => {
    renderWithProviders(<App />);
    // The app should mount without throwing
    expect(document.body).toBeInTheDocument();
  });

  it('shows sidebar navigation when authenticated', async () => {
    renderWithProviders(<App />);

    // Wait for getCurrentUser to resolve so the sidebar nav items render
    // Use the sidebar-nav container to scope our query
    await waitFor(() => {
      const sidebar = document.querySelector('.sidebar-nav');
      expect(sidebar).toBeInTheDocument();
      // The sidebar should contain nav links
      const navLinks = sidebar!.querySelectorAll('.nav-link');
      expect(navLinks.length).toBeGreaterThan(0);
    });

    // Sidebar title
    expect(screen.getByText('UniBridge')).toBeInTheDocument();
  });

  it('renders the sidebar title "UniBridge"', async () => {
    renderWithProviders(<App />);

    await waitFor(() => {
      expect(screen.getByText('UniBridge')).toBeInTheDocument();
    });
  });

  it('renders navigation links for permitted pages', async () => {
    renderWithProviders(<App />);

    // Wait for permissions to load and navigation links to appear
    await waitFor(() => {
      expect(screen.getByText('Connections')).toBeInTheDocument();
    });

    // All nav items that the mocked user has permission for should be present
    // "Dashboard" appears both as a nav link and as the page h1, so use getAllByText
    expect(screen.getAllByText('Dashboard').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Connections')).toBeInTheDocument();
    expect(screen.getByText('Permissions')).toBeInTheDocument();
    expect(screen.getByText('Audit Logs')).toBeInTheDocument();
    expect(screen.getByText('Query Playground')).toBeInTheDocument();
    expect(screen.getByText('Gateway Routes')).toBeInTheDocument();
    expect(screen.getByText('Gateway Upstreams')).toBeInTheDocument();
    expect(screen.getByText('API Keys')).toBeInTheDocument();
    // "Gateway Monitoring" appears both as a nav link and as the Dashboard section title
    expect(screen.getAllByText('Gateway Monitoring').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Roles')).toBeInTheDocument();
  });

  it('hides menu items when user lacks permissions', async () => {
    // Override getCurrentUser to return a viewer with limited permissions
    const { getCurrentUser } = await import('../api/client');
    vi.mocked(getCurrentUser).mockResolvedValueOnce({
      username: 'viewer',
      role: 'viewer',
      permissions: ['query.databases.read', 'query.execute'],
    });

    renderWithProviders(<App />);

    await waitFor(() => {
      expect(screen.getByText('Connections')).toBeInTheDocument();
    });

    // These should NOT appear for a user without the required permissions
    expect(screen.queryByText('Permissions')).not.toBeInTheDocument();
    expect(screen.queryByText('Audit Logs')).not.toBeInTheDocument();
    expect(screen.queryByText('Roles')).not.toBeInTheDocument();
    expect(screen.queryByText('API Keys')).not.toBeInTheDocument();
  });

  it('redirects to / when navigating to unauthorized route', async () => {
    // User lacks gateway.routes.read permission
    const { getCurrentUser } = await import('../api/client');
    vi.mocked(getCurrentUser).mockResolvedValueOnce({
      username: 'viewer',
      role: 'viewer',
      permissions: ['query.databases.read'],
    });

    window.history.pushState({}, '', '/gateway/routes');

    renderWithProviders(<App />);

    // Should redirect to dashboard (/) and show dashboard content
    await waitFor(() => {
      expect(screen.getByText('Total Databases')).toBeInTheDocument();
    });

    // Should NOT show the gateway routes page
    expect(screen.queryByText('Gateway Routes')).not.toBeInTheDocument();
  });

  it('Dashboard renders loading state then summary cards', async () => {
    renderWithProviders(<App />);

    // After data loads, summary cards should appear
    await waitFor(() => {
      expect(screen.getByText('Total Databases')).toBeInTheDocument();
    });

    // "Connected" appears both as a summary card label and inside the db-card body
    // Use the summary card label selector to be specific
    const summaryLabels = document.querySelectorAll('.summary-card__label');
    const labelTexts = Array.from(summaryLabels).map((el) => el.textContent);
    expect(labelTexts).toContain('Total Databases');
    expect(labelTexts).toContain('Connected');
    expect(labelTexts).toContain('Errors');

    // The mock returns 1 database in health and 1 in admin, both "ok"
    // totalDbs = databases.length || healthEntries.length = 1
    // connectedCount = 1, errorCount = 0
    await waitFor(() => {
      const values = document.querySelectorAll('.summary-card__value');
      expect(values).toHaveLength(3);
      expect(values[0]).toHaveTextContent('1'); // Total
      expect(values[1]).toHaveTextContent('1'); // Connected
      expect(values[2]).toHaveTextContent('0'); // Errors
    });
  });
});
