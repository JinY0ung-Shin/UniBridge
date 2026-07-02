vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getServers: vi.fn(),
  getServerMetrics: vi.fn(),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useParams: () => ({ id: '7' }),
    useNavigate: () => vi.fn(),
  };
});

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getServers, getServerMetrics } from '../api/client';
import ServerDetail from '../pages/ServerDetail';
import { renderWithProviders } from './helpers';

const mockGetServers = vi.mocked(getServers);
const mockGetServerMetrics = vi.mocked(getServerMetrics);

describe('ServerDetail page', () => {
  beforeEach(() => {
    mockGetServers.mockReset();
    mockGetServerMetrics.mockReset();
  });

  const server = {
    id: 7,
    name: 'edge-node-1',
    address: '10.0.0.7:39100',
    enabled: true,
    description: '',
    labels: null,
    disk_mountpoints: null,
    disk_warn_pct: null,
    disk_crit_pct: null,
    cpu_warn_pct: null,
    mem_warn_pct: null,
    status: 'up' as const,
  };

  it('shows an empty state when no metric series are returned', async () => {
    mockGetServers.mockResolvedValue([server]);
    mockGetServerMetrics.mockResolvedValue([]);

    renderWithProviders(<ServerDetail />);

    await waitFor(() => {
      expect(screen.getByText('edge-node-1')).toBeInTheDocument();
    });
    expect(screen.getByText('No metric data in this window.')).toBeInTheDocument();
    expect(mockGetServerMetrics).toHaveBeenCalledWith(7, { duration: '1h', step: '60s' });
  });

  it('shows disk capacity next to disk usage charts', async () => {
    mockGetServers.mockResolvedValue([server]);
    mockGetServerMetrics.mockResolvedValue([
      {
        metric: 'disk',
        mountpoint: '/',
        points: [
          {
            t: 1,
            v: 50,
            used_bytes: 549755813888,
            available_bytes: 549755813888,
            total_bytes: 1099511627776,
          },
        ],
      },
    ]);

    renderWithProviders(<ServerDetail />);

    expect(await screen.findByText('edge-node-1')).toBeInTheDocument();
    expect(screen.getByLabelText('Disk capacity')).toHaveTextContent('/: 512.0 GiB / 1.0 TiB');
  });
});
