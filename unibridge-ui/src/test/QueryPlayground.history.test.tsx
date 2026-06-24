import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders, makeDatabase, makeAuditLog, makeSavedQuery } from './helpers';
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
import {
  getDatabases,
  getQueryHistory,
  getSavedQueries,
  createSavedQuery,
  deleteSavedQuery,
} from '../api/client';

const mockGetDatabases = getDatabases as ReturnType<typeof vi.fn>;
const mockGetQueryHistory = getQueryHistory as ReturnType<typeof vi.fn>;
const mockGetSavedQueries = getSavedQueries as ReturnType<typeof vi.fn>;
const mockCreateSavedQuery = createSavedQuery as ReturnType<typeof vi.fn>;
const mockDeleteSavedQuery = deleteSavedQuery as ReturnType<typeof vi.fn>;

function sqlEditor() {
  return screen.getByRole('textbox', { name: 'SQL editor' });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetDatabases.mockResolvedValue([makeDatabase()]);
  mockGetQueryHistory.mockResolvedValue({ items: [], total: 0 });
  mockGetSavedQueries.mockResolvedValue([]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('QueryPlayground — history panel', () => {
  it('renders my recent queries with time, db, sql, and status', async () => {
    mockGetQueryHistory.mockResolvedValue({
      items: [
        makeAuditLog({ id: 1, sql: 'SELECT 1', status: 'success' }),
        makeAuditLog({ id: 2, sql: 'SELECT broken', status: 'error' }),
      ],
      total: 2,
    });

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByText('SELECT 1')).toBeInTheDocument();
    });
    expect(screen.getByText('SELECT broken')).toBeInTheDocument();
    expect(screen.getByText('success')).toBeInTheDocument();
    expect(screen.getByText('error')).toBeInTheDocument();
    expect(mockGetQueryHistory).toHaveBeenCalledWith({ limit: 50 });
  });

  it('shows an empty state when there is no history', async () => {
    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByText('No query history yet')).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Refresh query history' })).toBeInTheDocument();
  });

  it('clicking a history row loads SQL and database into the editor', async () => {
    const user = userEvent.setup();
    mockGetQueryHistory.mockResolvedValue({
      items: [makeAuditLog({ id: 1, sql: 'SELECT 42', database_alias: 'test-db' })],
      total: 1,
    });

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByText('SELECT 42')).toBeInTheDocument();
    });
    // Wait for database options so the alias can be selected on load
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });

    await user.click(screen.getByText('SELECT 42'));

    expect(sqlEditor()).toHaveValue('SELECT 42');
    expect(screen.getByRole('combobox')).toHaveValue('test-db');
  });

  it('loads a history row from keyboard activation', async () => {
    mockGetQueryHistory.mockResolvedValue({
      items: [makeAuditLog({ id: 1, sql: 'SELECT 99', database_alias: 'test-db' })],
      total: 1,
    });

    renderWithProviders(<QueryPlayground />);

    await waitFor(() => {
      expect(screen.getByText('SELECT 99')).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });

    fireEvent.keyDown(screen.getByRole('button', { name: /Load into editor: SELECT 99/ }), {
      key: 'Enter',
    });

    expect(sqlEditor()).toHaveValue('SELECT 99');
    expect(screen.getByRole('combobox')).toHaveValue('test-db');
  });
});

