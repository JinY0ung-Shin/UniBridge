vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getServers: vi.fn(),
  createServer: vi.fn(),
  updateServer: vi.fn(),
  deleteServer: vi.fn(),
  testServer: vi.fn(),
  getExternalServices: vi.fn(),
  createExternalService: vi.fn(),
  updateExternalService: vi.fn(),
  deleteExternalService: vi.fn(),
  testExternalService: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  getServers,
  createServer,
  updateServer,
  deleteServer,
  testServer,
  getExternalServices,
} from '../api/client';
import Servers from '../pages/Servers';
import { renderWithProviders } from './helpers';

const mockedGetServers = vi.mocked(getServers);
const mockedCreateServer = vi.mocked(createServer);
const mockedUpdateServer = vi.mocked(updateServer);
const mockedDeleteServer = vi.mocked(deleteServer);
const mockedTestServer = vi.mocked(testServer);
const mockedGetExternalServices = vi.mocked(getExternalServices);

function makeServer(overrides = {}) {
  return {
    id: 1,
    name: 'web-1',
    address: '10.0.0.5:39100',
    enabled: true,
    description: 'Frontend host',
    labels: null,
    disk_mountpoints: null,
    disk_warn_pct: null,
    disk_crit_pct: null,
    cpu_warn_pct: null,
    mem_warn_pct: null,
    status: 'up' as const,
    ...overrides,
  };
}

