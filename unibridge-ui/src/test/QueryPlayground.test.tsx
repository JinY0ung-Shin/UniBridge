import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders, makeDatabase } from './helpers';
import QueryPlayground from '../pages/QueryPlayground';

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getDatabases: vi.fn(),
  executeQuery: vi.fn(),
  getQueryHistory: vi.fn(),
  getSavedQueries: vi.fn(),
  createSavedQuery: vi.fn(),
  deleteSavedQuery: vi.fn(),
}));

// Import after mock so we get the mocked versions
import { getDatabases, executeQuery, getQueryHistory, getSavedQueries } from '../api/client';

const mockGetDatabases = getDatabases as ReturnType<typeof vi.fn>;
const mockExecuteQuery = executeQuery as ReturnType<typeof vi.fn>;
const mockGetQueryHistory = getQueryHistory as ReturnType<typeof vi.fn>;
const mockGetSavedQueries = getSavedQueries as ReturnType<typeof vi.fn>;

function sqlEditor() {
  return screen.getByRole('textbox', { name: 'SQL editor' });
}

type QueryResultFixture = {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  elapsed_ms: number;
  truncated: boolean;
};

beforeEach(() => {
  vi.clearAllMocks();
  mockGetDatabases.mockResolvedValue([]);
  mockGetQueryHistory.mockResolvedValue({ items: [], total: 0 });
  mockGetSavedQueries.mockResolvedValue([]);
});

describe('QueryPlayground', () => {
  it('renders with database selector and SQL editor', async () => {
    renderWithProviders(<QueryPlayground />);

    expect(screen.getByRole('combobox', { name: 'Database' })).toBeInTheDocument();
    expect(
      screen.getByRole('textbox', { name: 'SQL editor' }),
    ).toBeInTheDocument();
  });

  it('links workspace tabs to panels and supports arrow navigation', async () => {
    renderWithProviders(<QueryPlayground />);
    const historyTab = screen.getByRole('tab', { name: 'History' });
    const savedTab = screen.getByRole('tab', { name: 'Saved Queries' });

    expect(screen.getByRole('tablist', { name: 'Query workspace' })).toBeInTheDocument();
    expect(historyTab).toHaveAttribute('aria-selected', 'true');
    expect(historyTab).toHaveAttribute('aria-controls', 'query-workspace-panel-history');
    expect(savedTab).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'query-workspace-tab-history');

    fireEvent.keyDown(historyTab, { key: 'ArrowRight' });

    await waitFor(() => expect(savedTab).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'query-workspace-tab-saved');
  });

  it('shows a database load error when the selector data fails', async () => {
    mockGetDatabases.mockRejectedValue(new Error('database list failed'));

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load databases.')).toBeInTheDocument();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load databases.');
  });

  it('execute button is disabled when no database selected', async () => {
    renderWithProviders(<QueryPlayground />);

    const button = screen.getByRole('button', { name: /execute/i });
    expect(button).toBeDisabled();
  });

  it('shows database options after loading', async () => {
    mockGetDatabases.mockResolvedValue([makeDatabase()]);

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });
  });

  it('renders query results on successful execution', async () => {
    const user = userEvent.setup();

    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockResolvedValue({
      columns: ['id', 'name'],
      rows: [[1, 'Alice']],
      row_count: 1,
      elapsed_ms: 15,
      truncated: false,
    });

    renderWithProviders(<QueryPlayground />);

    // Wait for the DB option to appear
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });

    // Select the database
    await user.selectOptions(screen.getByRole('combobox'), 'test-db');

    // Type SQL
    await user.type(sqlEditor(), 'SELECT * FROM users');

    // Click execute
    await user.click(screen.getByRole('button', { name: /execute/i }));

    // Check results appear
    await waitFor(() => {
      expect(screen.getByText('Alice')).toBeInTheDocument();
      expect(screen.getByText('15ms')).toBeInTheDocument();
    });
    expect(screen.getByRole('status')).toHaveTextContent('1 row(s) returned');
    expect(screen.getByText('Alice').closest('td')).toHaveAttribute('title', 'Alice');
  });

  it('marks execute as busy while a query is running', async () => {
    const user = userEvent.setup();
    let resolveExecution: (value: QueryResultFixture) => void = () => {};

    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockReturnValueOnce(new Promise<QueryResultFixture>((resolve) => {
      resolveExecution = resolve;
    }));

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });

    await user.selectOptions(screen.getByRole('combobox'), 'test-db');
    await user.type(sqlEditor(), 'SELECT * FROM users');
    await user.click(screen.getByRole('button', { name: /execute/i }));

    expect(screen.getByRole('button', { name: 'Executing...' })).toHaveAttribute('aria-busy', 'true');

    resolveExecution({
      columns: ['id'],
      rows: [[1]],
      row_count: 1,
      elapsed_ms: 1,
      truncated: false,
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Execute' })).toHaveAttribute('aria-busy', 'false');
    });
  });

  it('clears editor text and stale results', async () => {
    const user = userEvent.setup();

    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockResolvedValue({
      columns: ['id', 'name'],
      rows: [[1, 'Alice']],
      row_count: 1,
      elapsed_ms: 15,
      truncated: false,
    });

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });

    await user.selectOptions(screen.getByRole('combobox', { name: 'Database' }), 'test-db');
    await user.type(screen.getByRole('textbox', { name: 'SQL editor' }), 'SELECT * FROM users');
    await user.click(screen.getByRole('button', { name: /execute/i }));

    await waitFor(() => {
      expect(screen.getByText('Alice')).toBeInTheDocument();
    });

    const clearButton = screen.getByRole('button', { name: 'Clear' });
    await user.click(clearButton);

    expect(screen.getByRole('textbox', { name: 'SQL editor' })).toHaveValue('');
    expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    expect(clearButton).toBeDisabled();
  });

  it('shows error message on execution failure', async () => {
    const user = userEvent.setup();

    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockRejectedValue({
      response: { data: { detail: 'Permission denied' } },
    });

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });

    await user.selectOptions(screen.getByRole('combobox'), 'test-db');
    await user.type(sqlEditor(), 'SELECT * FROM users');

    await user.click(screen.getByRole('button', { name: /execute/i }));

    await waitFor(() => {
      expect(screen.getByText('Permission denied')).toBeInTheDocument();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Permission denied');
  });

  it('announces truncated query results as an alert', async () => {
    const user = userEvent.setup();

    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockResolvedValue({
      columns: ['id'],
      rows: [[1]],
      row_count: 1,
      elapsed_ms: 15,
      truncated: true,
    });

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });

    await user.selectOptions(screen.getByRole('combobox'), 'test-db');
    await user.type(sqlEditor(), 'SELECT * FROM users');
    await user.click(screen.getByRole('button', { name: /execute/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Results were truncated');
    });
  });
});

