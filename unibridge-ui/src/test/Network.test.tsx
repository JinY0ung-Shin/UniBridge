vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getServers: vi.fn(),
}));

vi.mock('../api/network', () => ({
  getNetworkOverview: vi.fn(),
  getNetworkSettings: vi.fn(),
  updateNetworkSettings: vi.fn(),
  checkNetworkAccess: vi.fn(),
  getRegions: vi.fn(),
  createRegion: vi.fn(),
  updateRegion: vi.fn(),
  deleteRegion: vi.fn(),
  getNetworkHosts: vi.fn(),
  getNetworkHost: vi.fn(),
  createNetworkHost: vi.fn(),
  updateNetworkHost: vi.fn(),
  deleteNetworkHost: vi.fn(),
  verifyNetworkHost: vi.fn(),
  importMonitoredHosts: vi.fn(),
  getFirewallOpenings: vi.fn(),
  createFirewallOpening: vi.fn(),
  updateFirewallOpening: vi.fn(),
  deleteFirewallOpening: vi.fn(),
  verifyFirewallOpening: vi.fn(),
  getPortUsages: vi.fn(),
  createPortUsage: vi.fn(),
  updatePortUsage: vi.fn(),
  deletePortUsage: vi.fn(),
  verifyPortUsage: vi.fn(),
  getNetworkProbes: vi.fn(),
  importNetworkCsv: vi.fn(),
  findPorts: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  checkNetworkAccess,
  createFirewallOpening,
  createNetworkHost,
  getFirewallOpenings,
  getNetworkHost,
  getNetworkHosts,
  getNetworkOverview,
  getPortUsages,
  updateNetworkSettings,
  getNetworkProbes,
  getRegions,
  importNetworkCsv,
  updateNetworkHost,
  verifyFirewallOpening,
  findPorts,
  type FirewallOpening,
  type NetworkHost,
  type NetworkOverview,
  type PortUsage,
  type Probe,
} from '../api/network';
import Network from '../pages/Network';
import NetworkHostDetail from '../pages/NetworkHostDetail';
import { renderWithProviders } from './helpers';

const READ = ['network.read'];
const WRITE = ['network.read', 'network.write'];

const mocked = {
  overview: vi.mocked(getNetworkOverview),
  hosts: vi.mocked(getNetworkHosts),
  host: vi.mocked(getNetworkHost),
  regions: vi.mocked(getRegions),
  openings: vi.mocked(getFirewallOpenings),
  createOpening: vi.mocked(createFirewallOpening),
  verifyOpening: vi.mocked(verifyFirewallOpening),
  usages: vi.mocked(getPortUsages),
  check: vi.mocked(checkNetworkAccess),
  probes: vi.mocked(getNetworkProbes),
  importCsv: vi.mocked(importNetworkCsv),
  findPorts: vi.mocked(findPorts),
  createHost: vi.mocked(createNetworkHost),
  updateHost: vi.mocked(updateNetworkHost),
  updateSettings: vi.mocked(updateNetworkSettings),
};

const provenance = {
  source: 'manual',
  created_by: 'alice',
  verified: true,
  verified_at: '2026-11-01T00:00:00Z',
  verified_by: 'alice',
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:00:00Z',
};

function makeHost(overrides: Partial<NetworkHost> = {}): NetworkHost {
  return {
    ...provenance,
    id: 1,
    name: 'app-1',
    ip: '10.1.0.5',
    region_id: 1,
    region_name: 'seoul',
    description: '',
    owner: 'team-a',
    kind: 'managed',
    monitored_host_id: null,
    monitored_host_name: null,
    ...overrides,
  };
}

function makeOpening(overrides: Partial<FirewallOpening> = {}): FirewallOpening {
  return {
    ...provenance,
    id: 10,
    src_host_id: 1,
    src_host_name: 'app-1',
    src_host_ip: '10.1.0.5',
    src_cidr: null,
    src_region_id: 1,
    src_region_name: 'seoul',
    dst_host_id: 2,
    dst_host_name: 'db-1',
    dst_host_ip: '10.2.0.9',
    dst_host_kind: 'managed',
    dst_region_id: 2,
    dst_region_name: 'busan',
    port_from: 5432,
    port_to: 5432,
    protocol: 'tcp',
    status: 'open',
    ticket_no: 'FW-1',
    requester: 'bob',
    purpose: 'app db',
    in_use: true,
    last_probe: null,
    ...overrides,
  };
}

function makeUsage(overrides: Partial<PortUsage> = {}): PortUsage {
  return {
    ...provenance,
    id: 100,
    host_id: 2,
    host_name: 'db-1',
    host_ip: '10.2.0.9',
    port: 5432,
    protocol: 'tcp',
    service_name: 'postgres',
    description: '',
    owner: '',
    auto_kind: null,
    reachable_from_platform: null,
    ...overrides,
  };
}

const HOSTS = [
  makeHost(),
  makeHost({ id: 2, name: 'db-1', ip: '10.2.0.9', region_id: 2, region_name: 'busan' }),
];

function renderNetwork(route: string, permissions = WRITE) {
  return renderWithProviders(<Network />, { permissions, route, useMemoryRouter: true });
}

describe('Network page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.hosts.mockResolvedValue(HOSTS);
    mocked.regions.mockResolvedValue([]);
    mocked.openings.mockResolvedValue([]);
    mocked.usages.mockResolvedValue([]);
    mocked.probes.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the region matrix with counts and lists a cell’s openings on click', async () => {
    mocked.overview.mockResolvedValue({
      regions: [
        { id: 1, name: 'seoul', description: '', cidrs: [], host_count: 1, created_at: null, updated_at: null },
        { id: 2, name: 'busan', description: '', cidrs: [], host_count: 1, created_at: null, updated_at: null },
      ],
      links: [
        { src_region_id: 1, dst_region_id: 2, openings: 4, open_openings: 3, unverified: 1, ports: ['tcp/443', 'tcp/5432'] },
        { src_region_id: null, dst_region_id: 1, openings: 1, open_openings: 1, unverified: 0, ports: ['tcp/22'] },
      ],
      host_count: 7,
      external_host_count: 0,
      unassigned_hosts: 2,
      opening_count: 5,
      unverified_count: 1,
      unused_openings: 1,
      unreachable_usages: 0,
      probe_mismatches: 0,
      platform_host_id: 1,
      main_host_id: null,
      auto_sources_failed: ['gateway_upstream'],
    });
    mocked.openings.mockResolvedValue([
      makeOpening(),
      makeOpening({ id: 11, src_region_id: 2, src_region_name: 'busan', dst_region_id: 1, dst_region_name: 'seoul', dst_host_name: 'web-9' }),
    ]);
    const user = userEvent.setup();
    renderNetwork('/network?view=matrix', READ);

    const matrix = await screen.findByRole('table', { name: 'Region × region openings' });
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('gateway_upstream');
    expect(within(matrix).getAllByText('Same region: all open')).toHaveLength(2);
    expect(within(matrix).getByRole('rowheader', { name: 'Outside regions (CIDR / unassigned)' })).toBeInTheDocument();

    const cell = within(matrix).getByRole('button', { name: /^seoul → busan: 3 open openings/ });
    expect(cell).toHaveTextContent('3');
    expect(cell).toHaveTextContent('of 4');
    expect(cell).toHaveAttribute('title', 'tcp/443, tcp/5432');
    expect(within(matrix).getByRole('button', { name: /^Outside regions.* → seoul: 1 open openings/ })).toBeInTheDocument();

    await user.click(cell);
    const list = await screen.findByRole('table', { name: 'Openings seoul → busan' });
    expect(within(list).getByText('db-1')).toBeInTheDocument();
    expect(within(list).queryByText('web-9')).not.toBeInTheDocument();
  });

  it('creates an opening with the right body and shows the backend 422 message', async () => {
    mocked.createOpening.mockRejectedValueOnce({
      isAxiosError: true,
      response: { status: 422, data: { detail: 'Source and destination are in the same region' } },
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=openings');

    await user.click(await screen.findByRole('button', { name: 'Add opening' }));
    const dialog = screen.getByRole('dialog', { name: 'Add opening' });
    await waitFor(() => expect(within(dialog).getAllByRole('option', { name: /app-1/ })).not.toHaveLength(0));
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Source' }), '1');
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Destination' }), '2');
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Port' }), '5432');
    await user.type(within(dialog).getByRole('textbox', { name: 'Ticket no.' }), ' FW-7 ');
    await user.type(within(dialog).getByRole('textbox', { name: 'Purpose' }), 'db access');
    await user.click(within(dialog).getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mocked.createOpening).toHaveBeenCalledWith({
      src_host_id: 1,
      dst_host_id: 2,
      port_from: 5432,
      port_to: 5432,
      protocol: 'tcp',
      status: 'open',
      ticket_no: 'FW-7',
      requester: '',
      purpose: 'db access',
    }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Source and destination are in the same region');
    expect(screen.getByRole('dialog', { name: 'Add opening' })).toBeInTheDocument();
  });

  it('sends a CIDR source and a port range', async () => {
    mocked.createOpening.mockResolvedValue(makeOpening());
    const user = userEvent.setup();
    renderNetwork('/network?tab=openings');

    await user.click(await screen.findByRole('button', { name: 'Add opening' }));
    const dialog = screen.getByRole('dialog', { name: 'Add opening' });
    await user.click(within(dialog).getByRole('radio', { name: 'CIDR' }));
    await user.type(within(dialog).getByRole('textbox', { name: /^CIDR/ }), '192.168.0.0/24');
    await waitFor(() => expect(within(dialog).getAllByRole('option', { name: /db-1/ })).not.toHaveLength(0));
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Destination' }), '2');
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Port' }), '8000');
    await user.type(within(dialog).getByRole('spinbutton', { name: /^Port to/ }), '8010');
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Protocol' }), 'udp');
    await user.click(within(dialog).getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mocked.createOpening).toHaveBeenCalledWith(expect.objectContaining({
      src_cidr: '192.168.0.0/24', dst_host_id: 2, port_from: 8000, port_to: 8010, protocol: 'udp',
    })));
    expect(mocked.createOpening.mock.calls[0][0]).not.toHaveProperty('src_host_id');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows verify only on unverified rows and only to network.write holders', async () => {
    mocked.openings.mockResolvedValue([
      makeOpening(),
      makeOpening({
        id: 12, source: 'agent', verified: false, verified_at: null, verified_by: null,
        port_from: 8080, port_to: 8080, in_use: false,
      }),
    ]);
    mocked.verifyOpening.mockResolvedValue(makeOpening({ id: 12 }));
    const user = userEvent.setup();
    const view = renderNetwork('/network?tab=openings');

    const table = await screen.findByRole('table', { name: 'Firewall openings' });
    const verifyButtons = within(table).getAllByRole('button', { name: /^Verify / });
    expect(verifyButtons).toHaveLength(1);
    expect(verifyButtons[0]).toHaveAccessibleName('Verify app-1 → db-1 tcp/8080');
    expect(within(table).getByText('Unverified')).toBeInTheDocument();
    expect(within(table).getByText('Agent')).toBeInTheDocument();
    // Open but nothing listens there: flagged as unused.
    expect(within(table).getByText('Unused').closest('tr')).toHaveClass('network-row--unused');

    await user.click(verifyButtons[0]);
    await waitFor(() => expect(mocked.verifyOpening).toHaveBeenCalledWith(12, expect.anything()));
    view.unmount();

    renderNetwork('/network?tab=openings', READ);
    const readOnly = await screen.findByRole('table', { name: 'Firewall openings' });
    expect(within(readOnly).getByText('Unverified')).toBeInTheDocument();
    expect(within(readOnly).queryByRole('button', { name: /^Verify / })).not.toBeInTheDocument();
    expect(within(readOnly).queryByRole('button', { name: /^Edit / })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add opening' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Settings' })).not.toBeInTheDocument();
  });

  it('shows the request draft when access is blocked', async () => {
    mocked.check.mockResolvedValue({
      status: 'blocked',
      reason: 'no_opening',
      message: 'Different regions and no firewall opening is recorded: request one.',
      src: { input: 'app-1', host_id: 1, host_name: 'app-1', ip: '10.1.0.5', region_id: 1, region_name: 'seoul' },
      dst: { input: '10.2.0.9', host_id: 2, host_name: 'db-1', ip: '10.2.0.9', region_id: 2, region_name: 'busan' },
      port: 5432,
      protocol: 'tcp',
      openings: [],
      usages: [makeUsage()],
      request_draft: 'Source: app-1 (10.1.0.5)\nDestination: db-1 (10.2.0.9)\nPort: tcp/5432',
      probe: null,
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=check', READ);

    await user.type(await screen.findByRole('combobox', { name: 'Source' }), 'app-1');
    await user.type(screen.getByRole('combobox', { name: 'Destination' }), '10.2.0.9');
    await user.type(screen.getByRole('spinbutton', { name: 'Port' }), '5432');
    await user.click(screen.getByRole('button', { name: 'Check' }));

    await waitFor(() => expect(mocked.check).toHaveBeenCalledWith(
      { src: 'app-1', dst: '10.2.0.9', port: 5432, protocol: 'tcp' },
      expect.anything(),
    ));
    expect(await screen.findByText('Blocked')).toHaveClass('badge-error');
    expect(screen.getByText(/no firewall opening is recorded/)).toBeInTheDocument();
    expect(screen.getByLabelText('Firewall request draft')).toHaveTextContent('Destination: db-1 (10.2.0.9)');
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
    expect(within(screen.getByRole('table', { name: 'Usage on this port' })).getByText('postgres')).toBeInTheDocument();
  });

  it('hides the request draft when access is open', async () => {
    mocked.check.mockResolvedValue({
      status: 'open',
      reason: 'same_region',
      message: 'Same region',
      src: { input: 'a', host_id: 1, host_name: 'a', ip: '10.1.0.1', region_id: 1, region_name: 'seoul' },
      dst: { input: 'b', host_id: 3, host_name: 'b', ip: '10.1.0.2', region_id: 1, region_name: 'seoul' },
      port: 22,
      protocol: 'tcp',
      openings: [],
      usages: [],
      request_draft: null,
      probe: null,
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=check', READ);
    await user.type(await screen.findByRole('combobox', { name: 'Source' }), 'a');
    await user.type(screen.getByRole('combobox', { name: 'Destination' }), 'b');
    await user.type(screen.getByRole('spinbutton', { name: 'Port' }), '22');
    await user.click(screen.getByRole('button', { name: 'Check' }));

    expect(await screen.findByText('Open')).toHaveClass('badge-ok');
    expect(screen.queryByLabelText('Firewall request draft')).not.toBeInTheDocument();
  });

  it('keeps auto-discovered usages read-only and flags unreachable ones', async () => {
    mocked.usages.mockResolvedValue([
      makeUsage({
        id: null, source: 'auto', auto_kind: 'db', service_name: 'unibridge db: orders',
        reachable_from_platform: false, created_by: null,
      }),
      makeUsage({ id: 101, port: 6379, service_name: 'redis', source: 'agent', verified: false, verified_at: null }),
    ]);
    renderNetwork('/network?tab=usages');

    const table = await screen.findByRole('table', { name: 'Port usage' });
    const autoRow = within(table).getByText('unibridge db: orders').closest('tr')!;
    expect(within(autoRow).getByText('Auto-discovered')).toBeInTheDocument();
    expect(within(autoRow).getByText('Database connection')).toBeInTheDocument();
    expect(within(autoRow).getByText('Not reachable from platform')).toBeInTheDocument();
    expect(within(autoRow).getByText('Read-only (auto)')).toBeInTheDocument();
    expect(within(autoRow).queryByRole('button')).not.toBeInTheDocument();
    expect(within(autoRow).queryByText('Unverified')).not.toBeInTheDocument();

    const agentRow = within(table).getByText('redis').closest('tr')!;
    expect(within(agentRow).getByRole('button', { name: 'Verify db-1:6379 redis' })).toBeInTheDocument();
    expect(within(agentRow).getByRole('button', { name: 'Edit db-1:6379 redis' })).toBeInTheDocument();
    expect(within(agentRow).getByRole('button', { name: 'Delete db-1:6379 redis' })).toBeInTheDocument();
  });

  it('lists hosts with links to their detail page and the import button for writers', async () => {
    mocked.hosts.mockResolvedValue([
      makeHost({ monitored_host_id: 5, monitored_host_name: 'mon-app-1' }),
      makeHost({ id: 3, name: 'edge-1', ip: '10.9.0.1', region_id: null, region_name: null, source: 'agent', verified: false }),
    ]);
    renderNetwork('/network?tab=hosts');

    const table = await screen.findByRole('table', { name: 'Hosts' });
    expect(within(table).getByRole('link', { name: 'app-1' })).toHaveAttribute('href', '/network/hosts/1');
    expect(within(table).getByText('mon-app-1')).toBeInTheDocument();
    expect(within(table).getByText('No region')).toBeInTheDocument();
    expect(within(table).getAllByRole('button', { name: /^Verify / })).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Import monitored servers' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Settings' })).toBeInTheDocument();
  });
});

