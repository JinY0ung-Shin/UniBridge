import { describe, expect, it, vi, beforeEach } from 'vitest';
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig, AxiosRequestHeaders } from 'axios';
import type {
  DatabaseConfig,
  Permission,
  S3ConnectionConfig,
} from '../api/client';

type KeycloakMock = {
  authenticated: boolean;
  token: string;
  updateToken: ReturnType<typeof vi.fn>;
  login: ReturnType<typeof vi.fn>;
  logout: ReturnType<typeof vi.fn>;
};

function makeKeycloakMock(): KeycloakMock {
  return {
    authenticated: true,
    token: 'token-1',
    updateToken: vi.fn().mockResolvedValue(true),
    login: vi.fn(),
    logout: vi.fn(),
  };
}

async function importClient(keycloak: KeycloakMock) {
  vi.resetModules();
  vi.doMock('../keycloak', () => ({ default: keycloak }));
  const mod = await import('../api/client');
  mod.setApiAuthReady(true);
  return mod;
}

function makeAdapter(handler: (config: InternalAxiosRequestConfig) => unknown) {
  const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
    const data = handler(config);
    return {
      data,
      status: 200,
      statusText: 'OK',
      headers: {},
      config,
    } satisfies AxiosResponse;
  }) as unknown as AxiosAdapter;
  return adapter;
}

