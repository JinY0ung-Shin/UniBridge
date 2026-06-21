import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios';
import keycloak from '../keycloak';
import { type TimeSelection, type Bucket, DEFAULT_SELECTION, timeParams, bucketParam } from '../utils/timeRange';
export type { TimeSelection, Bucket } from '../utils/timeRange';

const API_BASE = '/_api';

const client = axios.create({
  baseURL: API_BASE,
});

interface AuthRequestConfig extends InternalAxiosRequestConfig {
  _retry?: boolean;
  _skipAuthRefresh?: boolean;
}

let apiAuthReady = false;
let tokenRefreshPromise: Promise<boolean> | null = null;
let loginRedirectStarted = false;
let logoutStarted = false;

export function setApiAuthReady(ready: boolean): void {
  apiAuthReady = ready;
  if (ready) {
    loginRedirectStarted = false;
    logoutStarted = false;
  }
}

function attachAuthorizationHeader(config: AuthRequestConfig): AuthRequestConfig {
  if (keycloak.token) {
    config.headers.Authorization = `Bearer ${keycloak.token}`;
  }
  return config;
}

function refreshTokenOnce(minValidity: number): Promise<boolean> {
  if (!tokenRefreshPromise) {
    tokenRefreshPromise = keycloak.updateToken(minValidity).finally(() => {
      tokenRefreshPromise = null;
    });
  }
  return tokenRefreshPromise;
}

function loginOnce(): void {
  if (!loginRedirectStarted) {
    loginRedirectStarted = true;
    void keycloak.login();
  }
}

function logoutOnce(): void {
  if (!logoutStarted) {
    logoutStarted = true;
    keycloak.logout({ redirectUri: window.location.origin });
  }
}

// Attach Keycloak token, refreshing if needed.
client.interceptors.request.use(async (config) => {
  const authConfig = config as AuthRequestConfig;
  if (!apiAuthReady) {
    return Promise.reject(new Error('Authentication is not ready'));
  }
  if (!keycloak.authenticated) {
    return Promise.reject(new Error('Authentication is required'));
  }

  try {
    if (!authConfig._skipAuthRefresh) {
      await refreshTokenOnce(5);
    }
  } catch {
    loginOnce();
    return Promise.reject(new Error('Session expired'));
  }

  return attachAuthorizationHeader(authConfig);
});

// Handle 401 responses: force one token refresh, then retry the original request.
client.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as AuthRequestConfig | undefined;
    if (error.response?.status === 401 && keycloak.authenticated && original && !original._retry) {
      original._retry = true;
      try {
        await refreshTokenOnce(-1);
        original._skipAuthRefresh = true;
        attachAuthorizationHeader(original);
        return client(original);
      } catch {
        logoutOnce();
      }
    }
    return Promise.reject(error);
  },
);

/* ── Types ── */

export type Neo4jProtocol = 'bolt' | 'bolt+s' | 'bolt+ssc' | 'neo4j' | 'neo4j+s' | 'neo4j+ssc';

export interface DatabaseConfig {
  alias: string;
  db_type: 'postgres' | 'mssql' | 'clickhouse' | 'neo4j' | 'graphdb';
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  protocol?: 'http' | 'https' | Neo4jProtocol | null;
  secure?: boolean | null;
  pool_size: number;
  max_overflow: number;
  query_timeout: number;
  status?: string;
}

export interface DatabaseHealth {
  status: string;
  pool_active?: number;
  pool_idle?: number;
}

export interface HealthResponse {
  status: string;
  databases: Record<string, DatabaseHealth>;
}

export interface Permission {
  id?: number;
  role: string;
  db_alias: string;
  allow_select: boolean;
  allow_insert: boolean;
  allow_update: boolean;
  allow_delete: boolean;
  allowed_tables?: string[] | null;
}

export interface AuditLog {
  id: number;
  timestamp: string;
  user: string;
  database_alias: string;
  sql: string;
  params?: string | null;
  row_count: number;
  elapsed_ms: number;
  status: 'success' | 'error';
  error_message?: string;
}

export interface AuditLogParams {
  database?: string;
  user?: string;
  from_date?: string;
  to_date?: string;
  limit?: number;
  offset?: number;
}

export interface AdminAuditLog {
  id: number;
  timestamp: string | null;
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string;
  summary: string | null;
  before: string | null;
  after: string | null;
  status: string;
  error_message: string | null;
}

export interface AdminAuditLogParams {
  actor?: string;
  resource_type?: string;
  action?: string;
  from_date?: string;
  to_date?: string;
  limit?: number;
  offset?: number;
}

