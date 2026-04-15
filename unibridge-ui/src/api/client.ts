import axios from 'axios';
import keycloak from '../keycloak';

const API_BASE = '/_api';

const client = axios.create({
  baseURL: API_BASE,
});

// Attach Keycloak token, refreshing if needed
client.interceptors.request.use(async (config) => {
  if (keycloak.authenticated) {
    try {
      await keycloak.updateToken(5);
    } catch {
      keycloak.login();
      return Promise.reject(new Error('Session expired'));
    }
    if (keycloak.token) {
      config.headers.Authorization = `Bearer ${keycloak.token}`;
      return config;
    }
  }
  return config;
});

// Handle 401 responses: trigger Keycloak logout
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && keycloak.authenticated) {
      keycloak.logout({ redirectUri: window.location.origin });
    }
    return Promise.reject(error);
  },
);

/* ── Types ── */

export interface DatabaseConfig {
  alias: string;
  db_type: 'postgres' | 'mssql' | 'clickhouse';
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  protocol?: 'http' | 'https' | null;
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

export interface QuerySettings {
  rate_limit_per_minute: number;
  max_concurrent_queries: number;
  blocked_sql_keywords: string[];
}

export interface QuerySettingsUpdate {
  rate_limit_per_minute?: number;
  max_concurrent_queries?: number;
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

/* ── Admin: Query Settings ── */

export async function getQuerySettings(): Promise<QuerySettings> {
  const { data } = await client.get('/admin/query/settings');
  return data;
}

export async function updateQuerySettings(body: QuerySettingsUpdate): Promise<QuerySettings> {
  const { data } = await client.put('/admin/query/settings', body);
  return data;
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
  plugins?: Record<string, unknown>;
  system?: boolean;
}

export interface GatewayUpstream {
  id: string;
  name?: string;
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

export async function getMetricsSummary(range = '1h', route?: string): Promise<MetricsSummary> {
  const { data } = await client.get('/admin/gateway/metrics/summary', { params: { range, route } });
  return data;
}

export async function getMetricsRequests(range = '1h', route?: string): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests', { params: { range, route } });
  return data;
}

export async function getMetricsStatusCodes(range = '1h', route?: string): Promise<StatusCodeData[]> {
  const { data } = await client.get('/admin/gateway/metrics/status-codes', { params: { range, route } });
  return data;
}

export async function getMetricsLatency(range = '1h', route?: string): Promise<LatencyData> {
  const { data } = await client.get('/admin/gateway/metrics/latency', { params: { range, route } });
  return data;
}

export async function getMetricsTopRoutes(range = '1h'): Promise<TopRoute[]> {
  const { data } = await client.get('/admin/gateway/metrics/top-routes', { params: { range } });
  return data;
}

export async function getMetricsRequestsTotal(range = '1h', route?: string): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests-total', { params: { range, route } });
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
  cost: number;
}

export interface LlmKeyUsage {
  api_key: string;
  tokens: number;
  requests: number;
}

export interface LlmErrorPoint {
  timestamp: number;
  success: number;
  error: number;
}

export async function getLlmSummary(range = '1h'): Promise<LlmSummary> {
  const { data } = await client.get('/admin/gateway/metrics/llm/summary', { params: { range } });
  return data;
}

export async function getLlmTokens(range = '1h'): Promise<LlmTokenSeries> {
  const { data } = await client.get('/admin/gateway/metrics/llm/tokens', { params: { range } });
  return data;
}

export async function getLlmByModel(range = '1h'): Promise<LlmModelUsage[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/by-model', { params: { range } });
  return data;
}

export async function getLlmTopKeys(range = '1h'): Promise<LlmKeyUsage[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/top-keys', { params: { range } });
  return data;
}

export async function getLlmErrors(range = '1h'): Promise<LlmErrorPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/errors', { params: { range } });
  return data;
}

export async function getLlmRequestsTotal(range = '1h'): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/llm/requests-total', { params: { range } });
  return data;
}

/* ── API Keys ── */

export interface ApiKey {
  name: string;
  description: string;
  api_key: string | null;
  key_created: boolean;
  allowed_databases: string[];
  allowed_routes: string[];
  created_at: string | null;
}

export interface ApiKeyCreate {
  name: string;
  description?: string;
  api_key?: string;
  allowed_databases: string[];
  allowed_routes: string[];
}

export interface ApiKeyUpdate {
  description?: string;
  api_key?: string;
  allowed_databases?: string[];
  allowed_routes?: string[];
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
  headers: Record<string, string> | null;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface AlertChannelCreate {
  name: string;
  webhook_url: string;
  payload_template: string;
  headers?: Record<string, string>;
  enabled?: boolean;
}

export interface RuleChannelMapping {
  channel_id: number;
  recipients: string[];
}

export interface RuleChannelDetail {
  channel_id: number;
  channel_name: string;
  recipients: string[];
}

export interface AlertRule {
  id: number;
  name: string;
  type: 'db_health' | 'upstream_health' | 'error_rate';
  target: string;
  threshold: number | null;
  enabled: boolean;
  channels: RuleChannelDetail[];
  created_at?: string;
  updated_at?: string;
}

export interface AlertRuleCreate {
  name: string;
  type: 'db_health' | 'upstream_health' | 'error_rate';
  target: string;
  threshold?: number;
  enabled?: boolean;
  channels: RuleChannelMapping[];
}

export interface AlertHistoryEntry {
  id: number;
  rule_id: number | null;
  channel_id: number | null;
  alert_type: 'triggered' | 'resolved';
  target: string;
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
}

// Channels
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

// Rules
export async function getAlertRules(): Promise<AlertRule[]> {
  const { data } = await client.get('/admin/alerts/rules');
  return data;
}

export async function createAlertRule(body: AlertRuleCreate): Promise<AlertRule> {
  const { data } = await client.post('/admin/alerts/rules', body);
  return data;
}

export async function updateAlertRule(id: number, body: Partial<AlertRuleCreate>): Promise<AlertRule> {
  const { data } = await client.put(`/admin/alerts/rules/${id}`, body);
  return data;
}

export async function deleteAlertRule(id: number): Promise<void> {
  await client.delete(`/admin/alerts/rules/${id}`);
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
  const { data } = await client.get(`/admin/s3/${alias}/buckets`);
  return data;
}

export async function getS3Objects(
  alias: string,
  params: { bucket: string; prefix?: string; delimiter?: string; max_keys?: number; continuation_token?: string },
): Promise<S3ListObjectsResponse> {
  const { data } = await client.get(`/admin/s3/${alias}/objects`, { params });
  return data;
}

export async function getS3ObjectMetadata(
  alias: string,
  params: { bucket: string; key: string },
): Promise<S3ObjectMetadata> {
  const { data } = await client.get(`/admin/s3/${alias}/objects/metadata`, { params });
  return data;
}

export async function getS3PresignedUrl(
  alias: string,
  params: { bucket: string; key: string; expires_in?: number },
): Promise<{ url: string; expires_in: string }> {
  const { data } = await client.get(`/admin/s3/${alias}/objects/presigned-url`, { params });
  return data;
}

export default client;