describe('api client API helpers', () => {
  let keycloak: KeycloakMock;

  beforeEach(() => {
    keycloak = makeKeycloakMock();
  });

  it('getDatabases / getAdminDatabases / getHealth', async () => {
    const mod = await importClient(keycloak);
    mod.default.defaults.adapter = makeAdapter((config) => {
      if (config.url === '/query/databases') return [{ alias: 'a' }];
      if (config.url === '/admin/query/databases') return [{ alias: 'b' }];
      if (config.url === '/health/databases') return { status: 'ok', databases: {} };
      return null;
    });
    expect(await mod.getDatabases()).toEqual([{ alias: 'a' }]);
    expect(await mod.getAdminDatabases()).toEqual([{ alias: 'b' }]);
    expect(await mod.getHealth()).toEqual({ status: 'ok', databases: {} });
  });

  it('executeQuery POSTs to /query/execute', async () => {
    const mod = await importClient(keycloak);
    const adapter = makeAdapter((config) => {
      expect(config.method).toBe('post');
      expect(config.url).toBe('/query/execute');
      expect(JSON.parse(config.data)).toEqual({ database: 'd', sql: 'SELECT 1' });
      return { columns: [], rows: [], row_count: 0, elapsed_ms: 1, truncated: false };
    });
    mod.default.defaults.adapter = adapter;
    const res = await mod.executeQuery({ database: 'd', sql: 'SELECT 1' });
    expect(res.row_count).toBe(0);
    expect(adapter).toHaveBeenCalledOnce();
  });

  it('createDatabase / updateDatabase / deleteDatabase / testDatabase', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return { alias: 'a' };
    });

    await mod.createDatabase({ alias: 'a' } as DatabaseConfig);
    await mod.updateDatabase('a', { host: 'x' });
    await mod.deleteDatabase('a');
    await mod.testDatabase('a');

    expect(calls).toEqual([
      { method: 'post', url: '/admin/query/databases' },
      { method: 'put', url: '/admin/query/databases/a' },
      { method: 'delete', url: '/admin/query/databases/a' },
      { method: 'post', url: '/admin/query/databases/a/test' },
    ]);
  });

  it('permissions endpoints', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return [{ id: 1 }];
    });
    await mod.getPermissions();
    await mod.updatePermission({
      role: 'r',
      db_alias: 'd',
      allow_select: true,
      allow_insert: false,
      allow_update: false,
      allow_delete: false,
    } satisfies Permission);
    await mod.deletePermission(7);
    await mod.getDbTables('a');
    expect(calls.map((c) => c.url)).toEqual([
      '/admin/query/permissions',
      '/admin/query/permissions',
      '/admin/query/permissions/7',
      '/admin/query/databases/a/tables',
    ]);
    expect(calls[1].method).toBe('put');
  });

  it('getAuditLogs forwards params', async () => {
    const mod = await importClient(keycloak);
    let captured: Record<string, unknown> | undefined;
    mod.default.defaults.adapter = makeAdapter((c) => {
      captured = c.params;
      return [];
    });
    await mod.getAuditLogs({ database: 'd', limit: 5 });
    expect(captured).toEqual({ database: 'd', limit: 5 });
  });

  it('saved-query, query-history, and admin-audit endpoints preserve request shapes', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string; params?: unknown; data?: unknown }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({
        method: c.method,
        url: c.url,
        params: c.params,
        data: c.data ? JSON.parse(c.data) : undefined,
      });
      return {};
    });

    await mod.getQueryHistory({ database_alias: 'main', limit: 10 });
    await mod.getSavedQueries();
    await mod.createSavedQuery({ name: 'daily', sql_text: 'SELECT 1' });
    await mod.updateSavedQuery(4, { description: 'updated' });
    await mod.deleteSavedQuery(4);
    await mod.getAdminAuditLogs({ actor: 'admin', offset: 20 });

    expect(calls).toEqual([
      { method: 'get', url: '/query/history', params: { database_alias: 'main', limit: 10 }, data: undefined },
      { method: 'get', url: '/query/saved', params: undefined, data: undefined },
      { method: 'post', url: '/query/saved', params: undefined, data: { name: 'daily', sql_text: 'SELECT 1' } },
      { method: 'put', url: '/query/saved/4', params: undefined, data: { description: 'updated' } },
      { method: 'delete', url: '/query/saved/4', params: undefined, data: undefined },
      { method: 'get', url: '/admin/audit-logs', params: { actor: 'admin', offset: 20 }, data: undefined },
    ]);
  });

  it('query settings', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return { rate_limit_per_minute: 60, max_concurrent_queries: 10, blocked_sql_keywords: [] };
    });
    await mod.getQuerySettings();
    await mod.updateQuerySettings({ rate_limit_per_minute: 100 });
    expect(calls).toEqual([
      { method: 'get', url: '/admin/query/settings' },
      { method: 'put', url: '/admin/query/settings' },
    ]);
  });

  it('query template endpoints', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return { id: 1, path: 'reports/users' };
    });

    await mod.getQueryTemplates();
    await mod.createQueryTemplate({
      path: 'reports/users',
      name: 'Users',
      database: 'main',
      sql: 'SELECT * FROM users',
    });
    await mod.updateQueryTemplate('reports/users', { enabled: false });
    await mod.deleteQueryTemplate('reports/users');
    await mod.executeQueryTemplate('reports/users', { params: { active: true } });

    expect(calls).toEqual([
      { method: 'get', url: '/admin/query/templates' },
      { method: 'post', url: '/admin/query/templates' },
      { method: 'put', url: '/admin/query/templates/reports/users' },
      { method: 'delete', url: '/admin/query/templates/reports/users' },
      { method: 'post', url: '/query/templates/reports/users' },
    ]);
  });

  it('getToken POSTs auth token request', async () => {
    const mod = await importClient(keycloak);
    let body: { username?: string; role?: string } | undefined;
    mod.default.defaults.adapter = makeAdapter((c) => {
      body = JSON.parse(c.data);
      expect(c.url).toBe('/auth/token');
      return { access_token: 'jwt.token' };
    });
    const res = await mod.getToken('user', 'admin');
    expect(body).toEqual({ username: 'user', role: 'admin' });
    expect(res.access_token).toBe('jwt.token');
  });

  it('gateway routes', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return {};
    });
    await mod.getGatewayRoutes();
    await mod.getGatewayRoute('r1');
    await mod.saveGatewayRoute('r1', { uri: '/x' });
    await mod.deleteGatewayRoute('r1');
    await mod.testGatewayRoute('r1');
    await mod.getGatewayRouteCurl('r1');
    await mod.getGatewayOpenApiSpec();
    expect(calls).toEqual([
      { method: 'get', url: '/admin/gateway/routes' },
      { method: 'get', url: '/admin/gateway/routes/r1' },
      { method: 'put', url: '/admin/gateway/routes/r1' },
      { method: 'delete', url: '/admin/gateway/routes/r1' },
      { method: 'post', url: '/admin/gateway/routes/r1/test' },
      { method: 'get', url: '/admin/gateway/routes/r1/curl' },
      { method: 'get', url: '/admin/gateway/openapi.json' },
    ]);
  });

  it('gateway upstreams', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return {};
    });
    await mod.getGatewayUpstreams();
    await mod.getGatewayUpstream('u1');
    await mod.saveGatewayUpstream('u1', { type: 'roundrobin' });
    await mod.deleteGatewayUpstream('u1');
    expect(calls).toEqual([
      { method: 'get', url: '/admin/gateway/upstreams' },
      { method: 'get', url: '/admin/gateway/upstreams/u1' },
      { method: 'put', url: '/admin/gateway/upstreams/u1' },
      { method: 'delete', url: '/admin/gateway/upstreams/u1' },
    ]);
  });

  it('gateway metrics endpoints', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ url?: string; params?: Record<string, unknown> }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ url: c.url, params: c.params });
      return {};
    });
    await mod.getMetricsSummary({ kind: 'preset', value: '6h' }, 'r1');
    await mod.getMetricsRequests({ kind: 'custom', start: 1000, end: 2000 });
    await mod.getMetricsStatusCodes({ kind: 'preset', value: '1h' }, 'r1');

    expect(calls[0]).toEqual({ url: '/admin/gateway/metrics/summary', params: { range: '6h', route: 'r1' } });
    expect(calls[1]).toEqual({ url: '/admin/gateway/metrics/requests', params: { start: 1000, end: 2000, route: undefined } });
    expect(calls[2].params).toEqual({ range: '1h', route: 'r1' });
  });

  it('covers gateway metric comparison, latency, totals, and default-selection helpers', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ url?: string; params?: Record<string, unknown> }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ url: c.url, params: c.params });
      return {};
    });

    await mod.getMetricsLatency({ kind: 'preset', value: '24h' }, 'orders', 'client-a');
    await mod.getMetricsTopRoutes();
    await mod.getMetricsRequestsTotal({ kind: 'custom', start: 10, end: 20 }, undefined, 'client-b', 'day');
    await mod.getMetricsRoutesComparison({ kind: 'preset', value: '6h' }, 'client-c');
    await mod.getMetricsConsumersComparison({ kind: 'preset', value: '1h' });
    await mod.getRoutesComparisonSeries({ kind: 'preset', value: '6h' }, 'client-c', 'week');
    await mod.getConsumersComparisonSeries({ kind: 'custom', start: 100, end: 200 }, 'hour');

    expect(calls).toEqual([
      { url: '/admin/gateway/metrics/latency', params: { range: '24h', route: 'orders', consumer: 'client-a' } },
      { url: '/admin/gateway/metrics/top-routes', params: { range: '1h' } },
      { url: '/admin/gateway/metrics/requests-total', params: { start: 10, end: 20, bucket: 'day', route: undefined, consumer: 'client-b' } },
      { url: '/admin/gateway/metrics/routes-comparison', params: { range: '6h', consumer: 'client-c' } },
      { url: '/admin/gateway/metrics/consumers-comparison', params: { range: '1h' } },
      { url: '/admin/gateway/metrics/routes-comparison-series', params: { range: '6h', bucket: 'week', consumer: 'client-c' } },
      { url: '/admin/gateway/metrics/consumers-comparison-series', params: { start: 100, end: 200, bucket: 'hour' } },
    ]);
  });

  it('llm metrics endpoints', async () => {
    const mod = await importClient(keycloak);
    const urls: Array<string | undefined> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      urls.push(c.url);
      return {};
    });
    await mod.getLlmSummary();
    await mod.getLlmTokens();
    await mod.getLlmByModel();
    await mod.getLlmTopKeys();
    await mod.getLlmErrors();
    await mod.getLlmRequestsTotal();
    expect(urls).toEqual([
      '/admin/gateway/metrics/llm/summary',
      '/admin/gateway/metrics/llm/tokens',
      '/admin/gateway/metrics/llm/by-model',
      '/admin/gateway/metrics/llm/top-keys',
      '/admin/gateway/metrics/llm/errors',
      '/admin/gateway/metrics/llm/requests-total',
    ]);
  });

  it('covers LLM status and bucketed series request parameters', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ url?: string; params?: Record<string, unknown> }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ url: c.url, params: c.params });
      return {};
    });

    await mod.getLlmStatusCodes({ kind: 'preset', value: '24h' }, 'key-a');
    await mod.getLlmByModelSeries({ kind: 'custom', start: 1, end: 2 }, 'day', 'key-b');
    await mod.getLlmTopKeysSeries({ kind: 'preset', value: '6h' }, 'week', 'key-c');
    await mod.getLlmTokens({ kind: 'preset', value: '1h' }, 'hour', 'key-d');
    await mod.getLlmErrors({ kind: 'preset', value: '1h' }, 'auto', 'key-e');
    await mod.getLlmRequestsTotal({ kind: 'preset', value: '1h' }, 'day', 'key-f');

    expect(calls).toEqual([
      { url: '/admin/gateway/metrics/llm/status-codes', params: { range: '24h', api_key: 'key-a' } },
      { url: '/admin/gateway/metrics/llm/by-model-series', params: { start: 1, end: 2, bucket: 'day', api_key: 'key-b' } },
      { url: '/admin/gateway/metrics/llm/top-keys-series', params: { range: '6h', bucket: 'week', api_key: 'key-c' } },
      { url: '/admin/gateway/metrics/llm/tokens', params: { range: '1h', bucket: 'hour', api_key: 'key-d' } },
      { url: '/admin/gateway/metrics/llm/errors', params: { range: '1h', api_key: 'key-e' } },
      { url: '/admin/gateway/metrics/llm/requests-total', params: { range: '1h', bucket: 'day', api_key: 'key-f' } },
    ]);
  });

  it('api keys CRUD', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return [];
    });
    await mod.getApiKeys();
    await mod.createApiKey({ name: 'a', allowed_databases: [], allowed_routes: [] });
    await mod.updateApiKey('a', { description: 'x' });
    await mod.deleteApiKey('a');
    expect(calls).toEqual([
      { method: 'get', url: '/admin/api-keys' },
      { method: 'post', url: '/admin/api-keys' },
      { method: 'put', url: '/admin/api-keys/a' },
      { method: 'delete', url: '/admin/api-keys/a' },
    ]);
  });

  it('my API key lifecycle endpoints', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return {};
    });

    await mod.getMyApiKey();
    await mod.createMyApiKey();
    await mod.regenerateMyApiKey();
    await mod.renewMyApiKey();
    await mod.deleteMyApiKey();

    expect(calls).toEqual([
      { method: 'get', url: '/admin/api-keys/me' },
      { method: 'post', url: '/admin/api-keys/me' },
      { method: 'post', url: '/admin/api-keys/me/regenerate' },
      { method: 'post', url: '/admin/api-keys/me/renew' },
      { method: 'delete', url: '/admin/api-keys/me' },
    ]);
  });

  it('roles & users CRUD', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return {};
    });
    await mod.getAuthRoles();
    await mod.getCurrentUser();
    await mod.getRoles();
    await mod.getRole(1);
    await mod.createRole({ name: 'a', permissions: [] });
    await mod.updateRole(1, { permissions: [] });
    await mod.deleteRole(1);
    await mod.getAllPermissions();

    await mod.getUsers({ search: 'x', max: 5 });
    await mod.createKeycloakUser({ username: 'u', password: 'p', role: 'r' });
    await mod.changeUserRole('u1', 'admin');
    await mod.resetUserPassword('u1', { password: 'p', temporary: true });
    await mod.toggleUserEnabled('u1', false);
    await mod.deleteKeycloakUser('u1');

    expect(calls.map((c) => c.url)).toEqual([
      '/auth/roles',
      '/auth/me',
      '/admin/roles',
      '/admin/roles/1',
      '/admin/roles',
      '/admin/roles/1',
      '/admin/roles/1',
      '/admin/permissions',
      '/admin/users',
      '/admin/users',
      '/admin/users/u1/role',
      '/admin/users/u1/reset-password',
      '/admin/users/u1/enabled',
      '/admin/users/u1',
    ]);
  });

  it('alerts settings, channels, recipients & resource owners', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string; data?: unknown }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url, data: c.data ? JSON.parse(c.data) : undefined });
      return [];
    });

    await mod.getAlertSettings();
    await mod.updateAlertSettings({ admin_emails: ['ops@example.com'], mail_channel_id: 1 });

    await mod.getAlertChannels();
    await mod.createAlertChannel({ name: 'n', webhook_url: 'http://x', payload_template: '{}' });
    await mod.updateAlertChannel(2, { name: 'n2' });
    await mod.deleteAlertChannel(2);
    await mod.testAlertChannel(2);

    await mod.testRecipientDelivery(1, ['admin@example.com', 'oncall@example.com']);

    await mod.getAlertResourceOwners();
    await mod.setAlertResourceOwner('db', 'orders-db', { emails: ['owner@example.com'] });
    await mod.deleteAlertResourceOwner('db', 'orders-db');

    await mod.getAlertHistory({ alert_type: 'triggered' });
    await mod.getAlertStatus();

    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      'get /admin/alerts/settings',
      'put /admin/alerts/settings',
      'get /admin/alerts/channels',
      'post /admin/alerts/channels',
      'put /admin/alerts/channels/2',
      'delete /admin/alerts/channels/2',
      'post /admin/alerts/channels/2/test',
      'post /admin/alerts/settings/recipients/test',
      'get /admin/alerts/resource-owners',
      'put /admin/alerts/resource-owners/db/orders-db',
      'delete /admin/alerts/resource-owners/db/orders-db',
      'get /admin/alerts/history',
      'get /admin/alerts/status',
    ]);

    // settings PUT carries admin_emails
    expect(calls[1].data).toEqual({ admin_emails: ['ops@example.com'], mail_channel_id: 1 });
    // recipients test body shape
    expect(calls[7].data).toEqual({
      mail_channel_id: 1,
      emails: ['admin@example.com', 'oncall@example.com'],
    });
    // resource-owner PUT body carries emails only
    expect(calls[9].data).toEqual({ emails: ['owner@example.com'] });
  });

  it('s3 connections CRUD + browse', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return [];
    });
    await mod.getS3Connections();
    await mod.createS3Connection({ alias: 'a', region: 'r', use_ssl: true } as S3ConnectionConfig);
    await mod.updateS3Connection('a', { region: 'r2' });
    await mod.deleteS3Connection('a');
    await mod.testS3Connection('a');

    await mod.getS3Buckets('a');
    await mod.getS3Objects('a', { bucket: 'b', prefix: 'p/' });
    await mod.getS3ObjectMetadata('a', { bucket: 'b', key: 'k' });
    await mod.getS3PresignedUrl('a', { bucket: 'b', key: 'k', expires_in: 60 });

    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      'get /admin/s3/connections',
      'post /admin/s3/connections',
      'put /admin/s3/connections/a',
      'delete /admin/s3/connections/a',
      'post /admin/s3/connections/a/test',
      'get /s3/a/buckets',
      'get /s3/a/objects',
      'get /s3/a/objects/metadata',
      'get /s3/a/objects/presigned-url',
    ]);
  });

  it('server and external-service registries use their expected endpoints', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string; params?: unknown }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url, params: c.params });
      return {};
    });

    await mod.getServers();
    await mod.createServer({ name: 'edge' });
    await mod.updateServer(3, { enabled: false });
    await mod.deleteServer(3);
    await mod.testServer(3);
    await mod.getServerMetrics(3, { duration: '6h', step: '120s' });
    await mod.getExternalServices();
    await mod.createExternalService({ name: 'orders' });
    await mod.updateExternalService(8, { scheme: 'https' });
    await mod.deleteExternalService(8);
    await mod.testExternalService(8);

    expect(calls).toEqual([
      { method: 'get', url: '/admin/servers', params: undefined },
      { method: 'post', url: '/admin/servers', params: undefined },
      { method: 'put', url: '/admin/servers/3', params: undefined },
      { method: 'delete', url: '/admin/servers/3', params: undefined },
      { method: 'post', url: '/admin/servers/3/test', params: undefined },
      { method: 'get', url: '/admin/servers/3/metrics', params: { duration: '6h', step: '120s' } },
      { method: 'get', url: '/admin/servers/external-services', params: undefined },
      { method: 'post', url: '/admin/servers/external-services', params: undefined },
      { method: 'put', url: '/admin/servers/external-services/8', params: undefined },
      { method: 'delete', url: '/admin/servers/external-services/8', params: undefined },
      { method: 'post', url: '/admin/servers/external-services/8/test', params: undefined },
    ]);
  });

  it('external metrics helpers forward time, service, and bucket parameters', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ url?: string; params?: Record<string, unknown> }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ url: c.url, params: c.params });
      return {};
    });

    const selection = { kind: 'preset', value: '6h' } as const;
    await mod.getExternalSummary(selection, 'orders');
    await mod.getExternalRequests(selection, 'orders');
    await mod.getExternalRequestsTotal(selection, 'orders', 'hour');
    await mod.getExternalStatusCodes(selection, 'orders');
    await mod.getExternalLatency(selection, 'orders');
    await mod.getExternalServicesComparison(selection);
    await mod.getExternalServicesComparisonSeries(selection, 'week');
    await mod.getExternalHandlersComparison(selection, 'orders');

    expect(calls).toEqual([
      { url: '/admin/external/metrics/summary', params: { range: '6h', service: 'orders' } },
      { url: '/admin/external/metrics/requests', params: { range: '6h', service: 'orders' } },
      { url: '/admin/external/metrics/requests-total', params: { range: '6h', bucket: 'hour', service: 'orders' } },
      { url: '/admin/external/metrics/status-codes', params: { range: '6h', service: 'orders' } },
      { url: '/admin/external/metrics/latency', params: { range: '6h', service: 'orders' } },
      { url: '/admin/external/metrics/services-comparison', params: { range: '6h' } },
      { url: '/admin/external/metrics/services-comparison-series', params: { range: '6h', bucket: 'week' } },
      { url: '/admin/external/metrics/handlers-comparison', params: { range: '6h', service: 'orders' } },
    ]);
  });

  it('NAS connection and browse endpoints preserve path parameters', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string; params?: unknown }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url, params: c.params });
      return {};
    });

    await mod.getNasConnections();
    await mod.createNasConnection({ alias: 'n', base_path: '/mnt', read_only: true, show_hidden: false, follow_symlinks: false });
    await mod.updateNasConnection('n', { show_hidden: true });
    await mod.deleteNasConnection('n');
    await mod.testNasConnection('n');
    await mod.getNasEntries('n', { path: '/docs', offset: 2, limit: 20, q: 'report' });
    await mod.getNasEntryMetadata('n', '/docs/report.pdf');

    expect(calls).toEqual([
      { method: 'get', url: '/admin/nas/connections', params: undefined },
      { method: 'post', url: '/admin/nas/connections', params: undefined },
      { method: 'put', url: '/admin/nas/connections/n', params: undefined },
      { method: 'delete', url: '/admin/nas/connections/n', params: undefined },
      { method: 'post', url: '/admin/nas/connections/n/test', params: undefined },
      { method: 'get', url: '/nas/n/entries', params: { path: '/docs', offset: 2, limit: 20, q: 'report' } },
      { method: 'get', url: '/nas/n/metadata', params: { path: '/docs/report.pdf' } },
    ]);
  });

  it('downloadS3Object parses UTF-8 filename header', async () => {
    const mod = await importClient(keycloak);
    mod.default.defaults.adapter = vi.fn(async (config) => {
      return {
        data: new Blob(['hello']),
        status: 200,
        statusText: 'OK',
        headers: { 'content-disposition': "attachment; filename*=UTF-8''my%20file.txt" },
        config,
      };
    }) as unknown as AxiosAdapter;
    const res = await mod.downloadS3Object('a', { bucket: 'b', key: 'x' });
    expect(res.filename).toBe('my file.txt');
    expect(res.blob).toBeInstanceOf(Blob);
  });

  it('downloadS3Object falls back to plain filename then key suffix', async () => {
    const mod = await importClient(keycloak);
    mod.default.defaults.adapter = vi.fn(async (config) => {
      return {
        data: new Blob([]),
        status: 200,
        statusText: 'OK',
        headers: { 'content-disposition': 'attachment; filename="hello.csv"' },
        config,
      };
    }) as unknown as AxiosAdapter;
    let res = await mod.downloadS3Object('a', { bucket: 'b', key: 'x' });
    expect(res.filename).toBe('hello.csv');

    mod.default.defaults.adapter = vi.fn(async (config) => {
      return {
        data: new Blob([]),
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      };
    }) as unknown as AxiosAdapter;
    res = await mod.downloadS3Object('a', { bucket: 'b', key: 'logs/2026-04-30.txt' });
    expect(res.filename).toBe('2026-04-30.txt');

    mod.default.defaults.adapter = vi.fn(async (config) => {
      return {
        data: new Blob([]),
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      };
    }) as unknown as AxiosAdapter;
    res = await mod.downloadS3Object('a', { bucket: 'b', key: '' });
    expect(res.filename).toBe('download');
  });

  it('downloadS3Object reports progress', async () => {
    const mod = await importClient(keycloak);
    let captured: InternalAxiosRequestConfig | undefined;
    mod.default.defaults.adapter = vi.fn(async (config) => {
      captured = config;
      return {
        data: new Blob(['x']),
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      };
    }) as unknown as AxiosAdapter;
    const onProgress = vi.fn();
    await mod.downloadS3Object('a', { bucket: 'b', key: 'x' }, onProgress);
    captured!.onDownloadProgress!({ loaded: 5, total: 10 } as never);
    expect(onProgress).toHaveBeenCalledWith(5, 10);

    captured!.onDownloadProgress!({ loaded: 5, total: 0 } as never);
    expect(onProgress).toHaveBeenCalledTimes(1);
  });

  it('downloadNasEntry parses filenames, falls back to path, and reports progress', async () => {
    const mod = await importClient(keycloak);
    let captured: InternalAxiosRequestConfig | undefined;
    mod.default.defaults.adapter = vi.fn(async (config) => {
      captured = config;
      return {
        data: new Blob(['nas']),
        status: 200,
        statusText: 'OK',
        headers: { 'content-disposition': "attachment; filename*=UTF-8''weekly%20report.pdf" },
        config,
      };
    }) as unknown as AxiosAdapter;
    const onProgress = vi.fn();
    let result = await mod.downloadNasEntry('n', '/docs/report.pdf', onProgress);
    expect(result.filename).toBe('weekly report.pdf');
    captured!.onDownloadProgress!({ loaded: 4, total: 8 } as never);
    expect(onProgress).toHaveBeenCalledWith(4, 8);
    captured!.onDownloadProgress!({ loaded: 4, total: 0 } as never);
    expect(onProgress).toHaveBeenCalledTimes(1);

    mod.default.defaults.adapter = vi.fn(async (config) => ({
      data: new Blob([]),
      status: 200,
      statusText: 'OK',
      headers: { 'content-disposition': 'attachment; filename="plain.txt"' },
      config,
    })) as unknown as AxiosAdapter;
    result = await mod.downloadNasEntry('n', '/docs/ignored.txt');
    expect(result.filename).toBe('plain.txt');

    mod.default.defaults.adapter = vi.fn(async (config) => ({
      data: new Blob([]), status: 200, statusText: 'OK', headers: {}, config,
    })) as unknown as AxiosAdapter;
    expect((await mod.downloadNasEntry('n', '/docs/fallback.csv')).filename).toBe('fallback.csv');
    expect((await mod.downloadNasEntry('n', '')).filename).toBe('download');
  });
});

