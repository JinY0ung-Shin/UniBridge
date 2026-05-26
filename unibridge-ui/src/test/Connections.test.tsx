vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAdminDatabases: vi.fn(),
  createDatabase: vi.fn(),
  updateDatabase: vi.fn(),
  deleteDatabase: vi.fn(),
  testDatabase: vi.fn(),
  getDbTables: vi.fn().mockResolvedValue([]),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAdminDatabases, createDatabase, testDatabase, deleteDatabase, getDbTables } from '../api/client';
import Connections from '../pages/Connections';
import { renderWithProviders, makeDatabase } from './helpers';

const mockedGetAdminDatabases = vi.mocked(getAdminDatabases);
const mockedCreateDatabase = vi.mocked(createDatabase);
const mockedTestDatabase = vi.mocked(testDatabase);
const mockedDeleteDatabase = vi.mocked(deleteDatabase);
const mockedGetDbTables = vi.mocked(getDbTables);

describe('Connections', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetAdminDatabases.mockResolvedValue([]);
  });

  it('renders loading state', () => {
    // Make the query hang so the loading state persists
    mockedGetAdminDatabases.mockReturnValue(new Promise(() => {}));
    renderWithProviders(<Connections />);
    expect(screen.getByText('Loading connections...')).toBeInTheDocument();
  });

  it('renders database table when data loads', async () => {
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    expect(screen.getByText('localhost:5432')).toBeInTheDocument();
  });

  it('hides write actions for users with read-only database permission', async () => {
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);

    renderWithProviders(<Connections />, {
      permissions: ['query.databases.read'],
    });

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: '+ Add Connection' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Test' })).toBeInTheDocument();
  });

  it('renders empty state when no databases', async () => {
    mockedGetAdminDatabases.mockResolvedValue([]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });
  });

  it('renders error state on fetch failure', async () => {
    mockedGetAdminDatabases.mockRejectedValue(new Error('Network error'));

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load database connections.')).toBeInTheDocument();
    });
  });

  it('opens create modal on add button click', async () => {
    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    expect(screen.getByRole('heading', { name: 'Add Connection' })).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Add Connection' })).toHaveAttribute('aria-modal', 'true');
  });

  it('opens edit modal on edit button click', async () => {
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Edit "test-db"' })).toBeInTheDocument();
    });
  });

  it('calls createDatabase on form submit', async () => {
    const newDb = makeDatabase({ alias: 'new-db' });
    mockedCreateDatabase.mockResolvedValue(newDb);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    // Fill out required fields
    await userEvent.type(screen.getByPlaceholderText('e.g., main-db'), 'new-db');
    await userEvent.type(screen.getByPlaceholderText('localhost'), 'db.example.com');
    await userEvent.type(screen.getByPlaceholderText('mydb'), 'proddb');
    await userEvent.type(screen.getByPlaceholderText('dbuser'), 'admin');

    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateDatabase).toHaveBeenCalledTimes(1);
    });

    expect(mockedCreateDatabase).toHaveBeenCalledWith(
      expect.objectContaining({ alias: 'new-db', host: 'db.example.com' }),
    );
  });

  it('calls testDatabase and shows success toast', async () => {
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);
    mockedTestDatabase.mockResolvedValue({ status: 'ok', message: 'Connection successful' });

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Test' }));

    await waitFor(() => {
      expect(mockedTestDatabase).toHaveBeenCalledWith('test-db');
    });
  });

  it('calls deleteDatabase after confirmation', async () => {
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);
    mockedDeleteDatabase.mockResolvedValue(undefined);

    // Mock window.confirm to return true
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeleteDatabase).toHaveBeenCalledWith('test-db');
    });

    vi.restoreAllMocks();
  });

  it('submits neo4j connection with default bolt protocol and port 7687', async () => {
    mockedCreateDatabase.mockClear();
    const newDb = makeDatabase({
      alias: 'graph-db',
      db_type: 'neo4j' as const,
      port: 7687,
      protocol: 'bolt' as const,
    });
    mockedCreateDatabase.mockResolvedValue(newDb);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    const [typeSelect] = screen.getAllByRole('combobox');
    await userEvent.selectOptions(typeSelect, 'neo4j');

    expect(screen.getByDisplayValue('7687')).toBeInTheDocument();

    await userEvent.type(screen.getByPlaceholderText('e.g., main-db'), 'graph-db');
    await userEvent.type(screen.getByPlaceholderText('localhost'), 'graph.example.com');
    await userEvent.type(screen.getByPlaceholderText('mydb'), 'neo4j');
    await userEvent.type(screen.getByPlaceholderText('dbuser'), 'neo4j');

    const comboboxes = screen.getAllByRole('combobox');
    expect(comboboxes).toHaveLength(2);
    expect(comboboxes[1]).toHaveDisplayValue('bolt');

    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateDatabase).toHaveBeenCalledTimes(1);
    });

    const submitted = mockedCreateDatabase.mock.calls[0][0];
    expect(submitted).toMatchObject({
      alias: 'graph-db',
      db_type: 'neo4j',
      port: 7687,
      protocol: 'bolt',
      secure: null,
    });
  });

  it('shows cypher cURL sample for neo4j connections', async () => {
    const db = makeDatabase({
      alias: 'graph-db',
      db_type: 'neo4j' as const,
      port: 7687,
      database: 'neo4j',
      protocol: 'bolt' as const,
    });
    mockedGetAdminDatabases.mockResolvedValue([db]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('graph-db')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'cURL' }));

    await waitFor(() => {
      expect(screen.getByText(/MATCH \(n\) RETURN n LIMIT 10/)).toBeInTheDocument();
    });
    expect(screen.getByRole('dialog', { name: /cURL — graph-db/ })).toHaveAttribute('aria-modal', 'true');
    expect(screen.queryByText(/SELECT \* FROM/)).not.toBeInTheDocument();
    expect(mockedGetDbTables).not.toHaveBeenCalled();
  });

});

