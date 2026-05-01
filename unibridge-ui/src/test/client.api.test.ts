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
    expect(calls).toEqual([
      { method: 'get', url: '/admin/gateway/routes' },
      { method: 'get', url: '/admin/gateway/routes/r1' },
      { method: 'put', url: '/admin/gateway/routes/r1' },
      { method: 'delete', url: '/admin/gateway/routes/r1' },
      { method: 'post', url: '/admin/gateway/routes/r1/test' },
      { method: 'get', url: '/admin/gateway/routes/r1/curl' },
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
    await mod.getMetricsSummary('5m', 'r1');
    await mod.getMetricsRequests('1h');
    await mod.getMetricsStatusCodes('1h', 'r1');
    await mod.getMetricsLatency('15m');
    await mod.getMetricsTopRoutes('1h');
    await mod.getMetricsRequestsTotal('1h');
    await mod.getMetricsRoutesComparison('1h');

    expect(calls[0]).toEqual({ url: '/admin/gateway/metrics/summary', params: { range: '5m', route: 'r1' } });
    expect(calls[1].params).toEqual({ range: '1h', route: undefined });
    expect(calls[6].url).toBe('/admin/gateway/metrics/routes-comparison');
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

  it('alerts channels & rules', async () => {
    const mod = await importClient(keycloak);
    const calls: Array<{ method?: string; url?: string }> = [];
    mod.default.defaults.adapter = makeAdapter((c) => {
      calls.push({ method: c.method, url: c.url });
      return [];
    });
    await mod.getAlertChannels();
    await mod.createAlertChannel({ name: 'n', webhook_url: 'http://x', payload_template: '{}' });
    await mod.updateAlertChannel(2, { name: 'n2' });
    await mod.deleteAlertChannel(2);
    await mod.testAlertChannel(2);

    await mod.getAlertRules();
    await mod.createAlertRule({ name: 'r', type: 'db_health', target: 't', channels: [] });
    await mod.updateAlertRule(3, { enabled: false });
    await mod.deleteAlertRule(3);
    await mod.testAlertRule(3);

    await mod.getAlertHistory({ alert_type: 'triggered' });
    await mod.getAlertStatus();

    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      'get /admin/alerts/channels',
      'post /admin/alerts/channels',
      'put /admin/alerts/channels/2',
      'delete /admin/alerts/channels/2',
      'post /admin/alerts/channels/2/test',
      'get /admin/alerts/rules',
      'post /admin/alerts/rules',
      'put /admin/alerts/rules/3',
      'delete /admin/alerts/rules/3',
      'post /admin/alerts/rules/3/test',
      'get /admin/alerts/history',
      'get /admin/alerts/status',
    ]);
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
});

describe('api client interceptor edge cases', () => {
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
});