describe('Network host detail', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.hosts.mockResolvedValue(HOSTS);
    mocked.probes.mockResolvedValue([]);
  });

  it('shows inbound, outbound and port usage of the host', async () => {
    mocked.host.mockResolvedValue({
      ...makeHost({ id: 2, name: 'db-1', ip: '10.2.0.9', region_id: 2, region_name: 'busan' }),
      inbound: [makeOpening()],
      outbound: [],
      outbound_via_cidr: [],
      usages: [makeUsage({ id: null, source: 'auto', auto_kind: 'db', reachable_from_platform: false })],
      outbound_usages: [],
      same_region_hosts: [],
    });
    renderWithProviders(
      <Routes><Route path="/network/hosts/:id" element={<NetworkHostDetail />} /></Routes>,
      { permissions: READ, route: '/network/hosts/2', useMemoryRouter: true },
    );

    expect(await screen.findByRole('heading', { name: 'db-1' })).toBeInTheDocument();
    expect(mocked.host).toHaveBeenCalledWith(2);
    expect(within(screen.getByRole('table', { name: 'Inbound openings' })).getByText('app-1')).toBeInTheDocument();
    expect(screen.getByText('No firewall openings.')).toBeInTheDocument();
    const usages = screen.getByRole('table', { name: 'Port usage' });
    expect(within(usages).getByText('Not reachable from platform')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit host' })).not.toBeInTheDocument();
  });
});

