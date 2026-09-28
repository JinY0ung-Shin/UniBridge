vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div data-testid="responsive-container">{children}</div>,
  // Rows and per-line props are exposed so tests can check the latency merge
  // (nulls kept) and that gaps are not bridged (no connectNulls).
  LineChart: ({ children, data }: { children: React.ReactNode; data?: unknown }) => (
    <div data-testid="line-chart" data-rows={JSON.stringify(data ?? [])}>{children}</div>
  ),
  BarChart: ({ children }: { children: React.ReactNode }) => <div data-testid="bar-chart">{children}</div>,
  Line: ({ dataKey, connectNulls, dot }: { dataKey: string; connectNulls?: boolean; dot?: unknown }) => (
    <span
      data-testid={`line-${dataKey}`}
      data-connect-nulls={String(Boolean(connectNulls))}
      data-dot={typeof dot}
    />
  ),
  Bar: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getQueryMetricsSummary: vi.fn(),
  getQueryMetricsTotal: vi.fn(),
  getQueryMetricsOutcomes: vi.fn(),
  getQueryMetricsLatency: vi.fn(),
  getQueryDatabasesComparison: vi.fn(),
  getQueryConsumersComparison: vi.fn(),
  getQueryDatabasesSeries: vi.fn(),
  getQueryConsumersSeries: vi.fn(),
  getApiKeys: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { ReactElement } from 'react';
import {
  getQueryMetricsSummary,
  getQueryMetricsTotal,
  getQueryMetricsOutcomes,
  getQueryMetricsLatency,
  getQueryDatabasesComparison,
  getQueryConsumersComparison,
  getQueryDatabasesSeries,
  getQueryConsumersSeries,
  getApiKeys,
  type QueryDatabaseComparisonRow,
  type QueryConsumerComparisonRow,
} from '../api/client';
import { AuthContext } from '../components/AuthContext';
import QueryMonitoring from '../pages/QueryMonitoring';
import { renderWithProviders, makeApiKey, VIEWER_PERMISSIONS } from './helpers';

const mockedSummary = vi.mocked(getQueryMetricsSummary);
const mockedTotal = vi.mocked(getQueryMetricsTotal);
const mockedOutcomes = vi.mocked(getQueryMetricsOutcomes);
const mockedLatency = vi.mocked(getQueryMetricsLatency);
const mockedDatabases = vi.mocked(getQueryDatabasesComparison);
const mockedConsumers = vi.mocked(getQueryConsumersComparison);
const mockedDatabasesSeries = vi.mocked(getQueryDatabasesSeries);
const mockedConsumersSeries = vi.mocked(getQueryConsumersSeries);
const mockedGetApiKeys = vi.mocked(getApiKeys);

const emptyBucketed = { buckets: [], series: [], unit: 'queries' as const };
const EMPTY_SUMMARY = {
  total_queries: 0, error_rate: 0, timeouts: 0, avg_latency_ms: null, p95_latency_ms: null, avg_rows: null,
};

function dbRow(overrides: Partial<QueryDatabaseComparisonRow> = {}): QueryDatabaseComparisonRow {
  return {
    database: 'orders-db', db_type: 'postgres', queries: 100, share: 100, error_rate: 0,
    avg_latency_ms: 12.5, latency_p95_ms: 40, avg_rows: 7.25, ...overrides,
  };
}

function keyRow(overrides: Partial<QueryConsumerComparisonRow> = {}): QueryConsumerComparisonRow {
  return {
    consumer: 'alice', queries: 100, share: 100, error_rate: 0,
    avg_latency_ms: 12.5, latency_p95_ms: 40, avg_rows: 7.25, ...overrides,
  };
}

const asAdmin = (ui: ReactElement) => (
  <AuthContext.Provider
    value={{ authenticated: true, token: null, username: 'tester', appRole: 'admin', initialized: true, logout: () => {} }}
  >
    {ui}
  </AuthContext.Provider>
);

