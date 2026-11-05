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
  getRegions,
  verifyFirewallOpening,
  type FirewallOpening,
  type NetworkHost,
  type PortUsage,
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