describe('QueryPlayground — graph rendering', () => {
  async function selectDbAndExecute(user: ReturnType<typeof userEvent.setup>) {
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });
    await user.selectOptions(screen.getByRole('combobox'), 'test-db');
    // SQL contents don't affect rendering (executeQuery is mocked); use a simple string
    // because userEvent.type interprets { and } as keyboard key descriptors.
    await user.type(sqlEditor(), 'QUERY');
    await user.click(screen.getByRole('button', { name: /execute/i }));
  }

  it('renders Turtle in a <pre> when result.graph is set', async () => {
    const user = userEvent.setup();
    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockResolvedValue({
      columns: [],
      rows: [],
      row_count: 0,
      truncated: false,
      elapsed_ms: 5,
      graph: '@prefix ex: <http://ex/> . ex:a ex:b ex:c .',
    });

    const { container } = renderWithProviders(<QueryPlayground />);

    await selectDbAndExecute(user);

    await waitFor(() => {
      const pre = container.querySelector('pre.rdf-graph');
      expect(pre).not.toBeNull();
      expect(pre?.textContent).toContain('ex:a ex:b ex:c');
    });
  });

  it('falls back to no-rows when graph is empty string', async () => {
    const user = userEvent.setup();
    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockResolvedValue({
      columns: [],
      rows: [],
      row_count: 0,
      truncated: false,
      elapsed_ms: 3,
      graph: '',
    });

    const { container } = renderWithProviders(<QueryPlayground />);

    await selectDbAndExecute(user);

    await waitFor(() => {
      expect(container.querySelector('.no-rows')).not.toBeNull();
    });
    expect(screen.getByText('Query executed successfully. No rows returned.')).toHaveAttribute(
      'role',
      'status',
    );
    expect(container.querySelector('pre.rdf-graph')).toBeNull();
  });

  it('renders ASK boolean as a single-row table', async () => {
    const user = userEvent.setup();
    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockResolvedValue({
      columns: ['boolean'],
      rows: [[true]],
      row_count: 1,
      truncated: false,
      elapsed_ms: 2,
      graph: null,
    });

    const { container } = renderWithProviders(<QueryPlayground />);

    await selectDbAndExecute(user);

    await waitFor(() => {
      expect(screen.getByText('true')).toBeInTheDocument();
    });
    expect(container.querySelector('table.results-table')).not.toBeNull();
    expect(container.querySelector('pre.rdf-graph')).toBeNull();
  });

  it('renders SELECT table when graph is undefined', async () => {
    const user = userEvent.setup();
    mockGetDatabases.mockResolvedValue([makeDatabase()]);
    mockExecuteQuery.mockResolvedValue({
      columns: ['s'],
      rows: [['http://ex/a']],
      row_count: 1,
      truncated: false,
      elapsed_ms: 4,
    });

    const { container } = renderWithProviders(<QueryPlayground />);

    await selectDbAndExecute(user);

    await waitFor(() => {
      expect(screen.getByText('http://ex/a')).toBeInTheDocument();
    });
    expect(container.querySelector('table.results-table')).not.toBeNull();
    expect(container.querySelector('pre.rdf-graph')).toBeNull();
  });
});