describe('api client interceptor edge cases', () => {
  it('rejects requests until authentication initialization is ready', async () => {
    const keycloak = makeKeycloakMock();
    vi.resetModules();
    vi.doMock('../keycloak', () => ({ default: keycloak }));
    const mod = await import('../api/client');

    await expect(mod.default.get('/anything')).rejects.toThrow('Authentication is not ready');
    expect(keycloak.updateToken).not.toHaveBeenCalled();
  });

  it('rejects with "Authentication is required" when not authenticated', async () => {
    const keycloak = makeKeycloakMock();
    keycloak.authenticated = false;
    const mod = await importClient(keycloak);
    await expect(mod.default.get('/anything')).rejects.toThrow('Authentication is required');
  });

  it('logs out when 401 retry refresh fails', async () => {
    const keycloak = makeKeycloakMock();
    const mod = await importClient(keycloak);
    keycloak.updateToken
      .mockResolvedValueOnce(true) // initial request refresh
      .mockRejectedValueOnce(new Error('refresh failed')); // retry refresh

    let calls = 0;
    mod.default.defaults.adapter = vi.fn(async (config) => {
      calls += 1;
      return Promise.reject({
        config,
        response: { status: 401, data: {}, headers: {}, config, statusText: '401' },
        isAxiosError: true,
      });
    }) as unknown as AxiosAdapter;

    await expect(mod.default.get('/admin/users')).rejects.toMatchObject({
      response: { status: 401 },
    });
    expect(calls).toBe(1);
    expect(keycloak.logout).toHaveBeenCalledTimes(1);
  });

  it('triggers login redirect when initial refresh fails', async () => {
    const keycloak = makeKeycloakMock();
    keycloak.updateToken.mockRejectedValue(new Error('expired'));
    const mod = await importClient(keycloak);

    await expect(mod.default.get('/anything')).rejects.toThrow('Session expired');
    expect(keycloak.login).toHaveBeenCalledTimes(1);
  });

  it('login redirect runs only once on repeated session-expired requests', async () => {
    const keycloak = makeKeycloakMock();
    keycloak.updateToken.mockRejectedValue(new Error('expired'));
    const mod = await importClient(keycloak);

    await expect(mod.default.get('/a')).rejects.toThrow('Session expired');
    await expect(mod.default.get('/b')).rejects.toThrow('Session expired');
    expect(keycloak.login).toHaveBeenCalledTimes(1);
  });

  it('logout runs only once across multiple 401 retries', async () => {
    const keycloak = makeKeycloakMock();
    const mod = await importClient(keycloak);
    keycloak.updateToken
      .mockResolvedValue(true);
    // First request: initial refresh OK, then 401, retry refresh rejects → logout
    keycloak.updateToken
      .mockResolvedValueOnce(true)
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(true)
      .mockRejectedValueOnce(new Error('boom'));

    let calls = 0;
    mod.default.defaults.adapter = vi.fn(async (config) => {
      calls += 1;
      return Promise.reject({
        config,
        response: { status: 401, data: {}, headers: {}, config, statusText: '401' },
        isAxiosError: true,
      });
    }) as unknown as AxiosAdapter;

    await expect(mod.default.get('/a')).rejects.toMatchObject({ response: { status: 401 } });
    await expect(mod.default.get('/b')).rejects.toMatchObject({ response: { status: 401 } });
    expect(keycloak.logout).toHaveBeenCalledTimes(1);
    expect(calls).toBe(2);
  });

  it('setApiAuthReady(true) resets login/logout once-flags', async () => {
    const keycloak = makeKeycloakMock();
    keycloak.updateToken.mockRejectedValue(new Error('expired'));
    const mod = await importClient(keycloak);

    await expect(mod.default.get('/a')).rejects.toThrow('Session expired');
    expect(keycloak.login).toHaveBeenCalledTimes(1);

    mod.setApiAuthReady(true); // reset flags
    await expect(mod.default.get('/b')).rejects.toThrow('Session expired');
    expect(keycloak.login).toHaveBeenCalledTimes(2);
  });

  it('does not attach Authorization header when token is empty', async () => {
    const keycloak = makeKeycloakMock();
    keycloak.token = '';
    const mod = await importClient(keycloak);
    let captured: AxiosRequestHeaders | undefined;
    mod.default.defaults.adapter = makeAdapter((c) => {
      captured = c.headers;
      return {};
    });
    await mod.default.get('/anything');
    expect(captured?.Authorization).toBeUndefined();
  });

  it('successful 200 response passes straight through', async () => {
    const keycloak = makeKeycloakMock();
    const mod = await importClient(keycloak);
    mod.default.defaults.adapter = makeAdapter(() => ({ ok: true }));
    const res = await mod.default.get('/admin/x');
    expect(res.data).toEqual({ ok: true });
  });

  it('refreshes and retries one 401 response with the latest token', async () => {
    const keycloak = makeKeycloakMock();
    const mod = await importClient(keycloak);
    keycloak.updateToken
      .mockResolvedValueOnce(true)
      .mockImplementationOnce(async () => {
        keycloak.token = 'token-2';
        return true;
      });

    let calls = 0;
    const authorizationHeaders: unknown[] = [];
    mod.default.defaults.adapter = vi.fn(async (config) => {
      calls += 1;
      authorizationHeaders.push(config.headers.Authorization);
      if (calls === 1) {
        return Promise.reject({
          config,
          response: { status: 401, data: {}, headers: {}, config, statusText: '401' },
          isAxiosError: true,
        });
      }
      return { data: { ok: true }, status: 200, statusText: 'OK', headers: {}, config };
    }) as unknown as AxiosAdapter;

    await expect(mod.default.get('/admin/retry')).resolves.toMatchObject({ data: { ok: true } });
    expect(calls).toBe(2);
    expect(authorizationHeaders).toEqual(['Bearer token-1', 'Bearer token-2']);
    expect(keycloak.updateToken).toHaveBeenNthCalledWith(1, 5);
    expect(keycloak.updateToken).toHaveBeenNthCalledWith(2, -1);
  });
});
