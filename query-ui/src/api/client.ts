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
  params?: Record<string, unknown>;
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

export default client;