export interface QuerySettings {
  rate_limit_per_minute: number;
  max_concurrent_queries: number;
  default_row_limit: number;
  blocked_sql_keywords: string[];
}

export interface QuerySettingsUpdate {
  rate_limit_per_minute?: number;
  max_concurrent_queries?: number;
  default_row_limit?: number;
  blocked_sql_keywords?: string[];
}

export interface QueryRequest {
  database: string;
  sql: string;
  params?: Record<string, unknown>;
}

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  elapsed_ms: number;
  truncated: boolean;
  graph?: string | null; // populated for SPARQL CONSTRUCT/DESCRIBE responses
}

export interface QueryTemplate {
  id: number;
  path: string;
  name: string;
  description: string;
  database: string;
  sql: string;
  default_limit?: number | null;
  timeout?: number | null;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface QueryTemplateCreate {
  path: string;
  name: string;
  description?: string;
  database: string;
  sql: string;
  default_limit?: number | null;
  timeout?: number | null;
  enabled?: boolean;
}

export interface QueryTemplateUpdate {
  name?: string;
  description?: string;
  database?: string;
  sql?: string;
  default_limit?: number | null;
  timeout?: number | null;
  enabled?: boolean;
}

export interface QueryTemplateExecuteRequest {
  params?: Record<string, unknown>;
  limit?: number;
  timeout?: number;
}

export interface QueryHistoryResponse {
  items: AuditLog[];
  total: number;
}

export interface QueryHistoryParams {
  database_alias?: string;
  limit?: number;
  offset?: number;
}

export interface SavedQuery {
  id: number;
  name: string;
  database_alias: string | null;
  sql_text: string;
  description: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SavedQueryCreate {
  name: string;
  database_alias?: string | null;
  sql_text: string;
  description?: string;
}

export interface SavedQueryUpdate {
  name?: string;
  database_alias?: string | null;
  sql_text?: string;
  description?: string;
}

function encodeTemplatePath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/');
}

/* ── Query endpoints ── */

export async function getDatabases(): Promise<DatabaseConfig[]> {
  const { data } = await client.get('/query/databases');
  return data;
}

export async function executeQuery(req: QueryRequest): Promise<QueryResult> {
  const { data } = await client.post('/query/execute', req);
  return data;
}

export async function executeQueryTemplate(path: string, req: QueryTemplateExecuteRequest = {}): Promise<QueryResult> {
  const { data } = await client.post(`/query/templates/${encodeTemplatePath(path)}`, req);
  return data;
}

/* ── Query: My history & saved queries ── */

export async function getQueryHistory(params: QueryHistoryParams = {}): Promise<QueryHistoryResponse> {
  const { data } = await client.get('/query/history', { params });
  return data;
}

export async function getSavedQueries(): Promise<SavedQuery[]> {
  const { data } = await client.get('/query/saved');
  return data;
}

export async function createSavedQuery(body: SavedQueryCreate): Promise<SavedQuery> {
  const { data } = await client.post('/query/saved', body);
  return data;
}

export async function updateSavedQuery(id: number, body: SavedQueryUpdate): Promise<SavedQuery> {
  const { data } = await client.put(`/query/saved/${id}`, body);
  return data;
}

export async function deleteSavedQuery(id: number): Promise<void> {
  await client.delete(`/query/saved/${id}`);
}

/* ── Health ── */

export async function getHealth(): Promise<HealthResponse> {
  const { data } = await client.get('/health/databases');
  return data;
}

/* ── Admin: Databases ── */

export async function getAdminDatabases(): Promise<DatabaseConfig[]> {
  const { data } = await client.get('/admin/query/databases');
  return data;
}

export async function createDatabase(db: DatabaseConfig): Promise<DatabaseConfig> {
  const { data } = await client.post('/admin/query/databases', db);
  return data;
}

export async function updateDatabase(alias: string, db: Partial<DatabaseConfig>): Promise<DatabaseConfig> {
  const { data } = await client.put(`/admin/query/databases/${alias}`, db);
  return data;
}

export async function deleteDatabase(alias: string): Promise<void> {
  await client.delete(`/admin/query/databases/${alias}`);
}

export async function testDatabase(alias: string): Promise<{ status: string; message: string }> {
  const { data } = await client.post(`/admin/query/databases/${alias}/test`);
  return data;
}

/* ── Admin: Permissions ── */

export async function getPermissions(): Promise<Permission[]> {
  const { data } = await client.get('/admin/query/permissions');
  return data;
}

export async function updatePermission(perm: Permission): Promise<Permission> {
  const { data } = await client.put('/admin/query/permissions', perm);
  return data;
}

export async function deletePermission(id: number): Promise<void> {
  await client.delete(`/admin/query/permissions/${id}`);
}

export async function getDbTables(alias: string): Promise<string[]> {
  const { data } = await client.get(`/admin/query/databases/${alias}/tables`);
  return data;
}

/* ── Admin: Audit Logs ── */

export async function getAuditLogs(params: AuditLogParams): Promise<AuditLog[]> {
  const { data } = await client.get('/admin/query/audit-logs', { params });
  return data;
}

/* ── Admin: Admin Audit Logs (config change audit trail) ── */

export async function getAdminAuditLogs(params: AdminAuditLogParams): Promise<AdminAuditLog[]> {
  const { data } = await client.get('/admin/audit-logs', { params });
  return data;
}

/* ── Admin: Query Settings ── */

export async function getQuerySettings(): Promise<QuerySettings> {
  const { data } = await client.get('/admin/query/settings');
  return data;
}

export async function updateQuerySettings(body: QuerySettingsUpdate): Promise<QuerySettings> {
  const { data } = await client.put('/admin/query/settings', body);
  return data;
}

/* ── Admin: Query Templates ── */

export async function getQueryTemplates(): Promise<QueryTemplate[]> {
  const { data } = await client.get('/admin/query/templates');
  return data;
}

export async function createQueryTemplate(body: QueryTemplateCreate): Promise<QueryTemplate> {
  const { data } = await client.post('/admin/query/templates', body);
  return data;
}

export async function updateQueryTemplate(path: string, body: QueryTemplateUpdate): Promise<QueryTemplate> {
  const { data } = await client.put(`/admin/query/templates/${encodeTemplatePath(path)}`, body);
  return data;
}

export async function deleteQueryTemplate(path: string): Promise<void> {
  await client.delete(`/admin/query/templates/${encodeTemplatePath(path)}`);
}

/* ── Auth ── */

export async function getToken(username: string, role: string): Promise<{ access_token: string }> {
  const { data } = await client.post('/auth/token', { username, role });
  return data;
}

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
  require_auth?: boolean;
  strip_prefix?: boolean;
  service_key?: GatewayServiceKey | null;
  service_keys?: GatewayServiceKey[];
  plugins?: Record<string, unknown>;
  system?: boolean;
}

