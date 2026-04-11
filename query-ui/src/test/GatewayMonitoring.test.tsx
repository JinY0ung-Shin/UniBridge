vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: any) => <div data-testid="responsive-container">{children}</div>,
  LineChart: ({ children }: any) => <div data-testid="line-chart">{children}</div>,
  BarChart: ({ children }: any) => <div data-testid="bar-chart">{children}</div>,
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
  getMetricsTopRoutes: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsTopRoutes,
} from '../api/client';
import GatewayMonitoring from '../pages/GatewayMonitoring';
import { renderWithProviders } from './helpers';

const mockedGetMetricsSummary = vi.mocked(getMetricsSummary);
const mockedGetMetricsRequests = vi.mocked(getMetricsRequests);
const mockedGetMetricsStatusCodes = vi.mocked(getMetricsStatusCodes);
const mockedGetMetricsLatency = vi.mocked(getMetricsLatency);
const mockedGetMetricsTopRoutes = vi.mocked(getMetricsTopRoutes);

describe('GatewayMonitoring', () => {
  beforeEach(() => {
    mockedGetMetricsSummary.mockResolvedValue(null as any);
    mockedGetMetricsRequests.mockResolvedValue([]);
    mockedGetMetricsStatusCodes.mockResolvedValue([]);
    mockedGetMetricsLatency.mockResolvedValue(null as any);
    mockedGetMetricsTopRoutes.mockResolvedValue([]);
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
    expect(screen.getByText('Top Routes by Traffic')).toBeInTheDocument();
  });

  it('renders top routes table when data available', async () => {
    mockedGetMetricsSummary.mockResolvedValue({
      total_requests: 500,
      error_rate: 0,
      avg_latency_ms: 20,
    });
    mockedGetMetricsTopRoutes.mockResolvedValue([
      { route: '/api/users', requests: 500 },
    ]);

    renderWithProviders(<GatewayMonitoring />);

    await waitFor(() => {
      expect(screen.getByText('/api/users')).toBeInTheDocument();
    });

    const table = screen.getByRole('table');
    expect(table).toHaveTextContent('500');
  });
});
