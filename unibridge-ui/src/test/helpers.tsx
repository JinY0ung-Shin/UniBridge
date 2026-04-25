import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, MemoryRouter } from 'react-router-dom';
import { PermissionProvider } from '../components/PermissionContext';
import { ToastProvider } from '../components/ToastContext';
import { ThemeProvider } from '../components/ThemeContext';

/* ── Default permissions for admin user ── */

export const ADMIN_PERMISSIONS = [
  'query.databases.read',
  'query.databases.write',
  'query.execute',
  'query.permissions.read',
  'query.permissions.write',
  'query.audit.read',
  'admin.roles.read',
  'admin.roles.write',
  'admin.users.read',
  'admin.users.write',
  'gateway.routes.read',
  'gateway.routes.write',
  'gateway.upstreams.read',
  'gateway.upstreams.write',
  'apikeys.read',
  'apikeys.write',
  'gateway.monitoring.read',
  'alerts.read',
  'alerts.write',
  's3.connections.read',
  's3.connections.write',
  's3.browse',
];

export const VIEWER_PERMISSIONS = [
  'query.databases.read',
  'query.execute',
  'query.audit.read',
  'gateway.routes.read',
  'gateway.upstreams.read',
  'gateway.monitoring.read',
];

/* ── Provider wrapper for tests ── */

interface RenderOptions {
  permissions?: string[];
  route?: string;
  useMemoryRouter?: boolean;
}

export function renderWithProviders(
  ui: React.ReactElement,
  options: RenderOptions = {},
) {
  const {
    permissions = ADMIN_PERMISSIONS,
    route = '/',
    useMemoryRouter = false,
  } = options;

  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  const Router = useMemoryRouter
    ? ({ children }: { children: React.ReactNode }) => (
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      )
    : ({ children }: { children: React.ReactNode }) => (
        <BrowserRouter>{children}</BrowserRouter>
      );

  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ToastProvider>
          <PermissionProvider permissions={permissions} loaded={true}>
            <Router>{ui}</Router>
          </PermissionProvider>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

/* ── Mock data factories ── */

export function makeDatabase(overrides = {}) {
  return {
    alias: 'test-db',
    db_type: 'postgres' as const,
    host: 'localhost',
    port: 5432,
    database: 'testdb',
    username: 'user',
    pool_size: 5,
    max_overflow: 3,
    query_timeout: 30,
    ...overrides,
  };
}

export function makeClickHouseDatabase(overrides = {}) {
  return {
    alias: 'test-ch',
    db_type: 'clickhouse' as const,
    host: 'localhost',
    port: 8123,
    database: 'analytics',
    username: 'default',
    protocol: 'http' as const,
    secure: false,
    pool_size: 5,
    max_overflow: 3,
    query_timeout: 30,
    ...overrides,
  };
}

export function makeAuditLog(overrides = {}) {
  return {
    id: 1,
    timestamp: '2026-04-10T12:00:00Z',
    user: 'admin',
    database_alias: 'test-db',
    sql: 'SELECT * FROM users',
    params: null,
    row_count: 10,
    elapsed_ms: 42,
    status: 'success' as const,
    error_message: undefined,
    ...overrides,
  };
}

export function makeApiKey(overrides = {}) {
  return {
    name: 'my-app',
    description: 'Test API key',
    api_key: 'key-abc***',
    key_created: true,
    allowed_databases: ['test-db'],
    allowed_routes: ['route-1'],
    created_at: '2026-04-10T12:00:00Z',
    ...overrides,
  };
}

export function makeRole(overrides = {}) {
  return {
    id: 1,
    name: 'admin',
    description: 'Administrator',
    is_system: true,
    permissions: ['query.execute', 'query.databases.read'],
    ...overrides,
  };
}

export function makeUser(overrides = {}) {
  return {
    id: 'user-1',
    username: 'testuser',
    email: 'test@example.com',
    enabled: true,
    role: 'developer',
    createdTimestamp: Date.now(),
    ...overrides,
  };
}

export function makeGatewayRoute(overrides = {}) {
  return {
    id: 'route-1',
    name: 'test-route',
    uri: '/api/test/*',
    methods: ['GET', 'POST'],
    upstream_id: 'upstream-1',
    status: 1,
    require_auth: false,
    strip_prefix: false,
    service_key: null,
    plugins: {},
    ...overrides,
  };
}

export function makeGatewayUpstream(overrides = {}) {
  return {
    id: 'upstream-1',
    name: 'test-upstream',
    type: 'roundrobin',
    nodes: { 'localhost:3000': 1 },
    ...overrides,
  };
}

export function makeS3Connection(overrides = {}) {
  return {
    alias: 's3-main',
    endpoint_url: 'https://s3.example.com',
    region: 'ap-northeast-2',
    access_key_id: 'access-key',
    secret_access_key: '',
    default_bucket: 'default-bucket',
    use_ssl: true,
    ...overrides,
  };
}