export interface GatewayUpstream {
  id: string;
  name?: string;
  scheme?: 'http' | 'https';
  pass_host?: 'pass' | 'node' | 'rewrite';
  upstream_host?: string;
  type: string;
  nodes: Record<string, number>;
  system?: boolean;
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

export interface RouteTestResult {
  reachable: boolean;
  status_code: number | null;
  response_time_ms: number;
  body: unknown;
  node: string;
  error?: string;
}

export async function testGatewayRoute(id: string): Promise<RouteTestResult> {
  const { data } = await client.post(`/admin/gateway/routes/${id}/test`);
  return data;
}

export async function getGatewayRouteCurl(id: string): Promise<{ curl: string }> {
  const { data } = await client.get(`/admin/gateway/routes/${id}/curl`);
  return data;
}

export async function getGatewayOpenApiSpec(): Promise<Record<string, unknown>> {
  const { data } = await client.get('/admin/gateway/openapi.json');
  return data;
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

/* ── Gateway: Metrics ── */

export interface MetricsSummary {
  total_requests: number;
  error_rate: number;
  avg_latency_ms: number;
}

export interface TimeSeriesPoint {
  timestamp: number;
  value: number;
}

export interface StatusCodeData {
  code: string;
  count: number;
}

export interface TopRoute {
  route: string;
  requests: number;
}

export interface LatencyData {
  p50: TimeSeriesPoint[];
  p95: TimeSeriesPoint[];
  p99: TimeSeriesPoint[];
}

export async function getMetricsSummary(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<MetricsSummary> {
  const { data } = await client.get('/admin/gateway/metrics/summary', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsRequests(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsStatusCodes(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<StatusCodeData[]> {
  const { data } = await client.get('/admin/gateway/metrics/status-codes', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsLatency(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
): Promise<LatencyData> {
  const { data } = await client.get('/admin/gateway/metrics/latency', {
    params: { ...timeParams(sel), route, consumer },
  });
  return data;
}

export async function getMetricsTopRoutes(sel: TimeSelection = DEFAULT_SELECTION): Promise<TopRoute[]> {
  const { data } = await client.get('/admin/gateway/metrics/top-routes', { params: { ...timeParams(sel) } });
  return data;
}

export async function getMetricsRequestsTotal(
  sel: TimeSelection = DEFAULT_SELECTION,
  route?: string,
  consumer?: string,
  bucket: Bucket = 'auto',
): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests-total', {
    params: { ...timeParams(sel), ...bucketParam(bucket), route, consumer },
  });
  return data;
}

export type RouteComparisonRow = {
  route: string;
  name?: string | null;
  requests: number;
  share: number;
  error_rate: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
};

export type RouteComparisonResponse = {
  total_requests: number;
  routes: RouteComparisonRow[];
};

export async function getMetricsRoutesComparison(
  sel: TimeSelection = DEFAULT_SELECTION,
  consumer?: string,
): Promise<RouteComparisonResponse> {
  const { data } = await client.get('/admin/gateway/metrics/routes-comparison', {
    params: { ...timeParams(sel), consumer },
  });
  return data;
}

export type ConsumerComparisonRow = {
  consumer: string;
  requests: number;
  share: number;
  error_rate: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
};

export type ConsumerComparisonResponse = {
  total_requests: number;
  consumers: ConsumerComparisonRow[];
};

export async function getMetricsConsumersComparison(
  sel: TimeSelection = DEFAULT_SELECTION,
): Promise<ConsumerComparisonResponse> {
  const { data } = await client.get('/admin/gateway/metrics/consumers-comparison', {
    params: { ...timeParams(sel) },
  });
  return data;
}

/* ── LLM Metrics ── */

export interface LlmSummary {
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  estimated_cost: number;
  total_requests: number;
  avg_latency_ms: number;
}

export interface LlmTokenSeries {
  prompt: TimeSeriesPoint[];
  completion: TimeSeriesPoint[];
}

export interface LlmModelUsage {
  model: string;
  tokens: number;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  requests: number;
}

export interface LlmKeyUsage {
  api_key: string;
  input_tokens: number;
  output_tokens: number;
  tokens: number;
  requests: number;
}

export interface LlmErrorPoint {
  timestamp: number;
  success: number;
  error: number;
}

export async function getLlmSummary(sel: TimeSelection = DEFAULT_SELECTION): Promise<LlmSummary> {
  const { data } = await client.get('/admin/gateway/metrics/llm/summary', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmTokens(
  sel: TimeSelection = DEFAULT_SELECTION,
  bucket: Bucket = 'auto',
): Promise<LlmTokenSeries> {
  const { data } = await client.get('/admin/gateway/metrics/llm/tokens', {
    params: { ...timeParams(sel), ...bucketParam(bucket) },
  });
  return data;
}

export async function getLlmByModel(sel: TimeSelection = DEFAULT_SELECTION): Promise<LlmModelUsage[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/by-model', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmTopKeys(sel: TimeSelection = DEFAULT_SELECTION): Promise<LlmKeyUsage[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/top-keys', { params: { ...timeParams(sel) } });
  return data;
}

export async function getLlmErrors(
  sel: TimeSelection = DEFAULT_SELECTION,
  bucket: Bucket = 'auto',
): Promise<LlmErrorPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/errors', {
    params: { ...timeParams(sel), ...bucketParam(bucket) },
  });
  return data;
}

export async function getLlmStatusCodes(
  sel: TimeSelection = DEFAULT_SELECTION,
): Promise<StatusCodeData[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/status-codes', {
    params: { ...timeParams(sel) },
  });
  return data;
}

export async function getLlmRequestsTotal(
  sel: TimeSelection = DEFAULT_SELECTION,
  bucket: Bucket = 'auto',
): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/requests-total', {
    params: { ...timeParams(sel), ...bucketParam(bucket) },
  });
  return data;
}

