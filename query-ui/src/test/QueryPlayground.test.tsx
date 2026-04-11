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
