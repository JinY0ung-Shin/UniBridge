import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders, makeDatabase } from './helpers';
import QueryPlayground from '../pages/QueryPlayground';

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getDatabases: vi.fn(),
  executeQuery: vi.fn(),
}));

// Import after mock so we get the mocked versions
import { getDatabases, executeQuery } from '../api/client';

const mockGetDatabases = getDatabases as ReturnType<typeof vi.fn>;
const mockExecuteQuery = executeQuery as ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  mockGetDatabases.mockResolvedValue([]);
});

describe('QueryPlayground', () => {
  it('renders with database selector and SQL editor', async () => {
    renderWithProviders(<QueryPlayground />);

    expect(screen.getByRole('combobox')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText('SELECT * FROM users LIMIT 10;'),
    ).toBeInTheDocument();
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
    await user.type(
      screen.getByPlaceholderText('SELECT * FROM users LIMIT 10;'),
      'SELECT * FROM users',
    );

    // Click execute
    await user.click(screen.getByRole('button', { name: /execute/i }));

    // Check results appear
    await waitFor(() => {
      expect(screen.getByText('Alice')).toBeInTheDocument();
      expect(screen.getByText('15ms')).toBeInTheDocument();
    });
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
    await user.type(
      screen.getByPlaceholderText('SELECT * FROM users LIMIT 10;'),
      'SELECT * FROM users',
    );

    await user.click(screen.getByRole('button', { name: /execute/i }));

    await waitFor(() => {
      expect(screen.getByText('Permission denied')).toBeInTheDocument();
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
    await user.type(
      screen.getByPlaceholderText('SELECT * FROM users LIMIT 10;'),
      'QUERY',
    );
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