/* ── Bucketed per-dimension breakdowns ── */

export interface BucketedSeries {
  key: string;
  total: number;
  points: number[];
}

export interface BucketedBreakdown {
  buckets: number[];
  series: BucketedSeries[];
  unit: 'tokens' | 'requests';
}

export async function getLlmByModelSeries(
  sel: TimeSelection = DEFAULT_SELECTION,
  bucket: Bucket = 'auto',
): Promise<BucketedBreakdown> {
  const { data } = await client.get('/admin/gateway/metrics/llm/by-model-series', {
    params: { ...timeParams(sel), ...bucketParam(bucket) },
  });
  return data;
}

export async function getLlmTopKeysSeries(
  sel: TimeSelection = DEFAULT_SELECTION,
  bucket: Bucket = 'auto',
): Promise<BucketedBreakdown> {
  const { data } = await client.get('/admin/gateway/metrics/llm/top-keys-series', {
    params: { ...timeParams(sel), ...bucketParam(bucket) },
  });
  return data;
}

export async function getRoutesComparisonSeries(
  sel: TimeSelection = DEFAULT_SELECTION,
  consumer?: string,
  bucket: Bucket = 'auto',
): Promise<BucketedBreakdown> {
  const { data } = await client.get('/admin/gateway/metrics/routes-comparison-series', {
    params: { ...timeParams(sel), ...bucketParam(bucket), consumer },
  });
  return data;
}

