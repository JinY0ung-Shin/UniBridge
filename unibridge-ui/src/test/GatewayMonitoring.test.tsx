vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div data-testid="responsive-container">{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>,
  BarChart: ({ children }: { children: React.ReactNode }) => <div data-testid="bar-chart">{children}</div>,
  Line: () => null,
  Bar: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
  Cell: () => null,
  LabelList: () => null,
}));

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getMetricsSummary: vi.fn(),
  getMetricsRequests: vi.fn(),
  getMetricsStatusCodes: vi.fn(),
  getMetricsLatency: vi.fn(),
  getMetricsRoutesComparison: vi.fn(),
  getMetricsConsumersComparison: vi.fn(),
  getRoutesComparisonSeries: vi.fn(),
  getConsumersComparisonSeries: vi.fn(),
  getMetricsRequestsTotal: vi.fn(),
  getApiKeys: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsRoutesComparison,
  getMetricsConsumersComparison,
  getRoutesComparisonSeries,
  getConsumersComparisonSeries,
  getMetricsRequestsTotal,
  getApiKeys,
} from '../api/client';
import GatewayMonitoring from '../pages/GatewayMonitoring';
import { renderWithProviders, VIEWER_PERMISSIONS } from './helpers';

const mockedGetMetricsSummary = vi.mocked(getMetricsSummary);
const mockedGetMetricsRequests = vi.mocked(getMetricsRequests);
const mockedGetMetricsStatusCodes = vi.mocked(getMetricsStatusCodes);
const mockedGetMetricsLatency = vi.mocked(getMetricsLatency);
const mockedGetMetricsRoutesComparison = vi.mocked(getMetricsRoutesComparison);
const mockedGetMetricsConsumersComparison = vi.mocked(getMetricsConsumersComparison);
const mockedGetRoutesComparisonSeries = vi.mocked(getRoutesComparisonSeries);
const mockedGetConsumersComparisonSeries = vi.mocked(getConsumersComparisonSeries);
const mockedGetMetricsRequestsTotal = vi.mocked(getMetricsRequestsTotal);
const mockedGetApiKeys = vi.mocked(getApiKeys);
const emptyBucketedRequests = { buckets: [], series: [], unit: 'requests' as const };

