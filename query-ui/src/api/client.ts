import axios from 'axios';

const API_BASE = '/api';

const client = axios.create({
  baseURL: API_BASE,
});

// Attach auth token if stored
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle 401 responses: clear stale token and reload to trigger login
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const token = localStorage.getItem('auth_token');
      if (token) {
        localStorage.removeItem('auth_token');
        window.location.reload();
      } else {
        console.warn('Received 401 but no token stored. User needs to authenticate.');
      }
    }
    return Promise.reject(error);
  },
);

/* ── Types ── */

export interface DatabaseConfig {
  alias: string;
  db_type: 'postgres' | 'mssql';
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
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

/* ── Admin: Audit Logs ── */

export async function getAuditLogs(params: AuditLogParams): Promise<AuditLog[]> {
  const { data } = await client.get('/admin/query/audit-logs', { params });
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
  service_key?: GatewayServiceKey | null;
  plugins?: Record<string, unknown>;
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

/* ── Gateway: Consumers ── */

export interface GatewayConsumer {
  username: string;
  api_key?: string | null;
  key_created?: boolean;
  plugins?: Record<string, unknown>;
}

export async function getGatewayConsumers(): Promise<GatewayListResponse<GatewayConsumer>> {
  const { data } = await client.get('/admin/gateway/consumers');
  return data;
}

export async function getGatewayConsumer(username: string): Promise<GatewayConsumer> {
  const { data } = await client.get(`/admin/gateway/consumers/${username}`);
  return data;
}

export async function saveGatewayConsumer(username: string, body: Record<string, unknown>): Promise<GatewayConsumer> {
  const { data } = await client.put(`/admin/gateway/consumers/${username}`, body);
  return data;
}

export async function deleteGatewayConsumer(username: string): Promise<void> {
  await client.delete(`/admin/gateway/consumers/${username}`);
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

export async function getMetricsSummary(range = '1h'): Promise<MetricsSummary> {
  const { data } = await client.get('/admin/gateway/metrics/summary', { params: { range } });
  return data;
}

export async function getMetricsRequests(range = '1h'): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/admin/gateway/metrics/requests', { params: { range } });
  return data;
}

export async function getMetricsStatusCodes(range = '1h'): Promise<StatusCodeData[]> {
  const { data } = await client.get('/admin/gateway/metrics/status-codes', { params: { range } });
  return data;
}

export async function getMetricsLatency(range = '1h'): Promise<LatencyData> {
  const { data } = await client.get('/admin/gateway/metrics/latency', { params: { range } });
  return data;
}

export async function getMetricsTopRoutes(range = '1h'): Promise<TopRoute[]> {
  const { data } = await client.get('/admin/gateway/metrics/top-routes', { params: { range } });
  return data;
}

export default client;