export async function getConsumersComparisonSeries(
  sel: TimeSelection = DEFAULT_SELECTION,
  bucket: Bucket = 'auto',
): Promise<BucketedBreakdown> {
  const { data } = await client.get('/admin/gateway/metrics/consumers-comparison-series', {
    params: { ...timeParams(sel), ...bucketParam(bucket) },
  });
  return data;
}

/* ── API Keys ── */

export interface ApiKey {
  name: string;
  description: string;
  api_key: string | null;
  key_created: boolean;
  is_master?: boolean;
  allowed_databases: string[];
  allowed_routes: string[];
  rate_limit_per_minute: number | null;
  allow_insert?: boolean;
  allow_update?: boolean;
  allow_delete?: boolean;
  allowed_tables?: string[] | null;
  owner: string | null;
  expires_at?: string | null;
  created_at: string | null;
}

export interface ApiKeyCreate {
  name: string;
  description?: string;
  api_key?: string;
  is_master?: boolean;
  allowed_databases: string[];
  allowed_routes: string[];
  rate_limit_per_minute?: number | null;
  allow_insert?: boolean;
  allow_update?: boolean;
  allow_delete?: boolean;
  allowed_tables?: string[] | null;
  owner?: string | null;
}

export interface ApiKeyUpdate {
  description?: string;
  api_key?: string;
  is_master?: boolean;
  allowed_databases?: string[];
  allowed_routes?: string[];
  rate_limit_per_minute?: number | null;
  allow_insert?: boolean;
  allow_update?: boolean;
  allow_delete?: boolean;
  allowed_tables?: string[] | null;
  owner?: string | null;
}

export async function getApiKeys(): Promise<ApiKey[]> {
  const { data } = await client.get('/admin/api-keys');
  return data;
}

export async function createApiKey(body: ApiKeyCreate): Promise<ApiKey> {
  const { data } = await client.post('/admin/api-keys', body);
  return data;
}

export async function updateApiKey(name: string, body: ApiKeyUpdate): Promise<ApiKey> {
  const { data } = await client.put(`/admin/api-keys/${name}`, body);
  return data;
}

export async function deleteApiKey(name: string): Promise<void> {
  await client.delete(`/admin/api-keys/${name}`);
}

/* ── API Keys: Self-service ── */

export async function getMyApiKey(): Promise<ApiKey | null> {
  const { data } = await client.get('/admin/api-keys/me');
  return data;
}

export async function createMyApiKey(): Promise<ApiKey> {
  const { data } = await client.post('/admin/api-keys/me');
  return data;
}

export async function regenerateMyApiKey(): Promise<ApiKey> {
  const { data } = await client.post('/admin/api-keys/me/regenerate');
  return data;
}

export async function renewMyApiKey(): Promise<ApiKey> {
  const { data } = await client.post('/admin/api-keys/me/renew');
  return data;
}

export async function deleteMyApiKey(): Promise<void> {
  await client.delete('/admin/api-keys/me');
}

/* ── Roles (RBAC) ── */

export interface RoleInfo {
  id: number;
  name: string;
  description: string;
  is_system: boolean;
  permissions: string[];
}

export interface UserInfo {
  username: string;
  role: string;
  permissions: string[];
}

export async function getAuthRoles(): Promise<string[]> {
  const { data } = await client.get('/auth/roles');
  return data;
}

export async function getCurrentUser(): Promise<UserInfo> {
  const { data } = await client.get('/auth/me');
  return data;
}

export async function getRoles(): Promise<RoleInfo[]> {
  const { data } = await client.get('/admin/roles');
  return data;
}

export async function getRole(id: number): Promise<RoleInfo> {
  const { data } = await client.get(`/admin/roles/${id}`);
  return data;
}

export async function createRole(body: { name: string; description?: string; permissions: string[] }): Promise<RoleInfo> {
  const { data } = await client.post('/admin/roles', body);
  return data;
}

export async function updateRole(id: number, body: { description?: string; permissions?: string[] }): Promise<RoleInfo> {
  const { data } = await client.put(`/admin/roles/${id}`, body);
  return data;
}

export async function deleteRole(id: number): Promise<void> {
  await client.delete(`/admin/roles/${id}`);
}

export async function getAllPermissions(): Promise<string[]> {
  const { data } = await client.get('/admin/permissions');
  return data;
}

/* ── Admin: Users (Keycloak) ── */

export interface KeycloakUser {
  id: string;
  username: string;
  email: string | null;
  enabled: boolean;
  role: string | null;
  createdTimestamp: number | null;
}