describe('Connections — graphdb', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetAdminDatabases.mockResolvedValue([]);
  });

  it('selecting graphdb sets port 7200 and shows Repository ID label', async () => {
    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    const [typeSelect] = screen.getAllByRole('combobox');
    await userEvent.selectOptions(typeSelect, 'graphdb');

    expect(screen.getByDisplayValue('7200')).toBeInTheDocument();
    expect(screen.getByText('Repository ID')).toBeInTheDocument();
    expect(screen.queryByText('Database', { selector: 'label' })).not.toBeInTheDocument();
  });

  it('hides secure / pool_size / max_overflow inputs for graphdb', async () => {
    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    const [typeSelect] = screen.getAllByRole('combobox');
    await userEvent.selectOptions(typeSelect, 'graphdb');

    expect(screen.queryByText('Pool Size')).not.toBeInTheDocument();
    expect(screen.queryByText('Max Overflow')).not.toBeInTheDocument();
    expect(screen.queryByText(/secure/i)).not.toBeInTheDocument();
  });

  it('protocol select for graphdb only offers http and https', async () => {
    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    const [typeSelect] = screen.getAllByRole('combobox');
    await userEvent.selectOptions(typeSelect, 'graphdb');

    const comboboxes = screen.getAllByRole('combobox');
    expect(comboboxes).toHaveLength(2);
    const protocolSelect = comboboxes[1] as HTMLSelectElement;
    const optionValues = Array.from(protocolSelect.options).map((o) => o.value);
    expect(optionValues).toEqual(['http', 'https']);
  });

  it('submits graphdb payload with protocol http and no pool fields', async () => {
    mockedCreateDatabase.mockClear();
    mockedCreateDatabase.mockResolvedValue(
      makeDatabase({
        alias: 'graphdb-1',
        db_type: 'graphdb' as const,
        port: 7200,
        protocol: 'http' as const,
        database: 'my-repo',
      }),
    );

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    const [typeSelect] = screen.getAllByRole('combobox');
    await userEvent.selectOptions(typeSelect, 'graphdb');

    await userEvent.type(screen.getByPlaceholderText('e.g., main-db'), 'graphdb-1');
    await userEvent.type(screen.getByPlaceholderText('localhost'), 'graph.example.com');
    await userEvent.type(screen.getByPlaceholderText('my-repo'), 'my-repo');
    await userEvent.type(screen.getByPlaceholderText('dbuser'), 'admin');

    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateDatabase).toHaveBeenCalledTimes(1);
    });

    const submitted = mockedCreateDatabase.mock.calls[0][0];
    expect(submitted).toMatchObject({
      alias: 'graphdb-1',
      db_type: 'graphdb',
      host: 'graph.example.com',
      port: 7200,
      protocol: 'http',
      database: 'my-repo',
      secure: null,
    });
  });

  it('shows SPARQL cURL sample for graphdb connections', async () => {
    const db = makeDatabase({
      alias: 'gdb-1',
      db_type: 'graphdb' as const,
      port: 7200,
      database: 'my-repo',
      protocol: 'http' as const,
    });
    mockedGetAdminDatabases.mockResolvedValue([db]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('gdb-1')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'cURL' }));

    await waitFor(() => {
      expect(screen.getByText(/SELECT \?s \?p \?o WHERE/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/SELECT \* FROM/)).not.toBeInTheDocument();
    expect(screen.queryByText(/MATCH \(n\)/)).not.toBeInTheDocument();
    expect(mockedGetDbTables).not.toHaveBeenCalled();
  });
});

describe('Connections (error case)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetAdminDatabases.mockResolvedValue([]);
  });

  it('shows error message when create fails', async () => {
    mockedCreateDatabase.mockRejectedValue(new Error('Duplicate alias'));

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    await userEvent.type(screen.getByPlaceholderText('e.g., main-db'), 'dup-db');
    await userEvent.type(screen.getByPlaceholderText('localhost'), 'host');
    await userEvent.type(screen.getByPlaceholderText('mydb'), 'db');
    await userEvent.type(screen.getByPlaceholderText('dbuser'), 'u');

    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(screen.getByText('Duplicate alias')).toBeInTheDocument();
    });
  });
});