/* ── Phase 2: region map, CSV import, probes ── */

const REGIONS = [
  { id: 1, name: 'seoul', description: '', cidrs: [], host_count: 3, created_at: null, updated_at: null },
  { id: 2, name: 'busan', description: '', cidrs: [], host_count: 1, created_at: null, updated_at: null },
];

function makeOverview(overrides: Partial<NetworkOverview> = {}): NetworkOverview {
  return {
    regions: REGIONS,
    links: [],
    host_count: 4,
    external_host_count: 0,
    unassigned_hosts: 0,
    opening_count: 0,
    unverified_count: 0,
    unused_openings: 0,
    unreachable_usages: 0,
    probe_mismatches: 0,
    platform_host_id: null,
    main_host_id: null,
    auto_sources_failed: [],
    ...overrides,
  };
}

function makeProbe(overrides: Partial<Probe> = {}): Probe {
  return {
    id: 500,
    src_host_id: 1,
    src_host_name: 'app-1',
    src_host_ip: '10.1.0.5',
    src_region_name: 'seoul',
    dst_host_id: 2,
    dst_host_name: 'db-1',
    dst_host_ip: '10.2.0.9',
    dst_region_name: 'busan',
    port: 5432,
    protocol: 'tcp',
    result: 'open',
    latency_ms: 1.25,
    error: '',
    reported_by: 'apikey:prober',
    observed_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    expected: 'open',
    expected_reason: 'opening',
    mismatch: null,
    ...overrides,
  };
}

