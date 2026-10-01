vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div data-testid="responsive-container">{children}</div>,
  // Rows and per-line/tooltip props are exposed so tests can check labels,
  // null gaps (no connectNulls) and the tooltip's full point time.
  LineChart: ({ children, data }: { children: React.ReactNode; data?: unknown }) => (
    <div data-testid="line-chart" data-points={JSON.stringify(data ?? [])}>{children}</div>
  ),
  BarChart: ({ children, data }: { children: React.ReactNode; data?: unknown }) => (
    <div data-testid="bar-chart" data-points={JSON.stringify(data ?? [])}>{children}</div>
  ),
  Line: ({ dataKey, connectNulls, dot }: { dataKey: string; connectNulls?: boolean; dot?: unknown }) => (
    <span
      data-testid={`line-${dataKey}`}
      data-connect-nulls={String(Boolean(connectNulls))}
      data-dot={typeof dot}
    />
  ),
  Bar: () => null,
  // The axis key and a tick probed at epoch 0 (= 1/1 09:00 KST) are exposed so
  // tests can check charts key on `ts` and tick span/bucket-aware.
  XAxis: ({ dataKey, tickFormatter }: { dataKey?: unknown; tickFormatter?: (value: unknown, index: number) => unknown }) => (
    <span data-testid="x-axis" data-key={String(dataKey)} data-tick={tickFormatter ? String(tickFormatter(0, 0)) : ''} />
  ),
  YAxis: () => null,
  CartesianGrid: () => null,
  // Tooltip labels are probed at epoch 0 (= 1/1 09:00 KST): the point time, or
  // the bucket period on calendar-bucket bars.
  Tooltip: ({ labelFormatter }: { labelFormatter?: (label: unknown, payload: unknown[]) => unknown }) => (
    <span data-testid="tooltip" data-label={labelFormatter ? String(labelFormatter(0, [])) : ''} />
  ),
  Legend: () => null,
  Cell: () => null,
  LabelList: () => null,
}));

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getExternalSummary: vi.fn(),
  getExternalRequests: vi.fn(),
  getExternalRequestsTotal: vi.fn(),
  getExternalStatusCodes: vi.fn(),
  getExternalLatency: vi.fn(),
  getExternalServicesComparison: vi.fn(),
  getExternalServicesComparisonSeries: vi.fn(),
  getExternalHandlersComparison: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getExternalSummary,
  getExternalRequests,
  getExternalRequestsTotal,
  getExternalStatusCodes,
  getExternalLatency,
  getExternalServicesComparison,
  getExternalServicesComparisonSeries,
  getExternalHandlersComparison,
} from '../api/client';
import ExternalMonitoring from '../pages/ExternalMonitoring';
import { renderWithProviders } from './helpers';

const mockedSummary = vi.mocked(getExternalSummary);
const mockedRequests = vi.mocked(getExternalRequests);
const mockedRequestsTotal = vi.mocked(getExternalRequestsTotal);
const mockedStatusCodes = vi.mocked(getExternalStatusCodes);
const mockedLatency = vi.mocked(getExternalLatency);
const mockedComparison = vi.mocked(getExternalServicesComparison);
const mockedComparisonSeries = vi.mocked(getExternalServicesComparisonSeries);
const mockedHandlers = vi.mocked(getExternalHandlersComparison);

