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
  'query.settings.read',
  'query.settings.write',
  'admin.roles.read',
  'admin.roles.write',
  'admin.users.read',
  'admin.users.write',
  'admin.audit.read',
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
  'nas.connections.read',
  'nas.connections.write',
  'nas.browse',
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

export function makeSavedQuery(overrides = {}) {
  return {
    id: 1,
    name: 'My users',
    database_alias: 'test-db' as string | null,
    sql_text: 'SELECT * FROM users',
    description: '',
    created_at: '2026-06-10T12:00:00Z',
    updated_at: '2026-06-10T12:00:00Z',
    ...overrides,
  };
}

export function makeAdminAuditLog(overrides = {}) {
  return {
    id: 1,
    timestamp: '2026-04-10T12:00:00Z',
    actor: 'admin',
    action: 'update' as const,
    resource_type: 'route' as const,
    resource_id: 'route-1',
    summary: 'Updated route route-1',
    before: '{"name": "old"}',
    after: '{"name": "new"}',
    status: 'success',
    error_message: null as string | null,
    ...overrides,
  };
}

export function makeApiKey(overrides = {}) {
  return {
    name: 'my-app',
    description: 'Test API key',
    api_key: 'key-abc***',
    key_created: true,
    is_master: false,
    allowed_databases: ['test-db'],
    allowed_routes: ['route-1'],
    rate_limit_per_minute: null,
    allow_insert: false,
    allow_update: false,
    allow_delete: false,
    allowed_tables: null,
    owner: null,
    expires_at: null,
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
    role: 'user',
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
    service_keys: [],
    plugins: {},
    ...overrides,
  };
}

export function makeGatewayUpstream(overrides = {}) {
  return {
    id: 'upstream-1',
    name: 'test-upstream',
    scheme: 'http' as const,
    pass_host: 'node' as const,
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

export function makeNasConnection(overrides = {}) {
  return {
    alias: 'nas-main',
    base_path: '/mnt/share',
    read_only: true,
    max_download_bytes: null as number | null,
    show_hidden: false,
    follow_symlinks: false,
    status: 'registered',
    ...overrides,
  };
}
