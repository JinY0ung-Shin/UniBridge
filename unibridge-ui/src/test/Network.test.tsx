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
}));

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  checkNetworkAccess,
  createFirewallOpening,
  getFirewallOpenings,
  getNetworkHost,
  getNetworkHosts,
  getNetworkOverview,
  getPortUsages,
  getNetworkProbes,
  getRegions,
  importNetworkCsv,
  verifyFirewallOpening,
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
      unassigned_hosts: 2,
      opening_count: 5,
      unverified_count: 1,
      unused_openings: 1,
      unreachable_usages: 0,
      probe_mismatches: 0,
      platform_host_id: 1,
      auto_sources_failed: ['gateway_upstream'],
    });
    mocked.openings.mockResolvedValue([
      makeOpening(),
      makeOpening({ id: 11, src_region_id: 2, src_region_name: 'busan', dst_region_id: 1, dst_region_name: 'seoul', dst_host_name: 'web-9' }),
    ]);
    const user = userEvent.setup();
    renderNetwork('/network', READ);

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
      usages: [makeUsage({ id: null, source: 'auto', auto_kind: 'db', reachable_from_platform: false })],
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
    unassigned_hosts: 0,
    opening_count: 0,
    unverified_count: 0,
    unused_openings: 0,
    unreachable_usages: 0,
    probe_mismatches: 0,
    platform_host_id: 1,
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
    expect(within(toggle).getByRole('button', { name: 'Matrix' })).toHaveAttribute('aria-pressed', 'true');
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
      usages: [],
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
