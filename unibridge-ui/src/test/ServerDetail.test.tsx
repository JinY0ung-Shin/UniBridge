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
  Line: ({ name, stroke, connectNulls }: { name: string; stroke?: string; connectNulls?: boolean }) => (
    <span data-testid="chart-line" data-stroke={stroke} data-connect-nulls={String(Boolean(connectNulls))}>{name}</span>
  ),
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
    gpu_address: null,
    gpu_util_warn_pct: null,
    gpu_mem_warn_pct: null,
    gpu_util_target_pct: null,
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
        points: [{ t: 120, v: 20 }, { t: 60, v: 10 }],
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
    expect(rechartsCapture.chartData[0].map((point) => (point as { timestamp: number }).timestamp)).toEqual([60, 120]);
  });

  it('renders one line per GPU and lists each GPU model once', async () => {
    mockGetServers.mockResolvedValue([{ ...server, gpu_address: '10.0.0.7:39400' }]);
    mockGetServerMetrics.mockResolvedValue([
      { metric: 'gpu_util', gpu: '0', gpu_model: 'NVIDIA A100 80GB', points: [{ t: 1, v: 40 }] },
      { metric: 'gpu_util', gpu: '1', gpu_model: 'NVIDIA A100 80GB', points: [{ t: 1, v: 60 }] },
      { metric: 'gpu_mem', gpu: '0', gpu_model: 'NVIDIA A100 80GB', points: [{ t: 1, v: 25 }] },
      { metric: 'gpu_mem', gpu: '1', gpu_model: 'NVIDIA A100 80GB', points: [{ t: 1, v: 35 }] },
    ]);

    renderWithProviders(<ServerDetail />);

    expect(await screen.findByRole('heading', { name: 'GPU utilization (per GPU)' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'GPU memory usage (per GPU)' })).toBeInTheDocument();
    expect(screen.getAllByTestId('chart-line').map((line) => line.textContent))
      .toEqual(['GPU 0', 'GPU 1', 'GPU 0', 'GPU 1']);
    expect(screen.getAllByLabelText('GPU models').map((el) => el.textContent))
      .toEqual(['NVIDIA A100 80GB', 'NVIDIA A100 80GB']);
    expect(screen.queryByRole('heading', { name: 'CPU usage (%)' })).not.toBeInTheDocument();
  });

  it('falls back to the series index and omits the model caption for unlabelled GPUs', async () => {
    mockGetServers.mockResolvedValue([server]);
    mockGetServerMetrics.mockResolvedValue([
      { metric: 'gpu_util', points: [{ t: 1, v: 40 }] },
    ]);

    renderWithProviders(<ServerDetail />);

    expect(await screen.findByRole('heading', { name: 'GPU utilization (per GPU)' })).toBeInTheDocument();
    expect(screen.getByTestId('chart-line')).toHaveTextContent('GPU 0');
    expect(screen.queryByLabelText('GPU models')).not.toBeInTheDocument();
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

  type GridRow = { timestamp: number; time: string; value_0: number | null };

  async function renderCpuAt(nowSeconds: number, points: Array<{ t: number; v: number }>) {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(nowSeconds * 1000);
    try {
      mockGetServers.mockResolvedValue([server]);
      mockGetServerMetrics.mockResolvedValue([{ metric: 'cpu', points }]);
      renderWithProviders(<ServerDetail />);
      await screen.findByRole('heading', { name: 'CPU usage (%)' });
      return rechartsCapture.chartData.at(-1) as GridRow[];
    } finally {
      vi.useRealTimers();
    }
  }

  it('puts every step of the window on the axis, with nulls where samples are missing', async () => {
    // 1h window ending at 01:00 UTC, 60s step: samples only at 00:00, 00:01, 00:10.
    const rows = await renderCpuAt(3600, [{ t: 0, v: 10 }, { t: 60, v: 11 }, { t: 600, v: 12 }]);

    expect(rows).toHaveLength(61); // 00:00 … 01:00, one row per step
    expect(rows.map((row) => row.timestamp)).toEqual(Array.from({ length: 61 }, (_, i) => i * 60));
    expect(rows.slice(0, 3).map((row) => row.value_0)).toEqual([10, 11, null]);
    expect(rows[10].value_0).toBe(12);
    // Down since 00:11: the end of the window is empty, not cut off.
    expect(rows.slice(11).every((row) => row.value_0 === null)).toBe(true);
    expect(rows[0].time).toBe('09:00'); // epoch 0 = 09:00 KST, whatever the browser's zone
    expect(screen.getByTestId('chart-line')).toHaveAttribute('data-connect-nulls', 'false');
  });

  it('shows an outage at the start of the window', async () => {
    // Host came back at 00:50; the window starts at 00:00.
    const rows = await renderCpuAt(3600, [{ t: 3000, v: 5 }, { t: 3060, v: 6 }]);
    expect(rows[0].timestamp).toBe(0);
    expect(rows).toHaveLength(61);
    expect(rows.filter((row) => row.value_0 !== null)).toHaveLength(2);
  });

  it('fills only the holes between samples when the browser clock is far off', async () => {
    const rows = await renderCpuAt(10 * 86400, [{ t: 0, v: 1 }, { t: 180, v: 2 }]);
    expect(rows.map((row) => [row.timestamp, row.value_0])).toEqual([[0, 1], [60, null], [120, null], [180, 2]]);
  });

  it('gives each of eight GPUs its own colour', async () => {
    const root = document.documentElement.style;
    const accents = { '--accent-yellow': '#eab308', '--accent-blue': '#3b82f6', '--accent-green': '#22c55e', '--accent-red': '#ef4444' };
    for (const [name, value] of Object.entries(accents)) root.setProperty(name, value);
    mockGetServers.mockResolvedValue([{ ...server, gpu_address: '10.0.0.7:39400' }]);
    mockGetServerMetrics.mockResolvedValue(
      Array.from({ length: 8 }, (_, gpu) => ({
        metric: 'gpu_util' as const, gpu: String(gpu), points: [{ t: 1, v: gpu }],
      })),
    );
    renderWithProviders(<ServerDetail />);
    await screen.findByRole('heading', { name: 'GPU utilization (per GPU)' });

    const strokes = screen.getAllByTestId('chart-line').map((line) => line.getAttribute('data-stroke'));
    expect(strokes).toHaveLength(8);
    expect(new Set(strokes).size).toBe(8);
    for (const name of Object.keys(accents)) root.removeProperty(name);
  });

  it('says Prometheus is unreachable when the metrics call returns 502', async () => {
    const { AxiosError } = await import('axios');
    mockGetServers.mockResolvedValue([server]);
    mockGetServerMetrics.mockRejectedValue(
      new AxiosError('Bad Gateway', 'ERR_BAD_RESPONSE', undefined, undefined, {
        status: 502, data: { detail: 'Prometheus is unreachable' },
      } as never),
    );
    renderWithProviders(<ServerDetail />);
    expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toHaveTextContent(
      'Prometheus is unreachable, so metrics could not be loaded.',
    );
  });

  it('keeps the generic error for any other 502 (e.g. from a proxy)', async () => {
    const { AxiosError } = await import('axios');
    mockGetServers.mockResolvedValue([server]);
    mockGetServerMetrics.mockRejectedValue(
      new AxiosError('Bad Gateway', 'ERR_BAD_RESPONSE', undefined, undefined, {
        status: 502, data: '<html>502 Bad Gateway</html>',
      } as never),
    );
    renderWithProviders(<ServerDetail />);
    expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toHaveTextContent('An error occurred');
  });
});
