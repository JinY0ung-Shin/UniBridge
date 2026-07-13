vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getServers: vi.fn(),
  getServerMetrics: vi.fn(),
}));

const rechartsCapture = vi.hoisted(() => ({
  tooltips: [] as Array<(value: unknown, name?: unknown, item?: unknown) => unknown>,
  chartData: [] as unknown[][],
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div data-testid="responsive-chart">{children}</div>,
  LineChart: ({ children, data }: { children: React.ReactNode; data: unknown[] }) => {
    rechartsCapture.chartData.push(data);
    return <div data-testid="line-chart">{children}</div>;
  },
  Line: ({ name }: { name: string }) => <span data-testid="chart-line">{name}</span>,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Legend: () => <span data-testid="chart-legend" />,
  Tooltip: ({ formatter }: { formatter: (value: unknown, name?: unknown, item?: unknown) => unknown }) => {
    rechartsCapture.tooltips.push(formatter);
    return null;
  },
}));

const navigateMock = vi.hoisted(() => vi.fn());

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useParams: () => ({ id: '7' }),
    useNavigate: () => navigateMock,
  };
});

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getServers, getServerMetrics } from '../api/client';
import ServerDetail from '../pages/ServerDetail';
import { renderWithProviders } from './helpers';

const mockGetServers = vi.mocked(getServers);
const mockGetServerMetrics = vi.mocked(getServerMetrics);

describe('ServerDetail page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    rechartsCapture.tooltips.length = 0;
    rechartsCapture.chartData.length = 0;
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

  it('shows loading and metric-query error states', async () => {
    mockGetServers.mockResolvedValue([server]);
    let resolveMetrics!: (value: []) => void;
    mockGetServerMetrics.mockReturnValue(new Promise((resolve) => { resolveMetrics = resolve; }));
    const loading = renderWithProviders(<ServerDetail />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
    resolveMetrics([]);
    expect(await screen.findByText('No metric data in this window.')).toBeInTheDocument();
    loading.unmount();

    mockGetServerMetrics.mockRejectedValueOnce(new Error('prometheus unavailable'));
    renderWithProviders(<ServerDetail />);
    expect(await screen.findByRole('alert')).toHaveTextContent('An error occurred');
  });

  it('switches duration/step and navigates back to the server list', async () => {
    mockGetServers.mockResolvedValue([server]);
    mockGetServerMetrics.mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<ServerDetail />);
    await screen.findByText('edge-node-1');

    await user.click(screen.getByRole('button', { name: '6h' }));
    await waitFor(() => expect(mockGetServerMetrics).toHaveBeenCalledWith(7, { duration: '6h', step: '120s' }));
    expect(screen.getByRole('button', { name: '6h' })).toHaveClass('btn-primary');
    await user.click(screen.getByRole('button', { name: '24h' }));
    await waitFor(() => expect(mockGetServerMetrics).toHaveBeenCalledWith(7, { duration: '24h', step: '300s' }));

    await user.click(screen.getByRole('button', { name: /Servers/ }));
    expect(navigateMock).toHaveBeenCalledWith('/servers');
  });

  it('builds sorted CPU, memory, and multi-disk chart panels', async () => {
    mockGetServers.mockResolvedValue([server]);
    mockGetServerMetrics.mockResolvedValue([
      {
        metric: 'cpu',
        points: [{ t: 2, v: 20 }, { t: 1, v: 10 }],
      },
      {
        metric: 'mem',
        points: [{ t: 1, v: 30 }],
      },
      {
        metric: 'disk',
        mountpoint: '/',
        points: [{
          t: 1,
          v: 50,
          used_bytes: '512' as unknown as number,
          total_bytes: '1024' as unknown as number,
        }],
      },
      {
        metric: 'disk',
        mountpoint: '/data',
        points: [{ t: 1, v: 75, used_bytes: null, total_bytes: 2048 }],
      },
    ]);
    renderWithProviders(<ServerDetail />);

    expect(await screen.findByRole('heading', { name: 'CPU usage (%)' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Memory usage (%)' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Disk usage (%)' })).toBeInTheDocument();
    expect(screen.getByTestId('chart-legend')).toBeInTheDocument();
    expect(screen.getAllByTestId('chart-line').map((line) => line.textContent)).toEqual(['cpu', 'mem', '/', '/data']);
    expect(screen.getByLabelText('Disk capacity')).toHaveTextContent('/: 512 B / 1.0 KiB');
    expect(screen.getByLabelText('Disk capacity')).toHaveTextContent('/data: total 2.0 KiB');
    expect(rechartsCapture.chartData[0].map((point) => (point as { timestamp: number }).timestamp)).toEqual([1, 2]);
  });

  it('formats tooltip percentages and disk capacity details defensively', async () => {
    mockGetServers.mockResolvedValue([server]);
    mockGetServerMetrics.mockResolvedValue([
      {
        metric: 'disk',
        mountpoint: '/',
        points: [{ t: 1, v: 50, used_bytes: 512, total_bytes: 1024 }],
      },
    ]);
    renderWithProviders(<ServerDetail />);
    await screen.findByRole('heading', { name: 'Disk usage (%)' });
    const formatter = rechartsCapture.tooltips.at(-1)!;
    const payload = rechartsCapture.chartData.at(-1)![0] as Record<string, unknown>;

    expect(formatter('not-a-number')).toBe('—');
    expect(formatter(50, undefined, null)).toBe('50.0%');
    expect(formatter(50, undefined, { dataKey: 'missing', payload })).toBe('50.0%');
    expect(formatter(50, undefined, { dataKey: 'value_0', payload })).toBe('50.0% · 512 B / 1.0 KiB');
  });

  it('renders a stable fallback title when the server record is absent', async () => {
    mockGetServers.mockResolvedValue([]);
    mockGetServerMetrics.mockResolvedValue([]);
    renderWithProviders(<ServerDetail />);
    expect(await screen.findByRole('heading', { name: '#7' })).toBeInTheDocument();
    expect(screen.queryByText('10.0.0.7:39100')).not.toBeInTheDocument();
  });
});