export interface KeycloakUserList {
  users: KeycloakUser[];
  total: number;
}

export interface CreateUserBody {
  username: string;
  email?: string;
  password: string;
  role: string;
}

export interface ResetPasswordBody {
  password: string;
  temporary: boolean;
}

export async function getUsers(params?: { search?: string; first?: number; max?: number }): Promise<KeycloakUserList> {
  const { data } = await client.get('/admin/users', { params });
  return data;
}

export async function createKeycloakUser(body: CreateUserBody): Promise<KeycloakUser> {
  const { data } = await client.post('/admin/users', body);
  return data;
}

export async function changeUserRole(userId: string, role: string): Promise<KeycloakUser> {
  const { data } = await client.put(`/admin/users/${userId}/role`, { role });
  return data;
}

export async function resetUserPassword(userId: string, body: ResetPasswordBody): Promise<void> {
  await client.put(`/admin/users/${userId}/reset-password`, body);
}

export async function toggleUserEnabled(userId: string, enabled: boolean): Promise<KeycloakUser> {
  const { data } = await client.put(`/admin/users/${userId}/enabled`, { enabled });
  return data;
}

export async function deleteKeycloakUser(userId: string): Promise<void> {
  await client.delete(`/admin/users/${userId}`);
}

// ── Alerts ──────────────────────────────────────────────────────────────────