describe('GatewayMonitoring', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetMetricsSummary.mockResolvedValue({ total_requests: 0, error_rate: 0, avg_latency_ms: 0 });
    mockedGetMetricsRequests.mockResolvedValue([]);
    mockedGetMetricsStatusCodes.mockResolvedValue([]);
    mockedGetMetricsLatency.mockResolvedValue({ p50: [], p95: [], p99: [] });
    mockedGetMetricsRoutesComparison.mockResolvedValue({ total_requests: 0, routes: [] });
    mockedGetMetricsConsumersComparison.mockResolvedValue({ total_requests: 0, consumers: [] });
    mockedGetRoutesComparisonSeries.mockResolvedValue(emptyBucketedRequests);
    mockedGetConsumersComparisonSeries.mockResolvedValue(emptyBucketedRequests);
    mockedGetMetricsRequestsTotal.mockResolvedValue([]);
    mockedGetApiKeys.mockResolvedValue([]);
  });

  it('renders loading state', async () => {
    mockedGetMetricsSummary.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<GatewayMonitoring />);

    expect(screen.getByText('Loading metrics...')).toBeInTheDocument();
  });

  it('renders summary cards with metrics', async () => {
    mockedGetMetricsSummary.mockResolvedValue({
      total_requests: 1234,
      error_rate: 2.5,
      avg_latency_ms: 45,
    });

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('1,234')).toBeInTheDocument();
    });

    expect(screen.getByText('2.5%')).toBeInTheDocument();
    expect(screen.getByText('45ms')).toBeInTheDocument();
  });

  it('renders time range toggle buttons', () => {
    renderWithProviders(<GatewayMonitoring />);

    expect(screen.getByRole('button', { name: '15m' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '1h' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '6h' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '24h' })).toBeInTheDocument();
  });

  it('renders the custom range toggle', () => {
    renderWithProviders(<GatewayMonitoring />);
    expect(screen.getByTestId('custom-toggle')).toBeInTheDocument();
  });

  it('renders chart panel titles', async () => {
    mockedGetMetricsSummary.mockResolvedValue({
      total_requests: 100,
      error_rate: 0,
      avg_latency_ms: 10,
    });

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('Total Requests (1h)')).toBeInTheDocument();
    });

    expect(screen.getByText('Request Rate (req/s)')).toBeInTheDocument();
    expect(screen.getByText('Request Count (per interval)')).toBeInTheDocument();
    expect(screen.getByText('Status Code Distribution (1h)')).toBeInTheDocument();
    expect(screen.getByText('Latency (ms)')).toBeInTheDocument();
    expect(screen.getByText('Route Comparison (1h total)')).toBeInTheDocument();
  });

  it('keeps a custom range when picking a day bucket, but nudges presets', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();

    renderWithProviders(<GatewayMonitoring />);

    // Preset selection: picking Daily nudges the range to 7d.
    await user.click(screen.getByRole('button', { name: 'Daily' }));
    expect(screen.getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'true');

    // Custom selection: picking Daily again must NOT clobber the custom range.
    await user.click(screen.getByTestId('custom-toggle'));
    await user.click(screen.getByTestId('custom-apply'));
    expect(screen.getByTestId('custom-clear')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Weekly' }));
    expect(screen.getByTestId('custom-clear')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '30d' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('explains the hidden API key comparison while a key filter is active', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();

    mockedGetApiKeys.mockResolvedValue([
      { name: 'alice', description: '', api_key: null, key_created: true, allowed_databases: [], allowed_routes: [], rate_limit_per_minute: null, owner: null, created_at: null },
    ]);

    renderWithProviders(<GatewayMonitoring />);

    const select = await screen.findByLabelText(/API Key/i) as HTMLSelectElement;
    await screen.findByRole('option', { name: 'alice' });
    await user.selectOptions(select, 'alice');

    expect(
      screen.getByText("Filtered to 'alice'. Clear the API key filter to compare all keys."),
    ).toBeInTheDocument();
  });

  it('shows partial load feedback when bucketed comparison series fail', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedGetRoutesComparisonSeries.mockRejectedValue(new Error('series failed'));

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('Total Requests (1h)')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: 'Daily' }));

    await waitFor(() => {
      expect(screen.getByText('Some metrics failed to load. Data may be incomplete.')).toBeInTheDocument();
    });
  });

  it('renders route comparison table with all columns', async () => {
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 1500,
      routes: [
        { route: 'route-a', requests: 1000, share: 66.67, error_rate: 1.0, latency_p50_ms: 42.5, latency_p95_ms: 180.0 },
        { route: 'route-b', requests: 500, share: 33.33, error_rate: 0.0, latency_p50_ms: 30.0, latency_p95_ms: 60.0 },
      ],
    });

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('route-a')).toBeInTheDocument();
    });
    expect(screen.getByText('route-b')).toBeInTheDocument();
  });

  it('opens route detail from the keyboard and exposes an accessible close button', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 100,
      routes: [
        { route: 'route-a', name: 'Orders Route', requests: 100, share: 100, error_rate: 0, latency_p50_ms: 10, latency_p95_ms: 20 },
      ],
    });

    renderWithProviders(<GatewayMonitoring />);

    const routeRow = await screen.findByRole('button', { name: 'Open route details for Orders Route' });
    routeRow.focus();
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(routeRow).toHaveAttribute('aria-pressed', 'true');
    });
    expect(screen.getByRole('button', { name: 'Close route details' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Close route details' }));
    expect(routeRow).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows route-detail load failure when the selected route summary fails', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedGetMetricsSummary
      .mockResolvedValueOnce({ total_requests: 0, error_rate: 0, avg_latency_ms: 0 })
      .mockRejectedValueOnce(new Error('route summary failed'));
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 100,
      routes: [
        { route: 'route-a', requests: 100, share: 100, error_rate: 0, latency_p50_ms: 10, latency_p95_ms: 20 },
      ],
    });

    renderWithProviders(<GatewayMonitoring />);

    await user.click(await screen.findByRole('button', { name: 'Open route details for route-a' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to load metrics. Is Prometheus running?')).toBeInTheDocument();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load metrics. Is Prometheus running?');
  });

  it('renders em-dash for null latency', async () => {
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 100,
      routes: [
        { route: 'x', requests: 100, share: 100, error_rate: 0, latency_p50_ms: null, latency_p95_ms: null },
      ],
    });

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('x')).toBeInTheDocument();
    });
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('applies red heatmap class when error rate >= 5%', async () => {
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 100,
      routes: [
        { route: 'hot', requests: 100, share: 100, error_rate: 7.5, latency_p50_ms: 10, latency_p95_ms: 20 },
      ],
    });

    const { container } = renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('hot')).toBeInTheDocument();
    });
    const redCells = container.querySelectorAll('.heatmap-cell--red');
    expect(redCells.length).toBeGreaterThan(0);
  });

  it('shows route name when provided and falls back to id otherwise', async () => {
    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 200,
      routes: [
        { route: 'abc-uuid-1', name: 'User Service', requests: 100, share: 50, error_rate: 0, latency_p50_ms: 10, latency_p95_ms: 20 },
        { route: 'no-name-id', name: null, requests: 100, share: 50, error_rate: 0, latency_p50_ms: 10, latency_p95_ms: 20 },
      ],
    });

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('User Service')).toBeInTheDocument();
    });
    expect(screen.queryByText('abc-uuid-1')).not.toBeInTheDocument();
    expect(screen.getByText('no-name-id')).toBeInTheDocument();
  });

  it('sorts by requests descending by default and toggles on header click', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();

    mockedGetMetricsRoutesComparison.mockResolvedValue({
      total_requests: 1500,
      routes: [
        { route: 'small', requests: 500, share: 33.33, error_rate: 0, latency_p50_ms: 10, latency_p95_ms: 20 },
        { route: 'big', requests: 1000, share: 66.67, error_rate: 0, latency_p50_ms: 5, latency_p95_ms: 10 },
      ],
    });

    const { container } = renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('big')).toBeInTheDocument();
    });

    let rows = container.querySelectorAll('.comparison-table tbody tr');
    expect(rows[0].textContent).toContain('big');

    const requestsHeader = screen.getAllByRole('button', { name: 'Requests' })[0];
    expect(requestsHeader).toBeDefined();
    await user.click(requestsHeader!);

    rows = container.querySelectorAll('.comparison-table tbody tr');
    expect(rows[0].textContent).toContain('small');
  });

  it('renders the API key filter dropdown with "All" default', async () => {
    mockedGetApiKeys.mockResolvedValue([
      { name: 'alice', description: '', api_key: null, key_created: true, allowed_databases: [], allowed_routes: [], rate_limit_per_minute: null, owner: null, created_at: null },
      { name: 'bob',   description: '', api_key: null, key_created: true, allowed_databases: [], allowed_routes: [], rate_limit_per_minute: null, owner: null, created_at: null },
    ]);

    renderWithProviders(<GatewayMonitoring />);

    const select = await screen.findByLabelText(/API Key/i) as HTMLSelectElement;
    expect(select.value).toBe('');                       // 기본 = 전체 (빈 문자열)
    expect(screen.getByRole('option', { name: 'All' })).toBeInTheDocument();
    // apiKeysQuery is async — wait for options to populate
    expect(await screen.findByRole('option', { name: 'alice' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'bob' })).toBeInTheDocument();
  });

  it('passes selected consumer to metric calls when changed', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();

    mockedGetApiKeys.mockResolvedValue([
      { name: 'alice', description: '', api_key: null, key_created: true, allowed_databases: [], allowed_routes: [], rate_limit_per_minute: null, owner: null, created_at: null },
    ]);

    renderWithProviders(<GatewayMonitoring />);

    const select = await screen.findByLabelText(/API Key/i) as HTMLSelectElement;
    // wait for apiKeysQuery to populate options before selecting
    await screen.findByRole('option', { name: 'alice' });
    await user.selectOptions(select, 'alice');

    await waitFor(() => {
      // Find the most recent call with consumer == 'alice'
      const calls = mockedGetMetricsSummary.mock.calls;
      const hasConsumer = calls.some((args) => args[2] === 'alice');
      expect(hasConsumer).toBe(true);
    });

    // Routes-comparison has consumer as 2nd arg (no route arg)
    await waitFor(() => {
      const calls = mockedGetMetricsRoutesComparison.mock.calls;
      const hasConsumer = calls.some((args) => args[1] === 'alice');
      expect(hasConsumer).toBe(true);
    });
  });

  it('omits consumer parameter when "All" is selected (default)', async () => {
    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(mockedGetMetricsSummary).toHaveBeenCalled();
    });

    // Every call so far should have undefined consumer (3rd arg)
    for (const args of mockedGetMetricsSummary.mock.calls) {
      expect(args[2]).toBeUndefined();
    }
  });

  it('hides the API key filter and skips API key fetch without apikeys.read', async () => {
    renderWithProviders(<GatewayMonitoring />, { permissions: VIEWER_PERMISSIONS });

    expect(screen.queryByLabelText(/API Key/i)).not.toBeInTheDocument();
    expect(mockedGetApiKeys).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(mockedGetMetricsSummary).toHaveBeenCalled();
    });

    for (const args of mockedGetMetricsSummary.mock.calls) {
      expect(args[2]).toBeUndefined();
    }
  });
});