describe('QueryMonitoring', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSummary.mockResolvedValue(EMPTY_SUMMARY);
    mockedTotal.mockResolvedValue([]);
    mockedOutcomes.mockResolvedValue([]);
    mockedLatency.mockResolvedValue({ p50: [], p95: [], p99: [] });
    mockedDatabases.mockResolvedValue({ total_queries: 0, databases: [] });
    mockedConsumers.mockResolvedValue({ total_queries: 0, consumers: [] });
    mockedDatabasesSeries.mockResolvedValue(emptyBucketed);
    mockedConsumersSeries.mockResolvedValue(emptyBucketed);
    mockedGetApiKeys.mockResolvedValue([]);
  });

  it('renders the loading state', () => {
    mockedSummary.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<QueryMonitoring />);

    expect(screen.getByText('Loading metrics...')).toBeInTheDocument();
  });

  it('renders the six summary cards', async () => {
    mockedSummary.mockResolvedValue({
      total_queries: 1234, error_rate: 2.5, timeouts: 3, avg_latency_ms: 45.5, p95_latency_ms: 120, avg_rows: 12.35,
    });

    renderWithProviders(<QueryMonitoring />);

    expect(await screen.findByText('1,234')).toBeInTheDocument();
    expect(screen.getByText('Total Queries (1h)')).toBeInTheDocument();
    expect(screen.getByText('2.5%')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('45.5ms')).toBeInTheDocument();
    expect(screen.getByText('120ms')).toBeInTheDocument();
    expect(screen.getByText('12.35')).toBeInTheDocument();
    expect(screen.getByText('Error Rate (incl. timeouts)')).toBeInTheDocument();
    expect(screen.getByText('Avg Rows Returned')).toBeInTheDocument();
  });

  it('shows em-dash latency and row cards when there were no successful queries', async () => {
    renderWithProviders(<QueryMonitoring />);

    expect(await screen.findByText('p95 Execution Time')).toBeInTheDocument();
    for (const label of ['Avg Execution Time', 'p95 Execution Time', 'Avg Rows Returned']) {
      const card = screen.getByText(label).closest('.metric-card') as HTMLElement;
      expect(within(card).getByText('—')).toBeInTheDocument();
    }
  });

  it('renders the chart and table panels with the success-only latency caption', async () => {
    renderWithProviders(<QueryMonitoring />);

    await screen.findByText('Total Queries (1h)');
    expect(screen.getByText('Query Count (per interval)')).toBeInTheDocument();
    expect(screen.getByText('Query Outcomes (per interval)')).toBeInTheDocument();
    expect(screen.getByText('Execution Time (ms)')).toBeInTheDocument();
    expect(screen.getByText(/Successful queries only · measured inside UniBridge from connection acquire/)).toBeInTheDocument();
    expect(screen.getByText('Database Comparison (1h total)')).toBeInTheDocument();
    expect(screen.getByText('API Key Comparison (1h total, top 20)')).toBeInTheDocument();
    expect(screen.getByText('Queries by database (over time)')).toBeInTheDocument();
    expect(screen.getByText('Queries by API key (over time)')).toBeInTheDocument();
  });

  it('renders charts once series data arrives', async () => {
    mockedTotal.mockResolvedValue([{ timestamp: 1772323200, value: 4.6 }]);
    mockedOutcomes.mockResolvedValue([{ timestamp: 1772323200, success: 4, error: 1, timeout: 0 }]);
    mockedLatency.mockResolvedValue({
      p50: [{ timestamp: 1772323200, value: 10 }],
      p95: [{ timestamp: 1772323200, value: 20 }],
      p99: [],
    });

    renderWithProviders(<QueryMonitoring />);

    await waitFor(() => {
      expect(screen.getAllByTestId('bar-chart')).toHaveLength(2);
    });
    expect(screen.getByTestId('line-chart')).toBeInTheDocument();
  });

  it('merges latency percentiles by timestamp, keeping nulls as gaps', async () => {
    mockedLatency.mockResolvedValue({
      p50: [{ timestamp: 200, value: null }, { timestamp: 100, value: 10 }],
      p95: [{ timestamp: 100, value: 20 }],
      p99: [{ timestamp: 300, value: 50 }],
    });

    renderWithProviders(<QueryMonitoring />);

    const chart = await screen.findByTestId('line-chart');
    const rows = JSON.parse(chart.getAttribute('data-rows') ?? '[]') as Array<Record<string, number | null>>;
    expect(rows.map((r) => [r.p50, r.p95, r.p99])).toEqual([
      [10, 20, null],
      [null, null, null],
      [null, null, 50],
    ]);
    for (const key of ['p50', 'p95', 'p99']) {
      expect(screen.getByTestId(`line-${key}`)).toHaveAttribute('data-connect-nulls', 'false');
      // Lone points between gaps need a dot renderer to be visible at all.
      expect(screen.getByTestId(`line-${key}`)).toHaveAttribute('data-dot', 'function');
    }
  });

  it('shows the latency empty state when every percentile point is null', async () => {
    mockedLatency.mockResolvedValue({ p50: [{ timestamp: 100, value: null }], p95: [], p99: [] });

    renderWithProviders(<QueryMonitoring />);

    expect(await screen.findByText('No execution time data available')).toBeInTheDocument();
    expect(screen.queryByTestId('line-chart')).not.toBeInTheDocument();
  });

  it('renders the database table with type, em-dash nulls, and heatmap classes', async () => {
    mockedDatabases.mockResolvedValue({
      total_queries: 150,
      databases: [
        dbRow({ database: 'orders-db', queries: 100, share: 66.67, error_rate: 7.5 }),
        dbRow({ database: 'graph-db', db_type: null, queries: 50, share: 33.33, avg_latency_ms: null, latency_p95_ms: null, avg_rows: null }),
      ],
    });

    const { container } = renderWithProviders(<QueryMonitoring />);

    const graphRow = await screen.findByRole('button', { name: 'Filter by database graph-db' });
    expect(within(graphRow).getAllByText('—')).toHaveLength(4);
    expect(screen.getByText('postgres')).toBeInTheDocument();
    expect(container.querySelectorAll('.heatmap-cell--red').length).toBeGreaterThan(0);
    // Default sort: queries descending.
    const rows = container.querySelectorAll('.comparison-table tbody tr');
    expect(rows[0].textContent).toContain('orders-db');
  });

  it('toggles the DB filter from a table row and cross-filters only the other dimension', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedDatabases.mockResolvedValue({ total_queries: 100, databases: [dbRow()] });

    renderWithProviders(<QueryMonitoring />);

    const row = await screen.findByRole('button', { name: 'Filter by database orders-db' });
    await user.click(row);

    await waitFor(() => {
      expect(row).toHaveAttribute('aria-pressed', 'true');
    });
    expect((screen.getByLabelText('Database') as HTMLSelectElement).value).toBe('orders-db');
    // summary(sel, consumer, database) and consumers-comparison(sel, database)
    expect(mockedSummary.mock.calls.some((args) => args[2] === 'orders-db')).toBe(true);
    expect(mockedConsumers.mock.calls.some((args) => args[1] === 'orders-db')).toBe(true);
    // The DB table itself never takes the DB filter (it stays a full list).
    for (const args of mockedDatabases.mock.calls) {
      expect(args[1]).toBeUndefined();
    }

    await user.click(row);
    await waitFor(() => {
      expect(row).toHaveAttribute('aria-pressed', 'false');
    });
    expect((screen.getByLabelText('Database') as HTMLSelectElement).value).toBe('');
  });

  it('toggles the DB filter from the keyboard', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedDatabases.mockResolvedValue({ total_queries: 100, databases: [dbRow()] });

    renderWithProviders(<QueryMonitoring />);

    const row = await screen.findByRole('button', { name: 'Filter by database orders-db' });
    row.focus();
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(row).toHaveAttribute('aria-pressed', 'true');
    });
  });

  it('lists databases with traffic in the DB filter and forwards the selection', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedDatabases.mockResolvedValue({
      total_queries: 150,
      databases: [dbRow({ database: 'zeta-db' }), dbRow({ database: 'alpha-db' })],
    });

    renderWithProviders(<QueryMonitoring />);

    const select = screen.getByLabelText('Database') as HTMLSelectElement;
    await screen.findByRole('option', { name: 'alpha-db' });
    const optionNames = within(select).getAllByRole('option').map((o) => o.textContent);
    expect(optionNames).toEqual(['All', 'alpha-db', 'zeta-db']);

    await user.selectOptions(select, 'zeta-db');

    await waitFor(() => {
      expect(mockedTotal.mock.calls.some((args) => args[2] === 'zeta-db')).toBe(true);
    });
    expect(mockedLatency.mock.calls.some((args) => args[2] === 'zeta-db')).toBe(true);
    expect(mockedOutcomes.mock.calls.some((args) => args[2] === 'zeta-db')).toBe(true);
  });

  it('labels UI and pre-tracking rows and keeps the pre-tracking row non-interactive', async () => {
    mockedConsumers.mockResolvedValue({
      total_queries: 300,
      consumers: [
        keyRow({ consumer: 'alice', queries: 150, share: 50 }),
        keyRow({ consumer: '__ui__', queries: 100, share: 33.33 }),
        keyRow({ consumer: '(untracked)', queries: 50, share: 16.67 }),
      ],
    });

    renderWithProviders(<QueryMonitoring />);

    expect(await screen.findByRole('button', { name: 'Filter by API key (UI / direct)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Filter by API key alice' })).toBeInTheDocument();
    const untracked = screen.getByText('(before per-key tracking)');
    expect(untracked).toHaveAttribute('title', 'Queries recorded before per-API-key tracking was enabled');
    expect(untracked.closest('tr')).not.toHaveAttribute('role');
  });

  it('toggles the API key filter from a table row', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedGetApiKeys.mockResolvedValue([makeApiKey({ name: 'alice', description: 'Billing batch' })]);
    mockedConsumers.mockResolvedValue({ total_queries: 100, consumers: [keyRow()] });

    renderWithProviders(<QueryMonitoring />);

    const row = await screen.findByRole('button', { name: 'Filter by API key alice' });
    await waitFor(() => {
      expect(within(row).getByText('alice')).toHaveAttribute('title', 'Billing batch');
    });
    await user.click(row);

    await waitFor(() => {
      expect(row).toHaveAttribute('aria-pressed', 'true');
    });
    expect((screen.getByLabelText('API Key') as HTMLSelectElement).value).toBe('alice');
    // summary(sel, consumer, database) and databases-comparison(sel, consumer)
    expect(mockedSummary.mock.calls.some((args) => args[1] === 'alice')).toBe(true);
    expect(mockedDatabases.mock.calls.some((args) => args[1] === 'alice')).toBe(true);
    // The key table itself never takes the key filter.
    for (const args of mockedConsumers.mock.calls) {
      expect(args[1]).toBeUndefined();
    }
  });

  it('offers All, the UI sentinel, and sorted keys in the API key filter', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedGetApiKeys.mockResolvedValue([makeApiKey({ name: 'bob' }), makeApiKey({ name: 'alice' })]);

    renderWithProviders(<QueryMonitoring />);

    const select = await screen.findByLabelText('API Key') as HTMLSelectElement;
    await screen.findByRole('option', { name: 'bob' });
    const optionNames = within(select).getAllByRole('option').map((o) => o.textContent);
    expect(optionNames).toEqual(['All', '(UI / direct)', 'alice', 'bob']);

    await user.selectOptions(select, '__ui__');

    await waitFor(() => {
      expect(mockedSummary.mock.calls.some((args) => args[1] === '__ui__')).toBe(true);
    });
  });

  it('omits both filters by default', async () => {
    renderWithProviders(<QueryMonitoring />);

    await waitFor(() => {
      expect(mockedSummary).toHaveBeenCalled();
    });
    for (const args of mockedSummary.mock.calls) {
      expect(args[1]).toBeUndefined();
      expect(args[2]).toBeUndefined();
    }
  });

  it('shows the API key filter to viewers without apikeys.read, fed by table rows only', async () => {
    mockedConsumers.mockResolvedValue({
      total_queries: 300,
      consumers: [
        keyRow({ consumer: 'alice' }),
        keyRow({ consumer: '__ui__' }),
        keyRow({ consumer: '(untracked)' }),
      ],
    });

    renderWithProviders(<QueryMonitoring />, { permissions: VIEWER_PERMISSIONS });

    const select = screen.getByLabelText('API Key') as HTMLSelectElement;
    await screen.findByRole('option', { name: 'alice' });
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'All', '(UI / direct)', 'alice',
    ]);
    expect(mockedGetApiKeys).not.toHaveBeenCalled();
  });

  it('keeps a row-picked key filter visible and clearable after its row disappears', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedDatabases.mockResolvedValue({ total_queries: 100, databases: [dbRow({ database: 'orders-db' })] });
    mockedConsumers.mockResolvedValue({ total_queries: 100, consumers: [keyRow({ consumer: 'alice' })] });

    renderWithProviders(<QueryMonitoring />, { permissions: VIEWER_PERMISSIONS });

    await user.click(await screen.findByRole('button', { name: 'Filter by API key alice' }));
    const keySelect = screen.getByLabelText('API Key') as HTMLSelectElement;
    await waitFor(() => {
      expect(keySelect.value).toBe('alice');
    });

    // alice has no traffic on the DB picked next, so her row drops out.
    mockedConsumers.mockResolvedValue({ total_queries: 0, consumers: [] });
    await user.selectOptions(screen.getByLabelText('Database'), 'orders-db');
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Filter by API key alice' })).not.toBeInTheDocument();
    });
    expect(keySelect.value).toBe('alice');
    expect(within(keySelect).getByRole('option', { name: 'alice' })).toBeInTheDocument();

    await user.selectOptions(keySelect, '');
    expect(keySelect.value).toBe('');
    await waitFor(() => {
      expect(mockedSummary.mock.lastCall?.[1]).toBeUndefined();
    });
    expect(mockedSummary.mock.lastCall?.[2]).toBe('orders-db');
  });

  it('pins self-scoped viewers to their key: note shown, no key filter, rows not filterable, no Grafana', async () => {
    mockedConsumers.mockResolvedValue({ total_queries: 10, consumers: [keyRow({ consumer: 'self_abc' })] });

    renderWithProviders(asAdmin(<QueryMonitoring />), { permissions: ['gateway.monitoring.self', 'apikeys.read'] });

    expect(await screen.findByText('Showing queries from your API key only')).toBeInTheDocument();
    expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument();
    expect(mockedGetApiKeys).not.toHaveBeenCalled();
    const cell = await screen.findByText('self_abc');
    expect(cell.closest('tr')).not.toHaveAttribute('role');
    expect(screen.queryByRole('link', { name: /Open in Grafana/ })).not.toBeInTheDocument();
  });

  it('deep-links Grafana with the active filters for admins', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedDatabases.mockResolvedValue({ total_queries: 100, databases: [dbRow()] });

    renderWithProviders(asAdmin(<QueryMonitoring />));

    await user.click(await screen.findByRole('button', { name: 'Filter by database orders-db' }));

    const link = await screen.findByRole('link', { name: /Open in Grafana/ });
    const href = link.getAttribute('href') ?? '';
    expect(href).toContain('/d/unibridge-queries?');
    expect(href).toContain('var-database=orders-db');
    expect(href).not.toContain('var-consumer');
  });

  it('picks a calendar bucket: nudges the preset and enables the breakdown series', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();

    renderWithProviders(<QueryMonitoring />);

    await screen.findByText('Total Queries (1h)');
    expect(mockedDatabasesSeries).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Daily' }));

    expect(screen.getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'true');
    await waitFor(() => {
      expect(mockedDatabasesSeries.mock.calls.some((args) => args[2] === 'day')).toBe(true);
    });
    expect(mockedConsumersSeries.mock.calls.some((args) => args[2] === 'day')).toBe(true);
    expect(mockedTotal.mock.calls.some((args) => args[3] === 'day')).toBe(true);
  });

  it('shows partial load feedback when a secondary panel fails', async () => {
    mockedOutcomes.mockRejectedValue(new Error('outcomes failed'));

    renderWithProviders(<QueryMonitoring />);

    expect(
      await screen.findByText('Some metrics failed to load. Data may be incomplete.'),
    ).toBeInTheDocument();
  });

  it('shows the load-failed banner when the summary fails', async () => {
    mockedSummary.mockRejectedValue(new Error('prometheus down'));

    renderWithProviders(<QueryMonitoring />);

    expect(
      await screen.findByText('Failed to load metrics. Is Prometheus running?'),
    ).toBeInTheDocument();
    expect(screen.queryByText('Some metrics failed to load. Data may be incomplete.')).not.toBeInTheDocument();
  });

  it('sorts the API key table on header click', async () => {
    const userEvent = (await import('@testing-library/user-event')).default;
    const user = userEvent.setup();
    mockedConsumers.mockResolvedValue({
      total_queries: 300,
      consumers: [
        keyRow({ consumer: 'small', queries: 100, share: 33.33 }),
        keyRow({ consumer: 'big', queries: 200, share: 66.67 }),
      ],
    });

    renderWithProviders(<QueryMonitoring />);

    const bigRow = await screen.findByRole('button', { name: 'Filter by API key big' });
    const table = bigRow.closest('table') as HTMLTableElement;
    let rows = table.querySelectorAll('tbody tr');
    expect(rows[0].textContent).toContain('big');

    await user.click(within(table).getByRole('button', { name: 'Queries' }));

    rows = table.querySelectorAll('tbody tr');
    expect(rows[0].textContent).toContain('small');
  });
});