export interface AlertChannel {
  id: number;
  name: string;
  webhook_url: string;
  payload_template: string;
  recipient_item_template?: string | null;
  headers: Record<string, string> | null;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface AlertChannelCreate {
  name: string;
  webhook_url: string;
  payload_template: string;
  recipient_item_template?: string | null;
  headers?: Record<string, string>;
  enabled?: boolean;
}

export interface AlertSettings {
  mail_channel_id: number | null;
  admin_emails: string[];
  route_error_threshold_pct: number;
  route_error_min_requests: number;
  check_interval_seconds: number;
  trigger_after_failures: number;
  server_disk_warn_pct?: number;
  server_disk_crit_pct?: number;
  server_cpu_warn_pct?: number;
  server_mem_warn_pct?: number;
  server_disk_forecast_hours?: number;
  repeat_alert_after_cycles?: number;
  updated_at?: string | null;
}

export interface AlertResourceOwner {
  resource_type: string;
  resource_id: string;
  display_name: string;
  emails: string[];
  alerts_enabled: boolean;
}

export interface AlertHistoryEntry {
  id: number;
  channel_id: number | null;
  alert_type: 'triggered' | 'resolved';
  target: string;
  severity?: string | null;
  message: string;
  recipients: string[] | null;
  sent_at: string;
  success: boolean | null;
  error_detail: string | null;
}

export interface AlertStatus {
  target: string;
  type: string;
  status: 'ok' | 'alert';
  since: string | null;
  severity?: string | null;
}

// Channels
export async function getAlertSettings(): Promise<AlertSettings> {
  const { data } = await client.get('/admin/alerts/settings');
  return data;
}

export async function updateAlertSettings(body: Partial<AlertSettings>): Promise<AlertSettings> {
  const { data } = await client.put('/admin/alerts/settings', body);
  return data;
}

export async function testRecipientDelivery(
  mailChannelId: number,
  emails: string[],
): Promise<{ success: boolean; error: string | null }> {
  const { data } = await client.post('/admin/alerts/settings/recipients/test', {
    mail_channel_id: mailChannelId,
    emails,
  });
  return data;
}

export async function getAlertChannels(): Promise<AlertChannel[]> {
  const { data } = await client.get('/admin/alerts/channels');
  return data;
}

export async function createAlertChannel(body: AlertChannelCreate): Promise<AlertChannel> {
  const { data } = await client.post('/admin/alerts/channels', body);
  return data;
}

export async function updateAlertChannel(id: number, body: Partial<AlertChannelCreate>): Promise<AlertChannel> {
  const { data } = await client.put(`/admin/alerts/channels/${id}`, body);
  return data;
}

export async function deleteAlertChannel(id: number): Promise<void> {
  await client.delete(`/admin/alerts/channels/${id}`);
}

export async function testAlertChannel(id: number): Promise<{ success: boolean; error: string | null }> {
  const { data } = await client.post(`/admin/alerts/channels/${id}/test`);
  return data;
}

export async function getAlertResourceOwners(): Promise<AlertResourceOwner[]> {
  const { data } = await client.get('/admin/alerts/resource-owners');
  return data;
}

export async function setAlertResourceOwner(
  resourceType: string,
  resourceId: string,
  body: { emails?: string[]; alerts_enabled?: boolean },
): Promise<AlertResourceOwner> {
  const type = encodeURIComponent(resourceType);
  const id = encodeURIComponent(resourceId);
  const { data } = await client.put(`/admin/alerts/resource-owners/${type}/${id}`, body);
  return data;
}

export async function deleteAlertResourceOwner(resourceType: string, resourceId: string): Promise<void> {
  const type = encodeURIComponent(resourceType);
  const id = encodeURIComponent(resourceId);
  await client.delete(`/admin/alerts/resource-owners/${type}/${id}`);
}

// History & Status
export async function getAlertHistory(params?: {
  alert_type?: string;
  target?: string;
  from_date?: string;
  to_date?: string;
  limit?: number;
  offset?: number;
}): Promise<AlertHistoryEntry[]> {
  const { data } = await client.get('/admin/alerts/history', { params });
  return data;
}

export async function getAlertStatus(): Promise<AlertStatus[]> {
  const { data } = await client.get('/admin/alerts/status');
  return data;
}

/* ── Monitored servers (hosts) ── */

export interface MonitoredServer {
  id: number;
  name: string;
  address: string;
  enabled: boolean;
  description: string;
  labels: Record<string, string> | null;
  disk_warn_pct: number | null;
  disk_crit_pct: number | null;
  cpu_warn_pct: number | null;
  mem_warn_pct: number | null;
  status?: 'up' | 'down' | 'unknown' | 'disabled' | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface MonitoredServerInput {
  name?: string;
  address?: string;
  enabled?: boolean;
  description?: string;
  labels?: Record<string, string> | null;
  disk_warn_pct?: number | null;
  disk_crit_pct?: number | null;
  cpu_warn_pct?: number | null;
  mem_warn_pct?: number | null;
}

export interface ServerMetricSeries {
  metric: 'cpu' | 'mem' | 'disk';
  points: Array<{ t: number; v: number | null }>;
}

export async function getServers(): Promise<MonitoredServer[]> {
  const { data } = await client.get('/admin/servers');
  return data;
}

export async function createServer(body: MonitoredServerInput): Promise<MonitoredServer> {
  const { data } = await client.post('/admin/servers', body);
  return data;
}

export async function updateServer(id: number, body: MonitoredServerInput): Promise<MonitoredServer> {
  const { data } = await client.put(`/admin/servers/${id}`, body);
  return data;
}

export async function deleteServer(id: number): Promise<void> {
  await client.delete(`/admin/servers/${id}`);
}

export async function testServer(id: number): Promise<{ status: string; detail: string | null }> {
  const { data } = await client.post(`/admin/servers/${id}/test`);
  return data;
}

export async function getServerMetrics(
  id: number,
  params: { duration?: string; step?: string } = {},
): Promise<ServerMetricSeries[]> {
  const { data } = await client.get(`/admin/servers/${id}/metrics`, { params });
  return data;
}

/* ── S3 Types ── */

export interface S3ConnectionConfig {
  alias: string;
  endpoint_url?: string | null;
  region: string;
  access_key_id?: string;
  access_key_id_masked?: string;
  secret_access_key?: string;
  default_bucket?: string | null;
  use_ssl: boolean;
  status?: string;
}

export interface S3Bucket {
  name: string;
  creation_date: string | null;
}

export interface S3Folder {
  prefix: string;
}

export interface S3Object {
  key: string;
  size: number;
  last_modified: string | null;
  storage_class?: string;
}

export interface S3ListObjectsResponse {
  folders: S3Folder[];
  objects: S3Object[];
  is_truncated: boolean;
  next_continuation_token?: string | null;
  key_count: number;
}

export interface S3ObjectMetadata {
  key: string;
  size: number;
  content_type: string;
  last_modified: string | null;
  etag: string;
  storage_class?: string;
  metadata: Record<string, string>;
}

/* ── S3: Connections ── */

export async function getS3Connections(): Promise<S3ConnectionConfig[]> {
  const { data } = await client.get('/admin/s3/connections');
  return data;
}

export async function createS3Connection(body: S3ConnectionConfig): Promise<S3ConnectionConfig> {
  const { data } = await client.post('/admin/s3/connections', body);
  return data;
}

export async function updateS3Connection(alias: string, body: Partial<S3ConnectionConfig>): Promise<S3ConnectionConfig> {
  const { data } = await client.put(`/admin/s3/connections/${alias}`, body);
  return data;
}

export async function deleteS3Connection(alias: string): Promise<void> {
  await client.delete(`/admin/s3/connections/${alias}`);
}

export async function testS3Connection(alias: string): Promise<{ status: string; message: string }> {
  const { data } = await client.post(`/admin/s3/connections/${alias}/test`);
  return data;
}

/* ── S3: Browse ── */

export async function getS3Buckets(alias: string): Promise<S3Bucket[]> {
  const { data } = await client.get(`/s3/${alias}/buckets`);
  return data;
}

export async function getS3Objects(
  alias: string,
  params: { bucket: string; prefix?: string; delimiter?: string; max_keys?: number; continuation_token?: string },
): Promise<S3ListObjectsResponse> {
  const { data } = await client.get(`/s3/${alias}/objects`, { params });
  return data;
}

export async function getS3ObjectMetadata(
  alias: string,
  params: { bucket: string; key: string },
): Promise<S3ObjectMetadata> {
  const { data } = await client.get(`/s3/${alias}/objects/metadata`, { params });
  return data;
}

export async function downloadS3Object(
  alias: string,
  params: { bucket: string; key: string },
  onProgress?: (loaded: number, total: number) => void,
): Promise<{ blob: Blob; filename: string }> {
  const response = await client.get(`/s3/${alias}/objects/download`, {
    params,
    responseType: 'blob',
    onDownloadProgress: (e) => {
      if (onProgress && e.total) onProgress(e.loaded, e.total);
    },
  });
  const disposition = response.headers['content-disposition'] || '';
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;\s]+)/i);
  const plainMatch = disposition.match(/filename="([^"]+)"/i);
  const raw = utf8Match?.[1] ?? plainMatch?.[1];
  const filename = raw
    ? decodeURIComponent(raw)
    : params.key.split('/').pop() || 'download';
  return { blob: response.data as Blob, filename };
}

