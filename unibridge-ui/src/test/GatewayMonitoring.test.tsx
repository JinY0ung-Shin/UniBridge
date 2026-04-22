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
}));

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getMetricsSummary: vi.fn(),
  getMetricsRequests: vi.fn(),
  getMetricsStatusCodes: vi.fn(),
  getMetricsLatency: vi.fn(),
  getMetricsRoutesComparison: vi.fn(),
  getMetricsRequestsTotal: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsRoutesComparison,
  getMetricsRequestsTotal,
} from '../api/client';
import GatewayMonitoring from '../pages/GatewayMonitoring';
import { renderWithProviders } from './helpers';

const mockedGetMetricsSummary = vi.mocked(getMetricsSummary);
const mockedGetMetricsRequests = vi.mocked(getMetricsRequests);
const mockedGetMetricsStatusCodes = vi.mocked(getMetricsStatusCodes);
const mockedGetMetricsLatency = vi.mocked(getMetricsLatency);
const mockedGetMetricsRoutesComparison = vi.mocked(getMetricsRoutesComparison);
const mockedGetMetricsRequestsTotal = vi.mocked(getMetricsRequestsTotal);

describe('GatewayMonitoring', () => {
  beforeEach(() => {
    mockedGetMetricsSummary.mockResolvedValue({ total_requests: 0, error_rate: 0, avg_latency_ms: 0 });
    mockedGetMetricsRequests.mockResolvedValue([]);
    mockedGetMetricsStatusCodes.mockResolvedValue([]);
    mockedGetMetricsLatency.mockResolvedValue({ p50: [], p95: [], p99: [] });
    mockedGetMetricsRoutesComparison.mockResolvedValue({ total_requests: 0, routes: [] });
    mockedGetMetricsRequestsTotal.mockResolvedValue([]);
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

    expect(screen.getByText('Request Trend')).toBeInTheDocument();
    expect(screen.getByText('Status Code Distribution')).toBeInTheDocument();
    expect(screen.getByText('Latency (ms)')).toBeInTheDocument();
    expect(screen.getByText('Route Comparison')).toBeInTheDocument();
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

    const requestsHeader = screen
      .getAllByRole('button')
      .find((el) => el.tagName === 'TH' && /Requests/.test(el.textContent ?? ''));
    expect(requestsHeader).toBeDefined();
    await user.click(requestsHeader!);

    rows = container.querySelectorAll('.comparison-table tbody tr');
    expect(rows[0].textContent).toContain('small');
  });
});