describe('QueryPlayground — saved queries panel', () => {
  async function openSavedTab(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('tab', { name: 'Saved Queries' }));
  }

  it('lists saved queries with load and delete actions', async () => {
    const user = userEvent.setup();
    mockGetSavedQueries.mockResolvedValue([
      makeSavedQuery({ id: 1, name: 'My users', sql_text: 'SELECT * FROM users' }),
      makeSavedQuery({ id: 2, name: 'No db', database_alias: null, sql_text: 'SELECT 2' }),
    ]);

    renderWithProviders(<QueryPlayground />);
    await openSavedTab(user);

    await waitFor(() => {
      expect(screen.getByText('My users')).toBeInTheDocument();
    });
    expect(screen.getByText('No db')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Load saved query "My users"' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete saved query "My users"' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Load saved query "No db"' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete saved query "No db"' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh saved queries' })).toBeInTheDocument();
  });

  it('filters saved queries by search text', async () => {
    const user = userEvent.setup();
    mockGetSavedQueries.mockResolvedValue([
      makeSavedQuery({ id: 1, name: 'My users', sql_text: 'SELECT * FROM users' }),
      makeSavedQuery({
        id: 2,
        name: 'Orders report',
        description: 'Daily order rollup',
        database_alias: 'test-db',
        sql_text: 'SELECT * FROM orders',
      }),
    ]);

    renderWithProviders(<QueryPlayground />);
    await openSavedTab(user);

    await waitFor(() => {
      expect(screen.getByText('My users')).toBeInTheDocument();
    });

    const search = screen.getByRole('searchbox', { name: /search saved queries/i });
    await user.type(search, 'orders');

    expect(screen.queryByText('My users')).not.toBeInTheDocument();
    expect(screen.getByText('Orders report')).toBeInTheDocument();

    await user.clear(search);
    await user.type(search, 'missing');
    expect(screen.getByText(/No matching saved queries|일치하는 저장 쿼리/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Clear search|검색 지우기/i }));
    expect(screen.getByText('My users')).toBeInTheDocument();
    expect(screen.getByText('Orders report')).toBeInTheDocument();
  });

  it('load button fills the editor with the saved query', async () => {
    const user = userEvent.setup();
    mockGetSavedQueries.mockResolvedValue([
      makeSavedQuery({ id: 1, sql_text: 'SELECT * FROM users', database_alias: 'test-db' }),
    ]);

    renderWithProviders(<QueryPlayground />);
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });
    await openSavedTab(user);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Load saved query "My users"' })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: 'Load saved query "My users"' }));

    expect(sqlEditor()).toHaveValue('SELECT * FROM users');
    expect(screen.getByRole('combobox')).toHaveValue('test-db');
  });

  it('deletes a saved query after confirmation', async () => {
    const user = userEvent.setup();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockGetSavedQueries.mockResolvedValue([makeSavedQuery({ id: 7, name: 'Old one' })]);
    mockDeleteSavedQuery.mockResolvedValue(undefined);

    renderWithProviders(<QueryPlayground />);
    await openSavedTab(user);

    await waitFor(() => {
      expect(screen.getByText('Old one')).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: 'Delete saved query "Old one"' }));

    await waitFor(() => {
      expect(mockDeleteSavedQuery).toHaveBeenCalledWith(7);
    });
  });

  it('does not delete when confirmation is dismissed', async () => {
    const user = userEvent.setup();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    mockGetSavedQueries.mockResolvedValue([makeSavedQuery({ id: 7, name: 'Old one' })]);

    renderWithProviders(<QueryPlayground />);
    await openSavedTab(user);

    await waitFor(() => {
      expect(screen.getByText('Old one')).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: 'Delete saved query "Old one"' }));

    expect(mockDeleteSavedQuery).not.toHaveBeenCalled();
  });
});

describe('QueryPlayground — save query modal', () => {
  it('save button is disabled until the editor has SQL', async () => {
    const user = userEvent.setup();
    renderWithProviders(<QueryPlayground />);

    const saveButton = screen.getByRole('button', { name: 'Save Query' });
    expect(saveButton).toBeDisabled();

    await user.type(sqlEditor(), 'SELECT 1');
    expect(saveButton).toBeEnabled();
  });

  it('saves the current editor content with a name from the modal', async () => {
    const user = userEvent.setup();
    mockCreateSavedQuery.mockResolvedValue(makeSavedQuery({ id: 3, name: 'Quick check' }));

    renderWithProviders(<QueryPlayground />);
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'test-db' })).toBeInTheDocument();
    });

    await user.selectOptions(screen.getByRole('combobox'), 'test-db');
    await user.type(sqlEditor(), 'SELECT 1');
    await user.click(screen.getByRole('button', { name: 'Save Query' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toBeInTheDocument();

    await user.type(screen.getByLabelText('Name'), 'Quick check');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockCreateSavedQuery).toHaveBeenCalledWith({
        name: 'Quick check',
        description: '',
        database_alias: 'test-db',
        sql_text: 'SELECT 1',
      });
    });
    // Modal closes after a successful save
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  it('marks the modal save button as busy while saving', async () => {
    const user = userEvent.setup();
    let resolveSave: (value: ReturnType<typeof makeSavedQuery>) => void = () => {};
    mockCreateSavedQuery.mockReturnValueOnce(new Promise<ReturnType<typeof makeSavedQuery>>((resolve) => {
      resolveSave = resolve;
    }));

    renderWithProviders(<QueryPlayground />);

    await user.type(sqlEditor(), 'SELECT 1');
    await user.click(screen.getByRole('button', { name: 'Save Query' }));
    await user.type(await screen.findByLabelText('Name'), 'Quick check');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(screen.getByRole('button', { name: 'Saving...' })).toHaveAttribute('aria-busy', 'true');

    resolveSave(makeSavedQuery({ id: 3, name: 'Quick check' }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  it('shows an error and keeps the modal open when saving fails', async () => {
    const user = userEvent.setup();
    mockCreateSavedQuery.mockRejectedValue({
      response: { data: { detail: 'name must not be empty' } },
    });

    renderWithProviders(<QueryPlayground />);
    await user.type(sqlEditor(), 'SELECT 1');
    await user.click(screen.getByRole('button', { name: 'Save Query' }));

    await user.type(await screen.findByLabelText('Name'), 'x');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(screen.getByText('name must not be empty')).toBeInTheDocument();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('name must not be empty');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});