export async function getS3PresignedUrl(
  alias: string,
  params: { bucket: string; key: string; expires_in?: number },
): Promise<{ url: string; expires_in: number }> {
  const { data } = await client.get(`/s3/${alias}/objects/presigned-url`, { params });
  return data;
}

/* ── NAS Types ── */

export interface NasConnectionConfig {
  alias: string;
  base_path: string;
  read_only: boolean;
  max_download_bytes?: number | null;
  show_hidden: boolean;
  follow_symlinks: boolean;
  status?: string;
}

export interface NasEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  modified_time: string | null;
}

export interface NasListResponse {
  path: string;
  folders: NasEntry[];
  files: NasEntry[];
  total_count: number;
  has_more: boolean;
  next_cursor: string | null;
  /** True when the directory scan hit the entry cap: more entries exist than
   * can be listed, so the caller should narrow with a search term. */
  truncated?: boolean;
}

export interface NasEntryMetadata {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  modified_time: string | null;
  content_type: string | null;
}

/* ── NAS: Connections ── */

export async function getNasConnections(): Promise<NasConnectionConfig[]> {
  const { data } = await client.get('/admin/nas/connections');
  return data;
}

export async function createNasConnection(body: NasConnectionConfig): Promise<NasConnectionConfig> {
  const { data } = await client.post('/admin/nas/connections', body);
  return data;
}

export async function updateNasConnection(alias: string, body: Partial<NasConnectionConfig>): Promise<NasConnectionConfig> {
  const { data } = await client.put(`/admin/nas/connections/${alias}`, body);
  return data;
}

export async function deleteNasConnection(alias: string): Promise<void> {
  await client.delete(`/admin/nas/connections/${alias}`);
}

export async function testNasConnection(alias: string): Promise<{ status: string; message: string }> {
  const { data } = await client.post(`/admin/nas/connections/${alias}/test`);
  return data;
}

/* ── NAS: Browse ── */

export async function getNasEntries(
  alias: string,
  params: { path?: string; offset?: number; limit?: number; q?: string },
): Promise<NasListResponse> {
  const { data } = await client.get(`/nas/${alias}/entries`, { params });
  return data;
}

export async function getNasEntryMetadata(
  alias: string,
  path: string,
): Promise<NasEntryMetadata> {
  const { data } = await client.get(`/nas/${alias}/metadata`, { params: { path } });
  return data;
}

export async function downloadNasEntry(
  alias: string,
  path: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<{ blob: Blob; filename: string }> {
  const response = await client.get(`/nas/${alias}/download`, {
    params: { path },
    responseType: 'blob',
    onDownloadProgress: (e) => {
      if (onProgress && e.total) onProgress(e.loaded, e.total);
    },
  });
  const disposition = response.headers['content-disposition'] || '';
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;\s]+)/i);
  const plainMatch = disposition.match(/filename="([^"]+)"/i);
  const raw = utf8Match?.[1] ?? plainMatch?.[1];
  const filename = raw
    ? decodeURIComponent(raw)
    : path.split('/').pop() || 'download';
  return { blob: response.data as Blob, filename };
}

export default client;
