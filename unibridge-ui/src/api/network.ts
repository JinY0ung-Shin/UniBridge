// Network map / firewall API (unibridge-service app/routers/network.py).
// Kept apart from client.ts so the feature's calls and types stay in one place.
import client from './client';

export type NetworkProtocol = 'tcp' | 'udp';
export type OpeningStatus = 'open' | 'closed';
/** manual = written by a person, agent = written with an API key (unverified
 *  until a person confirms), import = created from a monitored server,
 *  auto = discovered from UniBridge's own config (port usages only, read-only). */
export type RecordSource = 'manual' | 'agent' | 'import' | 'auto';

interface Provenance {
  source: RecordSource | string;
  created_by: string | null;
  verified: boolean;
  verified_at: string | null;
  verified_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface Region {
  id: number;
  name: string;
  description: string;
  cidrs: string[];
  host_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface RegionInput {
  name?: string;
  description?: string;
  cidrs?: string[];
}

export interface NetworkHost extends Provenance {
  id: number;
  name: string;
  ip: string;
  region_id: number | null;
  region_name: string | null;
  description: string;
  owner: string;
  monitored_host_id: number | null;
  monitored_host_name: string | null;
}

export interface NetworkHostInput {
  name?: string;
  ip?: string;
  /** null on update = unassign (the region is then left unset). */
  region_id?: number | null;
  description?: string;
  owner?: string;
  monitored_host_id?: number | null;
}

export interface FirewallOpening extends Provenance {
  id: number;
  src_host_id: number | null;
  src_host_name: string | null;
  src_host_ip: string | null;
  src_cidr: string | null;
  src_region_id: number | null;
  src_region_name: string | null;
  dst_host_id: number;
  dst_host_name: string;
  dst_host_ip: string;
  dst_region_id: number | null;
  dst_region_name: string | null;
  port_from: number;
  port_to: number;
  protocol: NetworkProtocol;
  status: OpeningStatus;
  ticket_no: string;
  requester: string;
  purpose: string;
  /** Whether any recorded or discovered service listens inside the port range. */
  in_use: boolean;
  /** The latest probe through this opening (source matches, port in range). */
  last_probe: ProbeSummary | null;
}

export interface FirewallOpeningInput {
  src_host_id?: number;
  src_cidr?: string;
  dst_host_id?: number;
  port_from?: number;
  port_to?: number | null;
  protocol?: NetworkProtocol;
  status?: OpeningStatus;
  ticket_no?: string;
  requester?: string;
  purpose?: string;
}

export interface PortUsage extends Provenance {
  /** null for a discovered (auto) usage, which is not stored. */
  id: number | null;
  host_id: number;
  host_name: string;
  host_ip: string;
  port: number;
  protocol: NetworkProtocol;
  service_name: string;
  description: string;
  owner: string;
  /** auto only: db | s3 | node_exporter | dcgm | metrics | gateway_upstream */
  auto_kind: string | null;
  /** auto only: whether the platform host can reach it (null = unknown). */
  reachable_from_platform: boolean | null;
}

export interface PortUsageInput {
  host_id?: number;
  port?: number;
  protocol?: NetworkProtocol;
  service_name?: string;
  description?: string;
  owner?: string;
}

export interface NetworkHostDetail extends NetworkHost {
  inbound: FirewallOpening[];
  outbound: FirewallOpening[];
  usages: PortUsage[];
}

export interface RegionLink {
  /** null = a CIDR source outside every region, or an unassigned host */
  src_region_id: number | null;
  dst_region_id: number | null;
  openings: number;
  open_openings: number;
  unverified: number;
  ports: string[];
}

export interface NetworkOverview {
  regions: Region[];
  links: RegionLink[];
  host_count: number;
  unassigned_hosts: number;
  opening_count: number;
  unverified_count: number;
  unused_openings: number;
  unreachable_usages: number;
  /** Probes that disagree with the records (see Probe.mismatch). */
  probe_mismatches: number;
  platform_host_id: number | null;
  auto_sources_failed: string[];
}

export interface NetworkSettings {
  platform_host_id: number | null;
}

export interface AccessEndpoint {
  input: string;
  host_id: number | null;
  host_name: string | null;
  ip: string | null;
  region_id: number | null;
  region_name: string | null;
}

export interface AccessCheckResult {
  status: 'open' | 'blocked' | 'unknown';
  reason: string;
  message: string;
  src: AccessEndpoint;
  dst: AccessEndpoint;
  port: number;
  protocol: NetworkProtocol;
  openings: FirewallOpening[];
  usages: PortUsage[];
  request_draft: string | null;
  /** The latest measured attempt on exactly this path (both ends registered). */
  probe: Probe | null;
}

export interface MonitoredImportResult {
  created: string[];
  linked: string[];
  skipped: Array<{ name: string; reason: string }>;
}

/* ── Probes: measured connection attempts reported from the source host ── */

/** open: connected · refused: reached, nothing listens (the network let it
 *  through) · timeout: no answer (what a dropping firewall looks like) · error */
export type ProbeResult = 'open' | 'refused' | 'timeout' | 'error';
/** recorded_open_but_blocked: the records say open, the probe timed out ·
 *  passes_without_opening: the records say blocked, the probe got through. */
export type ProbeMismatch = 'recorded_open_but_blocked' | 'passes_without_opening';

export interface Probe {
  id: number;
  src_host_id: number;
  src_host_name: string;
  src_host_ip: string;
  src_region_name: string | null;
  dst_host_id: number;
  dst_host_name: string;
  dst_host_ip: string;
  dst_region_name: string | null;
  port: number;
  protocol: NetworkProtocol;
  result: ProbeResult | string;
  latency_ms: number | null;
  error: string;
  reported_by: string | null;
  observed_at: string;
  /** What the records say about this path now. */
  expected: 'open' | 'blocked' | 'unknown' | string;
  /** Same codes as the access check's reason. */
  expected_reason: string;
  mismatch: ProbeMismatch | string | null;
}

export interface ProbeSummary {
  id: number;
  src_host_name: string;
  port: number;
  result: ProbeResult | string;
  observed_at: string;
  mismatch: ProbeMismatch | string | null;
}

export interface ProbeFilters {
  /** Source or destination host. */
  host_id?: number;
  mismatch?: boolean;
  result?: ProbeResult;
}

/* ── CSV import (people only) ── */

export type NetworkImportKind = 'hosts' | 'openings' | 'port_usages';

export interface NetworkImportRequest {
  kind: NetworkImportKind;
  csv: string;
  dry_run: boolean;
}

export interface NetworkImportRow {
  line: number;
  action: 'create' | 'skip' | 'error' | string;
  key: string;
  message: string;
}

export interface NetworkImportResult {
  kind: NetworkImportKind;
  dry_run: boolean;
  rows: NetworkImportRow[];
  created: number;
  skipped: number;
  errors: number;
}

export interface HostFilters {
  region_id?: number;
  q?: string;
  unverified?: boolean;
}

export interface OpeningFilters {
  host_id?: number;
  region_id?: number;
  status?: OpeningStatus;
  port?: number;
  unverified?: boolean;
}

export interface UsageFilters {
  host_id?: number;
  port?: number;
  source?: 'manual' | 'agent' | 'auto';
  unverified?: boolean;
}

/** Drop unset filters so the query string only carries what was chosen. */
function params<T extends object>(filters: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(filters).filter(([, v]) => v !== undefined && v !== '' && v !== false),
  ) as Partial<T>;
}

export async function getNetworkOverview(): Promise<NetworkOverview> {
  const { data } = await client.get('/network/overview');
  return data;
}

export async function getNetworkSettings(): Promise<NetworkSettings> {
  const { data } = await client.get('/network/settings');
  return data;
}

export async function updateNetworkSettings(body: NetworkSettings): Promise<NetworkSettings> {
  const { data } = await client.put('/network/settings', body);
  return data;
}

export async function checkNetworkAccess(query: {
  src: string;
  dst: string;
  port: number;
  protocol: NetworkProtocol;
}): Promise<AccessCheckResult> {
  const { data } = await client.get('/network/check', { params: query });
  return data;
}

export async function getRegions(): Promise<Region[]> {
  const { data } = await client.get('/network/regions');
  return data;
}

export async function createRegion(body: RegionInput): Promise<Region> {
  const { data } = await client.post('/network/regions', body);
  return data;
}

export async function updateRegion(id: number, body: RegionInput): Promise<Region> {
  const { data } = await client.put(`/network/regions/${id}`, body);
  return data;
}

export async function deleteRegion(id: number): Promise<void> {
  await client.delete(`/network/regions/${id}`);
}

export async function getNetworkHosts(filters: HostFilters = {}): Promise<NetworkHost[]> {
  const { data } = await client.get('/network/hosts', { params: params(filters) });
  return data;
}

export async function getNetworkHost(id: number): Promise<NetworkHostDetail> {
  const { data } = await client.get(`/network/hosts/${id}`);
  return data;
}

export async function createNetworkHost(body: NetworkHostInput): Promise<NetworkHost> {
  const { data } = await client.post('/network/hosts', body);
  return data;
}

export async function updateNetworkHost(id: number, body: NetworkHostInput): Promise<NetworkHost> {
  const { data } = await client.put(`/network/hosts/${id}`, body);
  return data;
}

export async function deleteNetworkHost(id: number): Promise<void> {
  await client.delete(`/network/hosts/${id}`);
}

export async function verifyNetworkHost(id: number): Promise<NetworkHost> {
  const { data } = await client.post(`/network/hosts/${id}/verify`);
  return data;
}

export async function importMonitoredHosts(): Promise<MonitoredImportResult> {
  const { data } = await client.post('/network/hosts/import-monitored');
  return data;
}

export async function getFirewallOpenings(filters: OpeningFilters = {}): Promise<FirewallOpening[]> {
  const { data } = await client.get('/network/openings', { params: params(filters) });
  return data;
}

export async function createFirewallOpening(body: FirewallOpeningInput): Promise<FirewallOpening> {
  const { data } = await client.post('/network/openings', body);
  return data;
}

export async function updateFirewallOpening(id: number, body: FirewallOpeningInput): Promise<FirewallOpening> {
  const { data } = await client.put(`/network/openings/${id}`, body);
  return data;
}

export async function deleteFirewallOpening(id: number): Promise<void> {
  await client.delete(`/network/openings/${id}`);
}

export async function verifyFirewallOpening(id: number): Promise<FirewallOpening> {
  const { data } = await client.post(`/network/openings/${id}/verify`);
  return data;
}

export async function getPortUsages(filters: UsageFilters = {}): Promise<PortUsage[]> {
  const { data } = await client.get('/network/port-usages', { params: params(filters) });
  return data;
}

export async function createPortUsage(body: PortUsageInput): Promise<PortUsage> {
  const { data } = await client.post('/network/port-usages', body);
  return data;
}

export async function updatePortUsage(id: number, body: PortUsageInput): Promise<PortUsage> {
  const { data } = await client.put(`/network/port-usages/${id}`, body);
  return data;
}

export async function deletePortUsage(id: number): Promise<void> {
  await client.delete(`/network/port-usages/${id}`);
}

export async function verifyPortUsage(id: number): Promise<PortUsage> {
  const { data } = await client.post(`/network/port-usages/${id}/verify`);
  return data;
}

export async function getNetworkProbes(filters: ProbeFilters = {}): Promise<Probe[]> {
  const { data } = await client.get('/network/probes', { params: params(filters) });
  return data;
}

export async function importNetworkCsv(body: NetworkImportRequest): Promise<NetworkImportResult> {
  const { data } = await client.post('/network/import', body);
  return data;
}