describe('Servers', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    mockedGetServers.mockReset();
    mockedGetServers.mockResolvedValue([]);
    mockedGetExternalServices.mockReset();
    mockedGetExternalServices.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders loading, empty, and load-error states', async () => {
    let resolveServers!: (value: ReturnType<typeof makeServer>[]) => void;
    mockedGetServers.mockReturnValue(new Promise((resolve) => { resolveServers = resolve; }));
    const loadingRender = renderWithProviders(<Servers />, { permissions: ['servers.read'] });
    await screen.findByText(/No external services registered/i);
    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
    resolveServers([]);
    expect(await screen.findByText(/No servers registered yet/i)).toBeInTheDocument();
    loadingRender.unmount();

    mockedGetServers.mockRejectedValueOnce(new Error('offline'));
    renderWithProviders(<Servers />, { permissions: ['servers.read'] });
    expect(await screen.findByRole('alert')).toHaveTextContent('An error occurred');
  });

  it('renders the external services section', async () => {
    mockedGetExternalServices.mockResolvedValue([
      {
        id: 1,
        name: 'order-api',
        address: '10.0.0.7:8080',
        metrics_path: '/metrics',
        scheme: 'https' as const,
        description: 'Orders backend',
        enabled: true,
        status: 'up' as const,
      },
    ]);

    renderWithProviders(<Servers />, { permissions: ['servers.read'] });

    await waitFor(() => expect(screen.getByText('External services')).toBeInTheDocument());
    expect(await screen.findByText('order-api')).toBeInTheDocument();
    expect(screen.getByText('https://10.0.0.7:8080')).toBeInTheDocument();
  });

  it('filters servers by search text', async () => {
    mockedGetServers.mockResolvedValue([
      makeServer(),
      makeServer({
        id: 2,
        name: 'payments-1',
        address: '10.0.0.8:39100',
        description: 'Payments worker',
        disk_mountpoints: '/data',
      }),
    ]);
    renderWithProviders(<Servers />, { permissions: ['servers.read'] });
    await waitFor(() => expect(screen.getByText('web-1')).toBeInTheDocument());

    const search = screen.getByRole('searchbox', { name: /search servers/i });
    await userEvent.type(search, 'payments');

    expect(screen.queryByText('web-1')).not.toBeInTheDocument();
    expect(screen.getByText('payments-1')).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, 'missing');
    expect(screen.getByText(/No matching servers|일치하는 서버/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Clear search|검색 지우기/i }));
    expect(screen.getByText('web-1')).toBeInTheDocument();
    expect(screen.getByText('payments-1')).toBeInTheDocument();
  });

  it('names row actions by server', async () => {
    mockedGetServers.mockResolvedValue([makeServer()]);

    renderWithProviders(<Servers />, { permissions: ['servers.read', 'servers.write'] });

    await waitFor(() => expect(screen.getByText('web-1')).toBeInTheDocument());

    expect(screen.getByRole('button', { name: 'Open details for web-1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Test web-1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit web-1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete web-1' })).toBeInTheDocument();
  });

  it('connects server form guidance to the relevant fields', async () => {
    renderWithProviders(<Servers />, { permissions: ['servers.read', 'servers.write'] });

    await userEvent.click(screen.getByRole('button', { name: 'Add server' }));

    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveAttribute('id', 'server-name');
    expect(screen.getByRole('textbox', { name: 'node_exporter address' })).toHaveAttribute(
      'aria-describedby',
      'server-address-hint',
    );
    expect(screen.getByText(/host:port of the node_exporter agent/i)).toHaveAttribute('id', 'server-address-hint');

    expect(screen.getByRole('textbox', { name: 'Disk mountpoints' })).toHaveAttribute(
      'aria-describedby',
      'server-disk-mountpoints-hint',
    );
    expect(screen.getByText(/Comma-separated node_exporter mountpoints/i)).toHaveAttribute(
      'id',
      'server-disk-mountpoints-hint',
    );

    const thresholdGroup = screen.getByRole('group', { name: 'Per-host thresholds (optional)' });
    expect(thresholdGroup).toHaveAttribute('aria-describedby', 'server-thresholds-hint');
    expect(screen.getByText(/inherit the global defaults from Alert settings/i)).toHaveAttribute(
      'id',
      'server-thresholds-hint',
    );
    expect(screen.getByRole('spinbutton', { name: 'Disk warn %' })).toHaveAttribute('id', 'server-disk-warn');
  });

  it('creates a trimmed server with numeric and inherited threshold values', async () => {
    mockedCreateServer.mockResolvedValue(makeServer());
    const user = userEvent.setup();
    renderWithProviders(<Servers />, { permissions: ['servers.read', 'servers.write'] });

    await user.click(screen.getByRole('button', { name: 'Add server' }));
    await user.type(screen.getByRole('textbox', { name: 'Name' }), '  edge-1  ');
    await user.type(screen.getByRole('textbox', { name: 'node_exporter address' }), ' 10.0.0.9:39100 ');
    await user.type(screen.getByRole('textbox', { name: 'Description' }), ' edge host ');
    await user.type(screen.getByRole('textbox', { name: 'Disk mountpoints' }), ' /,/data ');
    await user.type(screen.getByRole('spinbutton', { name: 'Disk warn %' }), '80');
    await user.type(screen.getByRole('spinbutton', { name: 'Disk critical %' }), '90');
    await user.type(screen.getByRole('spinbutton', { name: 'CPU warn %' }), '85');
    await user.click(screen.getByRole('checkbox', { name: /Enabled/ }));
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mockedCreateServer).toHaveBeenCalledWith({
      name: 'edge-1',
      address: '10.0.0.9:39100',
      description: 'edge host',
      enabled: false,
      disk_mountpoints: '/,/data',
      disk_warn_pct: 80,
      disk_crit_pct: 90,
      cpu_warn_pct: 85,
      mem_warn_pct: null,
    }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add server' })).not.toBeInTheDocument());
  });

  it('edits a server while preserving its immutable name and normalizing blank values', async () => {
    mockedGetServers.mockResolvedValue([makeServer({
      enabled: false,
      status: 'down',
      disk_mountpoints: '/data',
      disk_warn_pct: 70,
      disk_crit_pct: 91,
      cpu_warn_pct: 82,
      mem_warn_pct: 83,
    })]);
    mockedUpdateServer.mockResolvedValue(makeServer());
    const user = userEvent.setup();
    renderWithProviders(<Servers />, { permissions: ['servers.read', 'servers.write'] });
    await screen.findByText('web-1');

    await user.click(screen.getByRole('button', { name: 'Edit web-1' }));
    expect(screen.getByRole('textbox', { name: 'Name' })).toBeDisabled();
    expect(screen.getByRole('spinbutton', { name: 'Disk warn %' })).toHaveValue(70);
    await user.clear(screen.getByRole('textbox', { name: 'node_exporter address' }));
    await user.type(screen.getByRole('textbox', { name: 'node_exporter address' }), ' new.example:39100 ');
    await user.clear(screen.getByRole('textbox', { name: 'Disk mountpoints' }));
    await user.clear(screen.getByRole('spinbutton', { name: 'Disk warn %' }));
    await user.click(screen.getByRole('checkbox', { name: /Enabled/ }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(mockedUpdateServer).toHaveBeenCalledWith(1, {
      address: 'new.example:39100',
      description: 'Frontend host',
      enabled: true,
      disk_mountpoints: null,
      disk_warn_pct: null,
      disk_crit_pct: 91,
      cpu_warn_pct: 82,
      mem_warn_pct: 83,
    }));
  });

  it('surfaces string and validation-array errors from server mutations', async () => {
    const user = userEvent.setup();
    mockedCreateServer.mockRejectedValueOnce({
      isAxiosError: true,
      response: { data: { detail: 'Server name already exists' } },
    });
    const failedCreateRender = renderWithProviders(<Servers />, { permissions: ['servers.read', 'servers.write'] });
    await user.click(screen.getByRole('button', { name: 'Add server' }));
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'duplicate');
    await user.type(screen.getByRole('textbox', { name: 'node_exporter address' }), 'host:39100');
    await user.click(screen.getByRole('button', { name: 'Create' }));
    expect(await screen.findByText('Server name already exists')).toBeInTheDocument();
    expect(screen.getAllByRole('alert').some((alert) => alert.textContent?.includes('Failed to save server'))).toBe(true);

    failedCreateRender.unmount();
    mockedGetServers.mockResolvedValue([makeServer()]);
    mockedDeleteServer.mockRejectedValueOnce({
      isAxiosError: true,
      response: { data: { detail: [{ msg: 'Server is referenced' }, { nope: true }, { msg: 'Try again' }] } },
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<Servers />, { permissions: ['servers.read', 'servers.write'] });
    await screen.findByText('web-1');
    await user.click(screen.getByRole('button', { name: 'Delete web-1' }));
    expect(await screen.findByText('Server is referenced; Try again')).toBeInTheDocument();
  });

  it('tests, deletes, and opens a server only after the corresponding user action', async () => {
    mockedGetServers.mockResolvedValue([makeServer()]);
    mockedTestServer.mockResolvedValue({ status: 'up', detail: 'node_exporter reachable' });
    mockedDeleteServer.mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    const user = userEvent.setup();
    renderWithProviders(<Servers />, { permissions: ['servers.read', 'servers.write'] });
    await screen.findByText('web-1');

    await user.click(screen.getByRole('button', { name: 'Test web-1' }));
    expect(await screen.findByText('node_exporter reachable')).toBeInTheDocument();
    expect(mockedTestServer).toHaveBeenCalledWith(1);

    await user.click(screen.getByRole('button', { name: 'Delete web-1' }));
    expect(mockedDeleteServer).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Delete web-1' }));
    await waitFor(() => expect(mockedDeleteServer).toHaveBeenCalledWith(1));
    expect(confirm).toHaveBeenCalledTimes(2);

    await user.click(screen.getByRole('button', { name: 'Open details for web-1' }));
    expect(window.location.pathname).toBe('/servers/1');
  });

  it('hides host mutations for read-only users and renders status fallbacks', async () => {
    mockedGetServers.mockResolvedValue([
      makeServer({ id: 1, name: 'disabled-host', enabled: false, status: 'down' }),
      makeServer({ id: 2, name: 'unknown-host', status: null, disk_mountpoints: '' }),
    ]);
    renderWithProviders(<Servers />, { permissions: ['servers.read'] });
    await screen.findByText('disabled-host');

    expect(screen.getByText('disabled')).toHaveClass('status-badge--unknown');
    expect(screen.getByText('unknown')).toHaveClass('status-badge--unknown');
    expect(screen.getAllByText('Global default')).not.toHaveLength(0);
    expect(screen.queryByRole('button', { name: 'Add server' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit disabled-host' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete disabled-host' })).not.toBeInTheDocument();
  });
});