describe('Network region map', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.hosts.mockResolvedValue(HOSTS);
    mocked.regions.mockResolvedValue([]);
    mocked.probes.mockResolvedValue([]);
  });

  it('draws regions and directed edges, and an edge click lists that pair’s openings', async () => {
    mocked.overview.mockResolvedValue(makeOverview({
      links: [
        { src_region_id: 1, dst_region_id: 2, openings: 4, open_openings: 3, unverified: 1, ports: ['tcp/443', 'tcp/5432'] },
        { src_region_id: 2, dst_region_id: 1, openings: 1, open_openings: 1, unverified: 0, ports: ['tcp/22'] },
        { src_region_id: null, dst_region_id: 1, openings: 1, open_openings: 1, unverified: 0, ports: ['tcp/80'] },
      ],
    }));
    mocked.openings.mockResolvedValue([
      makeOpening(),
      makeOpening({ id: 11, src_region_id: 2, src_region_name: 'busan', dst_region_id: 1, dst_region_name: 'seoul', dst_host_name: 'web-9' }),
    ]);
    const user = userEvent.setup();
    renderNetwork('/network', READ);

    const toggle = await screen.findByRole('group', { name: 'View' });
    // The server-centred map is the default view.
    expect(within(toggle).getByRole('button', { name: 'By server' })).toHaveAttribute('aria-pressed', 'true');
    await user.click(within(toggle).getByRole('button', { name: 'Map' }));

    expect(screen.queryByRole('table', { name: 'Region × region openings' })).not.toBeInTheDocument();
    const graph = screen.getByRole('group', { name: 'Region map' });
    expect(within(graph).getByText('seoul', { selector: 'text' })).toBeInTheDocument();
    expect(within(graph).getByText('busan', { selector: 'text' })).toBeInTheDocument();
    expect(within(graph).getByText('3 hosts')).toBeInTheDocument();
    expect(within(graph).getByText('CIDR / outside regions', { selector: 'text' })).toBeInTheDocument();

    const forward = within(graph).getByRole('button', { name: /^seoul → busan: 3 open openings, 1 unverified/ });
    const back = within(graph).getByRole('button', { name: /^busan → seoul: 1 open openings, 0 unverified/ });
    expect(within(graph).getByRole('button', { name: /^CIDR \/ outside regions → seoul/ })).toBeInTheDocument();
    expect(forward).toHaveTextContent('3');
    expect(forward).toHaveTextContent('1 unverified');
    expect(forward.querySelector('title')).toHaveTextContent('tcp/443, tcp/5432');
    // The two directions of a pair bow apart instead of sharing one line.
    const line = (g: HTMLElement) => g.querySelector('.network-graph__line')?.getAttribute('d');
    expect(line(forward)).toMatch(/^M [\d.]+ [\d.]+ Q /);
    expect(line(forward)).not.toEqual(line(back));
    expect(forward.querySelector('.network-graph__line')?.getAttribute('marker-end')).toMatch(/-warn\)$/);

    await user.click(forward);
    expect(forward).toHaveAttribute('aria-pressed', 'true');
    const list = await screen.findByRole('table', { name: 'Openings seoul → busan' });
    expect(within(list).getByText('db-1')).toBeInTheDocument();
    expect(within(list).queryByText('web-9')).not.toBeInTheDocument();

    // Keyboard: Enter on the other edge switches the selection.
    back.focus();
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('table', { name: 'Openings busan → seoul' })).toBeInTheDocument();
  });
});

describe('Network CSV import', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.hosts.mockResolvedValue(HOSTS);
    mocked.regions.mockResolvedValue([]);
    mocked.openings.mockResolvedValue([]);
    mocked.usages.mockResolvedValue([]);
  });

  it('previews with a dry run, then imports the same text', async () => {
    const csv = 'name,ip,region\nweb-1,10.1.0.7,seoul\napp-1,10.1.0.5,seoul\nbad,not-an-ip,';
    mocked.importCsv
      .mockResolvedValueOnce({
        kind: 'hosts', dry_run: true, created: 1, skipped: 1, errors: 1,
        rows: [
          { line: 2, action: 'create', key: 'web-1', message: '' },
          { line: 3, action: 'skip', key: 'app-1', message: 'Already recorded' },
          { line: 4, action: 'error', key: 'bad', message: 'Invalid IP address' },
        ],
      })
      .mockResolvedValueOnce({
        kind: 'hosts', dry_run: false, created: 1, skipped: 1, errors: 1,
        rows: [
          { line: 2, action: 'create', key: 'web-1', message: '' },
          { line: 3, action: 'skip', key: 'app-1', message: 'Already recorded' },
          { line: 4, action: 'error', key: 'bad', message: 'Invalid IP address' },
        ],
      });
    const user = userEvent.setup();
    renderNetwork('/network?tab=hosts');

    await user.click(await screen.findByRole('button', { name: 'Import CSV' }));
    const dialog = screen.getByRole('dialog', { name: 'Import hosts from CSV' });
    // The expected columns are listed in both languages.
    expect(within(dialog).getByText(/호스트명/)).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Run import' })).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole('textbox', { name: 'Or paste CSV text' }));
    await user.paste(csv);
    await user.click(within(dialog).getByRole('button', { name: 'Preview (dry run)' }));

    await waitFor(() => expect(mocked.importCsv).toHaveBeenCalledWith({ kind: 'hosts', csv, dry_run: true }));
    const rows = await within(dialog).findByRole('table', { name: 'Import lines' });
    expect(within(rows).getByText('Invalid IP address')).toBeInTheDocument();
    expect(within(rows).getByText('Create')).toHaveClass('badge-ok');
    expect(within(rows).getByText('Skip')).toHaveClass('badge-unknown');
    expect(within(rows).getByText('Error')).toHaveClass('badge-error');
    expect(within(dialog).getByText('Create 1')).toBeInTheDocument();
    expect(within(dialog).getByText('Error 1')).toBeInTheDocument();
    expect(mocked.hosts).toHaveBeenCalledTimes(1);

    await user.click(within(dialog).getByRole('button', { name: 'Run import' }));
    await waitFor(() => expect(mocked.importCsv).toHaveBeenLastCalledWith({ kind: 'hosts', csv, dry_run: false }));
    expect(await within(dialog).findByText('Import finished: 1 created.')).toBeInTheDocument();
    // The lists refresh after a real import.
    await waitFor(() => expect(mocked.hosts.mock.calls.length).toBeGreaterThan(1));
  });

  it('reads an EUC-KR file into the text box and shows a 422 header error', async () => {
    mocked.importCsv.mockRejectedValueOnce({
      isAxiosError: true,
      response: { status: 422, data: { detail: 'The header row has no column for: dst (accepted names: dst, destination)' } },
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=openings');

    await user.click(await screen.findByRole('button', { name: 'Import CSV' }));
    const dialog = screen.getByRole('dialog', { name: 'Import firewall openings from CSV' });
    // "이름,ip\n서버1,10.0.0.1\n" as CP949 (Excel on Korean Windows).
    const bytes = new Uint8Array([192, 204, 184, 167, 44, 105, 112, 10, 188, 173, 185, 246, 49, 44, 49, 48, 46, 48, 46, 48, 46, 49, 10]);
    await user.upload(within(dialog).getByLabelText('CSV file'), new File([bytes], 'hosts.csv', { type: 'text/csv' }));

    const text = within(dialog).getByRole('textbox', { name: 'Or paste CSV text' });
    await waitFor(() => expect(text).toHaveValue('이름,ip\n서버1,10.0.0.1\n'));
    expect(within(dialog).getByText('hosts.csv read as EUC-KR')).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: 'Preview (dry run)' }));
    await waitFor(() => expect(mocked.importCsv).toHaveBeenCalledWith(
      { kind: 'openings', csv: '이름,ip\n서버1,10.0.0.1\n', dry_run: true },
    ));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The header row has no column for: dst');
    expect(within(dialog).queryByRole('button', { name: 'Run import' })).not.toBeInTheDocument();
  });

  it('hides the import button without network.write', async () => {
    for (const tab of ['hosts', 'openings', 'usages']) {
      const view = renderNetwork(`/network?tab=${tab}`, READ);
      await screen.findByRole('tabpanel');
      await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
      expect(screen.queryByRole('button', { name: 'Import CSV' })).not.toBeInTheDocument();
      view.unmount();
    }
    renderNetwork('/network?tab=usages');
    expect(await screen.findByRole('button', { name: 'Import CSV' })).toBeInTheDocument();
  });
});