describe('ExternalMonitoring', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSummary.mockResolvedValue({ total_requests: 0, error_rate: 0, avg_latency_ms: 0 });
    mockedRequests.mockResolvedValue([]);
    mockedRequestsTotal.mockResolvedValue([]);
    mockedStatusCodes.mockResolvedValue([]);
    mockedLatency.mockResolvedValue({ p50: [], p95: [], p99: [] });
    mockedComparison.mockResolvedValue({ total_requests: 0, services: [] });
    mockedComparisonSeries.mockResolvedValue({ buckets: [], series: [], unit: 'requests' });
    mockedHandlers.mockResolvedValue({ total_requests: 0, handlers: [] });
  });

  it('labels request-rate points with the date on multi-day ranges', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    const ts = Date.UTC(2026, 8, 24, 5, 0, 0) / 1000; // 2026-09-24 14:00 KST
    mockedRequests.mockResolvedValue([{ timestamp: ts, value: 1.5 }]);

    renderWithProviders(<ExternalMonitoring />);

    await user.click(screen.getByRole('button', { name: '7d' }));

    await waitFor(() => {
      const points = JSON.parse(screen.getByTestId('line-chart').getAttribute('data-points') ?? '[]');
      expect(points).toEqual([{ ts, rps: 1.5 }]);
    });
    const chart = screen.getByTestId('line-chart');
    expect(within(chart).getByTestId('x-axis')).toHaveAttribute('data-key', 'ts');
    expect(within(chart).getByTestId('x-axis')).toHaveAttribute('data-tick', '1/1 09h');
    expect(within(chart).getByTestId('tooltip')).toHaveAttribute('data-label', '1/1 09:00');
  });

  it('keys volume bars on the epoch; calendar buckets name the period on hover', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    const ts = Date.UTC(2026, 8, 24, 5, 0, 0) / 1000;
    mockedRequestsTotal.mockResolvedValue([{ timestamp: ts, value: 41.6 }]);

    renderWithProviders(<ExternalMonitoring />);

    const volume = await screen.findByTestId('bar-chart');
    expect(JSON.parse(volume.getAttribute('data-points') ?? '[]')).toEqual([{ ts, requests: 42 }]);
    expect(within(volume).getByTestId('x-axis')).toHaveAttribute('data-key', 'ts');
    expect(within(volume).getByTestId('x-axis')).toHaveAttribute('data-tick', '09:00');
    expect(within(volume).getByTestId('tooltip')).toHaveAttribute('data-label', '1/1 09:00');

    await user.click(screen.getByRole('button', { name: 'Daily' }));

    // A day bar is a period: tick and tooltip both carry the bucket label.
    await waitFor(() => {
      const bars = screen.getByTestId('bar-chart');
      expect(within(bars).getByTestId('x-axis')).toHaveAttribute('data-tick', '1/1');
      expect(within(bars).getByTestId('tooltip')).toHaveAttribute('data-label', '1/1');
    });
  });

  it('keeps null latency points as gaps without bridging them', async () => {
    const ts = Date.UTC(2026, 8, 24, 5, 0, 0) / 1000;
    mockedLatency.mockResolvedValue({
      p50: [{ timestamp: ts, value: null }, { timestamp: ts + 60, value: 8 }],
      p95: [{ timestamp: ts, value: null }, { timestamp: ts + 60, value: 20 }],
      p99: [{ timestamp: ts, value: null }, { timestamp: ts + 60, value: null }],
    });

    renderWithProviders(<ExternalMonitoring />);

    await waitFor(() => {
      const points = JSON.parse(screen.getByTestId('line-chart').getAttribute('data-points') ?? '[]');
      expect(points).toEqual([
        { ts, p50: null, p95: null, p99: null },
        { ts: ts + 60, p50: 8, p95: 20, p99: null },
      ]);
    });
    const chart = screen.getByTestId('line-chart');
    for (const key of ['p50', 'p95', 'p99']) {
      const line = within(chart).getByTestId(`line-${key}`);
      expect(line).toHaveAttribute('data-connect-nulls', 'false');
      expect(line).toHaveAttribute('data-dot', 'function');
    }
    expect(within(chart).getByTestId('tooltip')).toHaveAttribute('data-label', '1/1 09:00');
  });

  it('shows the no-latency-data state when every quantile point is null', async () => {
    const ts = Date.UTC(2026, 8, 24, 5, 0, 0) / 1000;
    mockedLatency.mockResolvedValue({
      p50: [{ timestamp: ts, value: null }],
      p95: [{ timestamp: ts, value: null }],
      p99: [{ timestamp: ts, value: null }],
    });

    renderWithProviders(<ExternalMonitoring />);

    expect(await screen.findByText('No latency data available')).toBeInTheDocument();
    expect(screen.queryByTestId('line-chart')).not.toBeInTheDocument();
  });

  it('renders loading state', () => {
    mockedSummary.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<ExternalMonitoring />);

    expect(screen.getByText('Loading metrics...')).toBeInTheDocument();
  });

  it('replaces the loading state with a clear error when the summary request fails', async () => {
    mockedSummary.mockRejectedValue(new Error('not deployed'));

    renderWithProviders(<ExternalMonitoring />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Failed to load metrics. Is Prometheus running?',
    );
    expect(screen.queryByText('Loading metrics...')).not.toBeInTheDocument();
  });

  it('renders summary cards and panel titles', async () => {
    mockedSummary.mockResolvedValue({ total_requests: 4321, error_rate: 0.4, avg_latency_ms: 87 });

    renderWithProviders(<ExternalMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('4,321')).toBeInTheDocument();
    });
    expect(screen.getByText('0.4%')).toBeInTheDocument();
    expect(screen.getByText('87ms')).toBeInTheDocument();
    expect(screen.getByText('Request Rate (req/s)')).toBeInTheDocument();
    expect(screen.getByText('Request Count (per interval)')).toBeInTheDocument();
    expect(screen.getByText('Status Code Distribution (1h)')).toBeInTheDocument();
    expect(screen.getByText('Service Comparison (1h total)')).toBeInTheDocument();
  });

  it('renders service comparison rows and populates the filter from them', async () => {
    mockedComparison.mockResolvedValue({
      total_requests: 300,
      services: [
        { service: 'order-api', requests: 200, share: 66.7, error_rate: 0.5, latency_p50_ms: 12, latency_p95_ms: 44 },
        { service: 'billing-api', requests: 100, share: 33.3, error_rate: 6.1, latency_p50_ms: null, latency_p95_ms: null },
      ],
    });

    renderWithProviders(<ExternalMonitoring />);

    // Service names appear both as table cells and as filter <option>s.
    await waitFor(() => {
      expect(screen.getAllByText('order-api').length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText('billing-api').length).toBeGreaterThan(0);
    expect(screen.getByRole('option', { name: 'order-api' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'billing-api' })).toBeInTheDocument();
  });

  it('passes the selected service to metric calls and shows the filtered note', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedComparison.mockResolvedValue({
      total_requests: 100,
      services: [
        { service: 'order-api', requests: 100, share: 100, error_rate: 0, latency_p50_ms: 10, latency_p95_ms: 20 },
      ],
    });

    renderWithProviders(<ExternalMonitoring />);

    const select = await screen.findByLabelText(/Service/i) as HTMLSelectElement;
    await screen.findByRole('option', { name: 'order-api' });
    await user.selectOptions(select, 'order-api');

    await waitFor(() => {
      const calls = mockedSummary.mock.calls;
      expect(calls.some((args) => args[1] === 'order-api')).toBe(true);
    });
    expect(
      screen.getByText("Filtered to 'order-api'. Clear the service filter to compare all services."),
    ).toBeInTheDocument();
  });

  it('opens the endpoint drill-down when a service row is clicked', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedComparison.mockResolvedValue({
      total_requests: 100,
      services: [
        { service: 'order-api', requests: 100, share: 100, error_rate: 0, latency_p50_ms: 10, latency_p95_ms: 20 },
      ],
    });
    mockedHandlers.mockResolvedValue({
      total_requests: 100,
      handlers: [
        { handler: '/orders/{id}', requests: 80, share: 80, error_rate: 0.5, latency_p50_ms: 12, latency_p95_ms: 30 },
        { handler: '/orders', requests: 20, share: 20, error_rate: 0, latency_p50_ms: null, latency_p95_ms: null },
      ],
    });

    renderWithProviders(<ExternalMonitoring />);

    const row = await screen.findByRole('button', { name: 'Open endpoint details for order-api' });
    await user.click(row);

    expect(await screen.findByText('Endpoint breakdown — order-api')).toBeInTheDocument();
    expect(screen.getByText('/orders/{id}')).toBeInTheDocument();
    expect(screen.getByText('/orders')).toBeInTheDocument();
    await waitFor(() => {
      expect(mockedHandlers).toHaveBeenCalledWith(expect.anything(), 'order-api');
    });

    // Clicking again closes the panel.
    await user.click(row);
    expect(screen.queryByText('Endpoint breakdown — order-api')).not.toBeInTheDocument();
  });
});
