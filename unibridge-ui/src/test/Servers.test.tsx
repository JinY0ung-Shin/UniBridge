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
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getServers, getExternalServices } from '../api/client';
import Servers from '../pages/Servers';
import { renderWithProviders } from './helpers';

const mockedGetServers = vi.mocked(getServers);
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
    mockedGetServers.mockReset();
    mockedGetServers.mockResolvedValue([]);
    mockedGetExternalServices.mockReset();
    mockedGetExternalServices.mockResolvedValue([]);
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
});