describe('Network probes', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.hosts.mockResolvedValue(HOSTS);
    mocked.regions.mockResolvedValue([]);
    mocked.openings.mockResolvedValue([]);
    mocked.probes.mockResolvedValue([]);
  });

  it('shows result and mismatch badges and filters to mismatches', async () => {
    mocked.probes.mockResolvedValue([
      makeProbe({ id: 1, result: 'open' }),
      makeProbe({ id: 2, port: 8080, result: 'refused', expected: 'blocked', expected_reason: 'no_opening', mismatch: 'passes_without_opening' }),
      makeProbe({ id: 3, port: 6379, result: 'timeout', mismatch: 'recorded_open_but_blocked', latency_ms: null }),
      makeProbe({ id: 4, port: 9000, result: 'error', error: 'no route to host', expected: 'unknown', expected_reason: 'unknown_region' }),
    ]);
    const user = userEvent.setup();
    renderNetwork('/network?tab=probes', READ);

    const table = await screen.findByRole('table', { name: 'Probes' });
    expect(mocked.probes).toHaveBeenCalledWith({ host_id: undefined, result: undefined, mismatch: false });
    const row = (port: string) => within(table).getByText(`:${port}`).closest('tr')!;
    // Columns: path, result, records say, mismatch, latency, measured at, reported by.
    const cell = (port: string, index: number) => within(row(port).cells[index]);

    expect(cell('5432', 1).getByText('Open')).toHaveClass('badge-ok');
    expect(cell('5432', 2).getByText('Open')).toHaveAttribute('title', 'Open through a recorded firewall opening.');
    expect(within(row('5432')).getByText('1.3 ms')).toBeInTheDocument();
    expect(within(row('5432')).getByText('5 minutes ago')).toBeInTheDocument();
    expect(within(row('5432')).getByText('apikey:prober')).toBeInTheDocument();

    expect(within(row('8080')).getByText('Refused')).toHaveClass('badge-info');
    expect(within(row('8080')).getByText('Path passes, no service listening')).toBeInTheDocument();
    expect(within(row('8080')).getByText('Blocked')).toHaveClass('badge-error');
    expect(within(row('8080')).getByText('Not recorded · probe passes')).toHaveClass('badge-warn');
    expect(row('8080')).toHaveClass('network-row--mismatch');

    expect(within(row('6379')).getByText('Timeout')).toHaveClass('badge-error');
    expect(within(row('6379')).getByText('Recorded open · probe blocked')).toHaveClass('badge-error');

    expect(cell('9000', 1).getByText('Error')).toHaveClass('badge-unknown');
    expect(within(row('9000')).getByText('no route to host')).toBeInTheDocument();
    expect(row('9000')).not.toHaveClass('network-row--mismatch');

    expect(screen.getByText('How probes get here')).toBeInTheDocument();

    await user.click(screen.getByRole('checkbox', { name: 'Mismatches only' }));
    await waitFor(() => expect(mocked.probes).toHaveBeenLastCalledWith({ host_id: undefined, result: undefined, mismatch: true }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Result' }), 'timeout');
    await waitFor(() => expect(mocked.probes).toHaveBeenLastCalledWith({ host_id: undefined, result: 'timeout', mismatch: true }));
  });

  it('the overview mismatch card opens the probes tab filtered to mismatches', async () => {
    mocked.overview.mockResolvedValue(makeOverview({ probe_mismatches: 2 }));
    const user = userEvent.setup();
    renderNetwork('/network', READ);

    const card = await screen.findByRole('button', { name: /Probes contradicting the records/ });
    expect(card).toHaveClass('network-summary-card--warn');
    expect(card).toHaveTextContent('2');
    await user.click(card);

    expect(await screen.findByRole('tab', { name: 'Probes' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('checkbox', { name: 'Mismatches only' })).toBeChecked();
    await waitFor(() => expect(mocked.probes).toHaveBeenCalledWith(expect.objectContaining({ mismatch: true })));
  });

  it('shows an opening’s last probe, red on a mismatch', async () => {
    mocked.openings.mockResolvedValue([
      makeOpening({
        last_probe: {
          id: 7, src_host_name: 'app-1', port: 5432, result: 'timeout',
          observed_at: new Date(Date.now() - 2 * 3600_000).toISOString(), mismatch: 'recorded_open_but_blocked',
        },
      }),
      makeOpening({
        id: 13, port_from: 443, port_to: 443,
        last_probe: { id: 8, src_host_name: 'app-1', port: 443, result: 'open', observed_at: new Date().toISOString(), mismatch: null },
      }),
      makeOpening({ id: 14, port_from: 22, port_to: 22 }),
    ]);
    renderNetwork('/network?tab=openings', READ);

    const table = await screen.findByRole('table', { name: 'Firewall openings' });
    const red = within(table).getByText(/^Probe: Timeout 2 hours ago$/);
    expect(red).toHaveClass('badge-error');
    expect(red.getAttribute('title')).toMatch(/the probe timed out/);
    expect(within(table).getByText(/^Probe: Open /)).toHaveClass('badge-ok');
    expect(within(table).getAllByText(/^Probe:/)).toHaveLength(2);
  });

  it('shows the measured probe in an access check result', async () => {
    mocked.check.mockResolvedValue({
      status: 'open',
      reason: 'opening',
      message: 'Open through a recorded firewall opening.',
      src: { input: 'app-1', host_id: 1, host_name: 'app-1', ip: '10.1.0.5', region_id: 1, region_name: 'seoul' },
      dst: { input: 'db-1', host_id: 2, host_name: 'db-1', ip: '10.2.0.9', region_id: 2, region_name: 'busan' },
      port: 5432,
      protocol: 'tcp',
      openings: [],
      usages: [],
      request_draft: null,
      probe: makeProbe({ result: 'timeout', mismatch: 'recorded_open_but_blocked' }),
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=check', READ);
    await user.type(await screen.findByRole('combobox', { name: 'Source' }), 'app-1');
    await user.type(screen.getByRole('combobox', { name: 'Destination' }), 'db-1');
    await user.type(screen.getByRole('spinbutton', { name: 'Port' }), '5432');
    await user.click(screen.getByRole('button', { name: 'Check' }));

    const probe = await screen.findByRole('group', { name: 'Measured' });
    expect(within(probe).getByText('Timeout')).toHaveClass('badge-error');
    expect(within(probe).getByText('Recorded open · probe blocked')).toBeInTheDocument();
    expect(within(probe).getByText('reported by apikey:prober')).toBeInTheDocument();
  });

  it('lists probes from and to the host on its detail page', async () => {
    mocked.host.mockResolvedValue({
      ...makeHost({ id: 2, name: 'db-1', ip: '10.2.0.9', region_id: 2, region_name: 'busan' }),
      inbound: [],
      outbound: [],
      outbound_via_cidr: [],
      usages: [],
      outbound_usages: [],
      same_region_hosts: [],
    });
    mocked.probes.mockResolvedValue([makeProbe({ result: 'refused' })]);
    renderWithProviders(
      <Routes><Route path="/network/hosts/:id" element={<NetworkHostDetail />} /></Routes>,
      { permissions: READ, route: '/network/hosts/2', useMemoryRouter: true },
    );

    const table = await screen.findByRole('table', { name: 'Probes from / to this host' });
    expect(mocked.probes).toHaveBeenCalledWith({ host_id: 2 });
    expect(within(table).getByText('Refused')).toHaveClass('badge-info');
    expect(screen.getByRole('link', { name: 'Open in Probes tab' })).toHaveAttribute('href', '/network?tab=probes&host_id=2');
  });
});

/* ── Server-centred map and port finder ── */

describe('Network server-centred map', () => {
  const center = {
    ...makeHost({ id: 2, name: 'db-1', ip: '10.2.0.9', region_id: 2, region_name: 'busan' }),
    inbound: [
      makeOpening(),
      makeOpening({ id: 12, port_from: 8000, port_to: 8010, verified: false }),
      makeOpening({ id: 13, src_host_id: null, src_host_name: null, src_host_ip: null, src_cidr: '172.16.0.0/16', src_region_id: null, src_region_name: null, port_from: 22, port_to: 22, status: 'closed' }),
    ],
    outbound: [],
    outbound_via_cidr: [
      makeOpening({ id: 14, src_host_id: null, src_cidr: '10.2.0.0/16', dst_host_id: 3, dst_host_name: 'gpu-1', dst_host_ip: '10.3.0.1', dst_region_name: 'cloud', port_from: 9400, port_to: 9400, last_probe: { id: 1, src_host_name: 'db-1', port: 9400, result: 'timeout', observed_at: '2026-11-01T00:00:00Z', mismatch: 'recorded_open_but_blocked' } }),
    ],
    usages: [makeUsage()],
    outbound_usages: [makeUsage({ id: 101, host_id: 3, host_name: 'gpu-1', host_ip: '10.3.0.1', port: 9400, service_name: 'dcgm-exporter' })],
    same_region_hosts: [makeHost({ id: 5, name: 'db-2', ip: '10.2.0.10', region_id: 2, region_name: 'busan' })],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mocked.hosts.mockResolvedValue(HOSTS);
    mocked.regions.mockResolvedValue([]);
    mocked.probes.mockResolvedValue([]);
    mocked.overview.mockResolvedValue(makeOverview());
    mocked.host.mockImplementation(async (id: number) =>
      id === 2 ? center : { ...center, ...makeHost({ id, name: `host-${id}` }), inbound: [], outbound_via_cidr: [], outbound_usages: [], same_region_hosts: [] },
    );
  });

  it('draws one host with what reaches it, what it reaches and its region, and re-centres on click', async () => {
    const user = userEvent.setup();
    renderNetwork('/network?view=host&host=2', READ);

    const map = await screen.findByRole('group', { name: 'Connection map of db-1' });
    // One edge per peer: app-1 (two openings merged) and the CIDR range; gpu-1 via a range.
    const fromApp = within(map).getByRole('button', { name: 'app-1 → db-1: tcp/5432, tcp/8000-8010' });
    expect(fromApp).toHaveClass('host-map__edge--warn');
    expect(within(map).getByRole('button', { name: /^172\.16\.0\.0\/16 → db-1: tcp\/22 \(Closed\)/ })).toHaveClass('host-map__edge--closed');
    expect(within(map).getByRole('button', { name: 'db-1 → gpu-1: tcp/9400' })).toHaveClass('host-map__edge--mismatch');
    expect(within(map).getByText(/via range/)).toBeInTheDocument();
    expect(within(map).getByText('postgres')).toBeInTheDocument();

    await user.click(fromApp);
    const list = await screen.findByRole('table', { name: 'Openings of the selected connection' });
    expect(within(list).getAllByText('app-1')).toHaveLength(2);

    await user.click(within(map).getByRole('button', { name: /^Same region busan: 1 hosts/ }));
    await user.click(screen.getByRole('button', { name: 'db-2' }));
    await waitFor(() => expect(mocked.host).toHaveBeenCalledWith(5));
    expect(await screen.findByRole('group', { name: 'Connection map of host-5' })).toBeInTheDocument();

  });

  it('picks the centre from the overview when no main server is set', async () => {
    const user = userEvent.setup();
    renderNetwork('/network', READ);
    // The default view, with nothing to centre on yet.
    expect(await screen.findByText('Pick a server to centre the map on.')).toBeInTheDocument();
    expect(mocked.host).not.toHaveBeenCalled();
    await user.selectOptions(screen.getByLabelText('Centre server'), '2');
    expect(await screen.findByRole('group', { name: 'Connection map of db-1' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Find free ports on this server' })).toHaveAttribute('href', '/network?tab=finder&dst=db-1');
  });

  it('opens straight on the main server, and a writer can make another server the main one', async () => {
    mocked.overview.mockResolvedValue(makeOverview({ platform_host_id: 1, main_host_id: 2 }));
    mocked.updateSettings.mockResolvedValue({ platform_host_id: 1, main_host_id: 1 });
    const user = userEvent.setup();
    renderNetwork('/network', WRITE);

    expect(await screen.findByRole('group', { name: 'Connection map of db-1' })).toBeInTheDocument();
    expect(screen.getByText('Main server')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Set as main server' })).not.toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'db-1 (10.2.0.9) · main' })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Centre server'), '1');
    expect(await screen.findByRole('group', { name: 'Connection map of host-1' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Set as main server' }));
    await waitFor(() => expect(mocked.updateSettings).toHaveBeenCalledWith({ main_host_id: 1 }));
    await user.click(screen.getByRole('button', { name: 'Back to db-1' }));
    expect(await screen.findByRole('group', { name: 'Connection map of db-1' })).toBeInTheDocument();
  });

  it('a reader sees the main server but cannot change it', async () => {
    mocked.overview.mockResolvedValue(makeOverview({ main_host_id: 2 }));
    renderNetwork('/network', READ);
    expect(await screen.findByRole('group', { name: 'Connection map of db-1' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Set as main server' })).not.toBeInTheDocument();
  });

  it('falls back to the platform host without a main server', async () => {
    mocked.overview.mockResolvedValue(makeOverview({ platform_host_id: 2 }));
    renderNetwork('/network', READ);
    expect(await screen.findByRole('group', { name: 'Connection map of db-1' })).toBeInTheDocument();
  });

  it('hovering an edge shows the service on each port or that the port is free', async () => {
    const user = userEvent.setup();
    renderNetwork('/network?host=2', READ);
    const map = await screen.findByRole('group', { name: 'Connection map of db-1' });

    const fromApp = within(map).getByRole('button', { name: 'app-1 → db-1: tcp/5432, tcp/8000-8010' });
    await user.hover(fromApp);
    let tip = await screen.findByRole('tooltip');
    expect(fromApp).toHaveAttribute('aria-describedby', tip.id);
    expect(tip).toHaveTextContent('Services on db-1');
    const [single, range] = within(tip).getAllByRole('listitem');
    expect(single).toHaveTextContent('tcp/5432');
    expect(single).toHaveTextContent('postgres');
    expect(single).not.toHaveTextContent('Free port');
    expect(range).toHaveTextContent('tcp/8000-8010');
    expect(range).toHaveTextContent('All 11 ports free');
    expect(range).toHaveTextContent('Unverified');

    await user.unhover(fromApp);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    // Outbound: the services are the peer's, through a range the host is in.
    await user.hover(within(map).getByRole('button', { name: 'db-1 → gpu-1: tcp/9400' }));
    tip = await screen.findByRole('tooltip');
    expect(tip).toHaveTextContent('Services on gpu-1');
    expect(tip).toHaveTextContent('dcgm-exporter');
    expect(tip).toHaveTextContent('Last probe timed out');

    // A closed opening with nothing on record.
    await user.hover(within(map).getByRole('button', { name: /^172\.16\.0\.0\/16 → db-1/ }));
    tip = await screen.findByRole('tooltip');
    expect(tip).toHaveTextContent('Closed');
    expect(tip).toHaveTextContent('Free port: no service on record');
  });
});

describe('Network port finder', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.hosts.mockResolvedValue(HOSTS);
    mocked.regions.mockResolvedValue(REGIONS);
    mocked.probes.mockResolvedValue([]);
  });

  it('sends hosts and regions as sources and shows suggestions, the port strip and same-region hosts', async () => {
    mocked.findPorts.mockResolvedValue({
      protocol: 'tcp',
      port: 8005,
      sources: [
        { input: 'app-1', host_id: 1, host_name: 'app-1', ip: '10.1.0.5', region_id: 1, region_name: 'seoul' },
      ],
      unplaced_sources: [],
      unplaced_destinations: [],
      truncated: false,
      hosts: [
        {
          host: makeHost({ id: 2, name: 'db-1', ip: '10.2.0.9', region_id: 2, region_name: 'busan' }),
          any_port: false,
          reachable: [{ port_from: 8000, port_to: 8007 }],
          reachable_count: 8,
          free: [{ port_from: 8000, port_to: 8000 }, { port_from: 8003, port_to: 8007 }],
          free_count: 6,
          free_truncated: false,
          used: [
            { port: 8001, service_name: 'api', kind: 'recorded', auto_kind: null },
            { port: 8002, service_name: '', kind: 'listening', auto_kind: null },
          ],
          blocked: [],
          suggestions: [
            { port: 8005, evidence: 'probed_free', probed_from: ['app-1'] },
            { port: 8000, evidence: 'recorded', probed_from: [] },
          ],
          requested_port: 'free',
        },
        {
          host: makeHost({ id: 7, name: 'app-2', ip: '10.1.0.6' }),
          any_port: true,
          reachable: [{ port_from: 1, port_to: 65535 }],
          reachable_count: 65535,
          free: [{ port_from: 1, port_to: 65535 }],
          free_count: 65535,
          free_truncated: false,
          used: [],
          blocked: [],
          suggestions: [],
          requested_port: 'free',
        },
      ],
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=finder', READ);

    const src = await screen.findByLabelText('Client servers');
    await user.type(src, 'app-1{Enter}');
    await waitFor(() => expect(screen.getByRole('option', { name: 'seoul (3 hosts)' })).toBeInTheDocument());
    await user.selectOptions(screen.getByRole('combobox', { name: '+ Add a whole region' }), '1');
    expect(screen.getByText('All of region seoul')).toBeInTheDocument();
    await user.type(screen.getByLabelText('Wanted port'), '8005');
    await user.click(screen.getByRole('button', { name: 'Find' }));

    await waitFor(() => expect(mocked.findPorts).toHaveBeenCalled());
    expect(mocked.findPorts.mock.calls[0][0]).toEqual({
      src: ['app-1'], srcRegions: [1], dst: undefined, dstRegion: undefined, port: 8005, protocol: 'tcp',
    });

    const card = await screen.findByRole('article', { name: 'db-1' });
    expect(within(card).getByText('8005 is free')).toHaveClass('badge-ok');
    expect(within(card).getByText('6 free ports')).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: 'Copy port 8005' })).toHaveClass('finder-chip--probed');
    const cells = within(card).getByRole('list', { name: 'Range 8000-8007, 6/8 free' });
    expect(within(cells).getByLabelText('8001: in use (api)')).toHaveClass('port-strip__cell--used');
    expect(within(cells).getByLabelText('8002: an unrecorded service answers')).toBeInTheDocument();
    expect(within(cells).getByLabelText('8005: free')).toHaveClass('port-strip__cell--suggested');

    const same = screen.getByRole('table', { name: '1 server(s) in the same region: no request needed' });
    expect(within(same).getByText('app-2')).toBeInTheDocument();
  });

  it('takes the destination from the URL and says when nothing is open', async () => {
    mocked.findPorts.mockResolvedValue({
      protocol: 'tcp', port: null, sources: [], unplaced_sources: [], unplaced_destinations: [], truncated: false,
      hosts: [{
        host: makeHost({ id: 2, name: 'db-1' }), any_port: false, reachable: [], reachable_count: 0,
        free: [], free_count: 0, free_truncated: false, used: [], blocked: [], suggestions: [], requested_port: null,
      }],
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=finder&dst=db-1&src=app-1&src=APP-1', READ);
    expect(await screen.findByLabelText('Server for the service')).toHaveValue('db-1');
    expect(screen.getByText('app-1')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Find' }));
    expect(await screen.findByText(/No open port on this server reaches from every source/)).toBeInTheDocument();
    expect(mocked.findPorts.mock.calls[0][0]).toMatchObject({ src: ['app-1'], dst: 'db-1' });
  });
});

describe('Network external hosts', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.hosts.mockResolvedValue(HOSTS);
    mocked.regions.mockResolvedValue(REGIONS);
    mocked.openings.mockResolvedValue([]);
    mocked.usages.mockResolvedValue([]);
  });

  it('records an opening to an IP not on the map as a new external destination', async () => {
    mocked.createOpening.mockResolvedValue(makeOpening({ dst_host_kind: 'external' }));
    const user = userEvent.setup();
    renderNetwork('/network?tab=openings');

    await user.click(await screen.findByRole('button', { name: 'Add opening' }));
    const dialog = screen.getByRole('dialog', { name: 'Add opening' });
    await waitFor(() => expect(within(dialog).getAllByRole('option', { name: /app-1/ })).not.toHaveLength(0));
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Source' }), '1');
    await user.click(within(dialog).getByRole('radio', { name: 'IP not on the map' }));
    expect(within(dialog).queryByRole('combobox', { name: 'Destination' })).not.toBeInTheDocument();
    await user.type(within(dialog).getByRole('textbox', { name: /^IP not on the map/ }), ' 172.20.0.5 ');
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Port' }), '443');
    await user.click(within(dialog).getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mocked.createOpening).toHaveBeenCalledWith(expect.objectContaining({
      src_host_id: 1, dst_host: '172.20.0.5', create_missing_destination: true, port_from: 443,
    })));
    expect(mocked.createOpening.mock.calls[0][0]).not.toHaveProperty('dst_host_id');
  });

  it('editing an opening offers no new-IP destination', async () => {
    mocked.openings.mockResolvedValue([makeOpening()]);
    const user = userEvent.setup();
    renderNetwork('/network?tab=openings');
    await user.click(await screen.findByRole('button', { name: /^Edit / }));
    const dialog = screen.getByRole('dialog', { name: 'Edit opening' });
    expect(within(dialog).queryByRole('radio', { name: 'IP not on the map' })).not.toBeInTheDocument();
  });

  it('shows external hosts as such and switches selected hosts in bulk', async () => {
    mocked.hosts.mockResolvedValue([
      ...HOSTS,
      makeHost({ id: 3, name: 'knox', ip: '172.20.0.5', region_id: null, region_name: null, kind: 'external' }),
    ]);
    mocked.updateHost
      .mockResolvedValueOnce(makeHost({ kind: 'external' }))
      .mockRejectedValueOnce({
        isAxiosError: true,
        response: { status: 422, data: { detail: 'A host linked to a monitored server is ours' } },
      });
    const user = userEvent.setup();
    renderNetwork('/network?tab=hosts');

    const table = await screen.findByRole('table', { name: 'Hosts' });
    expect(within(table).getByText('External host')).toBeInTheDocument();
    expect(within(table).queryByText('No region')).not.toBeInTheDocument();

    await user.click(within(table).getByRole('checkbox', { name: 'Select app-1' }));
    await user.click(within(table).getByRole('checkbox', { name: 'Select db-1' }));
    await user.click(within(table).getByRole('checkbox', { name: 'Select knox' }));
    await user.click(screen.getByRole('button', { name: 'Mark external' }));

    // knox is external already: only the other two are sent.
    await waitFor(() => expect(mocked.updateHost).toHaveBeenCalledTimes(2));
    expect(mocked.updateHost).toHaveBeenCalledWith(1, { kind: 'external' });
    expect(mocked.updateHost).toHaveBeenCalledWith(2, { kind: 'external' });
    expect(await screen.findByText('1 hosts could not be switched')).toBeInTheDocument();
    expect(screen.getByText(/db-1: A host linked to a monitored server is ours/)).toBeInTheDocument();
    // The refused host stays selected for another try.
    expect(within(table).getByRole('checkbox', { name: 'Select db-1' })).toBeChecked();
    expect(within(table).getByRole('checkbox', { name: 'Select app-1' })).not.toBeChecked();
  });

  it('an external host in the form has no region or monitored link', async () => {
    mocked.createHost.mockResolvedValue(makeHost({ kind: 'external' }));
    const user = userEvent.setup();
    renderNetwork('/network?tab=hosts');

    await user.click(await screen.findByRole('button', { name: 'Add host' }));
    const dialog = screen.getByRole('dialog', { name: 'Add host' });
    expect(within(dialog).getByRole('combobox', { name: 'Region' })).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Kind' }), 'external');
    expect(within(dialog).queryByRole('combobox', { name: 'Region' })).not.toBeInTheDocument();
    await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'knox');
    await user.type(within(dialog).getByRole('textbox', { name: 'IP' }), '172.20.0.5');
    await user.click(within(dialog).getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mocked.createHost).toHaveBeenCalledWith(expect.objectContaining({
      name: 'knox', ip: '172.20.0.5', kind: 'external', monitored_host_id: null,
    })));
    expect(mocked.createHost.mock.calls[0][0]).not.toHaveProperty('region_id');
  });

  it('the openings CSV import can add missing destinations', async () => {
    const csv = 'src,dst,ports\na1,172.20.0.5,443';
    mocked.importCsv.mockResolvedValue({
      kind: 'openings', dry_run: true, created: 1, skipped: 0, errors: 0,
      rows: [{ line: 2, action: 'create', key: 'a1 → dst-172.20.0.5 tcp/443', message: 'open · adds external host dst-172.20.0.5' }],
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=openings');

    await user.click(await screen.findByRole('button', { name: 'Import CSV' }));
    const dialog = screen.getByRole('dialog', { name: /from CSV/ });
    await user.click(within(dialog).getByRole('textbox', { name: 'Or paste CSV text' }));
    await user.paste(csv);
    await user.click(within(dialog).getByRole('checkbox', { name: /^Add destination IPs no host has/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Preview (dry run)' }));

    await waitFor(() => expect(mocked.importCsv).toHaveBeenCalledWith({
      kind: 'openings', csv, dry_run: true, create_missing_destinations: true,
    }));
    expect(await within(dialog).findByText(/adds external host dst-172.20.0.5/)).toBeInTheDocument();
  });

  it('says why access to an external host cannot be judged', async () => {
    mocked.check.mockResolvedValue({
      status: 'unknown', reason: 'external_host', message: '',
      src: { input: 'app-1', host_id: 1, host_name: 'app-1', ip: '10.1.0.5', region_id: 1, region_name: 'seoul' },
      dst: { input: 'knox', host_id: 3, host_name: 'knox', ip: '172.20.0.5', region_id: null, region_name: null },
      port: 25, protocol: 'tcp', openings: [], usages: [], request_draft: null, probe: null,
    });
    const user = userEvent.setup();
    renderNetwork('/network?tab=check', READ);
    await user.type(await screen.findByRole('combobox', { name: 'Source' }), 'app-1');
    await user.type(screen.getByRole('combobox', { name: 'Destination' }), 'knox');
    await user.type(screen.getByRole('spinbutton', { name: 'Port' }), '25');
    await user.click(screen.getByRole('button', { name: 'Check' }));
    expect(await screen.findByText(/An external host is involved/)).toBeInTheDocument();
  });
});
