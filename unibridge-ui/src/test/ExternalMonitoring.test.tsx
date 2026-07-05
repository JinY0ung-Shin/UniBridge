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
  getExternalSummary: vi.fn(),
  getExternalRequests: vi.fn(),
  getExternalRequestsTotal: vi.fn(),
  getExternalStatusCodes: vi.fn(),
  getExternalLatency: vi.fn(),
  getExternalServicesComparison: vi.fn(),
  getExternalServicesComparisonSeries: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getExternalSummary,
  getExternalRequests,
  getExternalRequestsTotal,
  getExternalStatusCodes,
  getExternalLatency,
  getExternalServicesComparison,
  getExternalServicesComparisonSeries,
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
  });

  it('renders loading state', () => {
    mockedSummary.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<ExternalMonitoring />);

    expect(screen.getByText('Loading metrics...')).toBeInTheDocument();
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
});
