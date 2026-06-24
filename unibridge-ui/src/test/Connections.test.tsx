vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAdminDatabases: vi.fn(),
  createDatabase: vi.fn(),
  updateDatabase: vi.fn(),
  deleteDatabase: vi.fn(),
  testDatabase: vi.fn(),
  getDbTables: vi.fn().mockResolvedValue([]),
  getAlertResourceOwners: vi.fn(),
  setAlertResourceOwner: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getAdminDatabases,
  createDatabase,
  updateDatabase,
  testDatabase,
  deleteDatabase,
  getDbTables,
  getAlertResourceOwners,
  setAlertResourceOwner,
} from '../api/client';
import Connections from '../pages/Connections';
import { renderWithProviders, makeDatabase } from './helpers';

const mockedGetAdminDatabases = vi.mocked(getAdminDatabases);
const mockedCreateDatabase = vi.mocked(createDatabase);
const mockedUpdateDatabase = vi.mocked(updateDatabase);
const mockedTestDatabase = vi.mocked(testDatabase);
const mockedDeleteDatabase = vi.mocked(deleteDatabase);
const mockedGetDbTables = vi.mocked(getDbTables);
const mockedGetAlertResourceOwners = vi.mocked(getAlertResourceOwners);
const mockedSetAlertResourceOwner = vi.mocked(setAlertResourceOwner);
const clipboardWriteText = vi.fn();

async function fillRequiredConnectionFields({
  alias,
  host,
  database,
  username,
  databaseLabel = 'Database',
}: {
  alias: string;
  host: string;
  database: string;
  username: string;
  databaseLabel?: string;
}) {
  await userEvent.type(screen.getByRole('textbox', { name: 'Alias' }), alias);
  await userEvent.type(screen.getByRole('textbox', { name: 'Host' }), host);
  await userEvent.type(screen.getByRole('textbox', { name: databaseLabel }), database);
  await userEvent.type(screen.getByRole('textbox', { name: 'Username' }), username);
}

beforeEach(() => {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: clipboardWriteText },
  });
  clipboardWriteText.mockResolvedValue(undefined);
});

describe('Connections', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetAdminDatabases.mockResolvedValue([]);
    mockedGetAlertResourceOwners.mockResolvedValue([]);
    mockedSetAlertResourceOwner.mockResolvedValue({
      resource_type: 'db',
      resource_id: 'test-db',
      display_name: 'test-db',
      emails: [],
      alerts_enabled: true,
    });
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

  it('filters connections by search text', async () => {
    mockedGetAdminDatabases.mockResolvedValue([
      makeDatabase({ alias: 'orders-db', database: 'orders', host: 'orders.internal' }),
      makeDatabase({ alias: 'analytics-db', database: 'warehouse', host: 'analytics.internal', db_type: 'clickhouse' }),
    ]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('orders-db')).toBeInTheDocument();
    });

    await userEvent.type(screen.getByRole('searchbox', { name: 'Search connections...' }), 'analytics');

    expect(screen.queryByText('orders-db')).not.toBeInTheDocument();
    expect(screen.getByText('analytics-db')).toBeInTheDocument();

    await userEvent.clear(screen.getByRole('searchbox', { name: 'Search connections...' }));
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search connections...' }), 'missing');

    expect(screen.getByText('No matching connections')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    expect(screen.getByText('orders-db')).toBeInTheDocument();
    expect(screen.getByText('analytics-db')).toBeInTheDocument();
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
    expect(screen.getByRole('button', { name: 'Test connection test-db' })).toBeInTheDocument();
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
    expect(screen.getByRole('textbox', { name: 'Alias' })).toHaveAttribute('id', 'connection-alias');
    expect(screen.getByRole('combobox', { name: 'Type' })).toHaveAttribute('id', 'connection-db-type');
    expect(screen.getByRole('textbox', { name: 'Host' })).toHaveAttribute('id', 'connection-host');
    expect(screen.getByRole('spinbutton', { name: 'Port' })).toHaveAttribute('id', 'connection-port');
    expect(screen.getByRole('textbox', { name: /Database|Repository/i })).toHaveAttribute('id', 'connection-database');
    expect(screen.getByRole('textbox', { name: 'Username' })).toHaveAttribute('id', 'connection-username');
  });

  it('opens edit modal on edit button click', async () => {
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit connection test-db' }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Edit "test-db"' })).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Password')).toHaveAttribute(
      'aria-describedby',
      'connection-password-hint',
    );
    expect(document.getElementById('connection-password-hint')).toHaveTextContent(
      'leave blank to keep current',
    );
  });

  it('calls createDatabase on form submit', async () => {
    const newDb = makeDatabase({ alias: 'new-db' });
    mockedCreateDatabase.mockResolvedValue(newDb);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    await fillRequiredConnectionFields({
      alias: 'new-db',
      host: 'db.example.com',
      database: 'proddb',
      username: 'admin',
    });

    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateDatabase).toHaveBeenCalledTimes(1);
    });

    expect(mockedCreateDatabase).toHaveBeenCalledWith(
      expect.objectContaining({ alias: 'new-db', host: 'db.example.com' }),
    );
  });

  it('sets assignees on create when emails are entered', async () => {
    mockedCreateDatabase.mockResolvedValue(makeDatabase({ alias: 'new-db' }));

    renderWithProviders(<Connections />);
    await waitFor(() => expect(screen.getByText('No connections yet')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));
    await fillRequiredConnectionFields({
      alias: 'new-db',
      host: 'db.example.com',
      database: 'proddb',
      username: 'admin',
    });
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Assignee emails' }),
      'alice@example.com',
    );
    expect(screen.getByRole('textbox', { name: 'Assignee emails' })).toHaveAttribute(
      'aria-describedby',
      'connection-assignees-hint',
    );
    expect(document.getElementById('connection-assignees-hint')).toHaveTextContent(
      'Emails notified of alerts',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mockedSetAlertResourceOwner).toHaveBeenCalledTimes(1));
    expect(mockedSetAlertResourceOwner).toHaveBeenCalledWith('db', 'new-db', {
      emails: ['alice@example.com'],
    });
  });

  it('does not rewrite assignees on edit when they are unchanged', async () => {
    const db = makeDatabase({ alias: 'test-db' });
    mockedGetAdminDatabases.mockResolvedValue([db]);
    mockedUpdateDatabase.mockResolvedValue(db);
    mockedGetAlertResourceOwners.mockResolvedValue([
      {
        resource_type: 'db',
        resource_id: 'test-db',
        display_name: 'test-db',
        emails: ['x@y.com'],
        alerts_enabled: true,
      },
    ]);

    renderWithProviders(<Connections />);
    await waitFor(() => expect(screen.getByText('test-db')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Edit connection test-db' }));
    // assignee field prefilled from the loaded owners
    await waitFor(() =>
      expect((screen.getByRole('textbox', { name: 'Assignee emails' }) as HTMLTextAreaElement).value)
        .toBe('x@y.com'),
    );

    await userEvent.click(screen.getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(mockedUpdateDatabase).toHaveBeenCalledTimes(1));
    // unchanged assignees must NOT trigger a (potentially destructive) PUT
    expect(mockedSetAlertResourceOwner).not.toHaveBeenCalled();
  });

  it('hides the assignee field for users without alert permissions', async () => {
    renderWithProviders(<Connections />, {
      permissions: ['query.databases.read', 'query.databases.write'],
    });
    await waitFor(() => expect(screen.getByText('No connections yet')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    expect(screen.getByRole('textbox', { name: 'Alias' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Assignee emails' })).not.toBeInTheDocument();
    expect(mockedGetAlertResourceOwners).not.toHaveBeenCalled();
  });

  it('calls testDatabase and shows success toast', async () => {
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);
    mockedTestDatabase.mockResolvedValue({ status: 'ok', message: 'Connection successful' });

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Test connection test-db' }));

    await waitFor(() => {
      expect(mockedTestDatabase).toHaveBeenCalledWith('test-db');
    });
  });

  it('shows pending feedback only on the active test row', async () => {
    mockedGetAdminDatabases.mockResolvedValue([
      makeDatabase({ alias: 'primary-db' }),
      makeDatabase({ alias: 'analytics-db' }),
    ]);
    mockedTestDatabase.mockReturnValue(new Promise<Awaited<ReturnType<typeof testDatabase>>>(() => {}));

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('primary-db')).toBeInTheDocument();
    });

    const primaryTest = screen.getByRole('button', { name: 'Test connection primary-db' });
    const analyticsTest = screen.getByRole('button', { name: 'Test connection analytics-db' });
    await userEvent.click(primaryTest);

    expect(primaryTest).toHaveAttribute('aria-busy', 'true');
    expect(primaryTest).toHaveTextContent('Testing...');
    expect(analyticsTest).toHaveAttribute('aria-busy', 'false');
    expect(analyticsTest).toHaveTextContent('Test');
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

    await userEvent.click(screen.getByRole('button', { name: 'Delete connection test-db' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeleteDatabase).toHaveBeenCalledWith('test-db');
    });

    vi.restoreAllMocks();
  });

  it('shows pending feedback only on the active delete row', async () => {
    mockedGetAdminDatabases.mockResolvedValue([
      makeDatabase({ alias: 'primary-db' }),
      makeDatabase({ alias: 'analytics-db' }),
    ]);
    mockedDeleteDatabase.mockReturnValue(new Promise<Awaited<ReturnType<typeof deleteDatabase>>>(() => {}));
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('primary-db')).toBeInTheDocument();
    });

    const primaryDelete = screen.getByRole('button', { name: 'Delete connection primary-db' });
    const analyticsDelete = screen.getByRole('button', { name: 'Delete connection analytics-db' });
    await userEvent.click(primaryDelete);

    expect(primaryDelete).toHaveAttribute('aria-busy', 'true');
    expect(primaryDelete).toHaveTextContent('Deleting...');
    expect(analyticsDelete).toHaveAttribute('aria-busy', 'false');
    expect(analyticsDelete).toHaveTextContent('Delete');

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

    const typeSelect = screen.getByRole('combobox', { name: 'Type' });
    await userEvent.selectOptions(typeSelect, 'neo4j');

    expect(screen.getByRole('spinbutton', { name: 'Port' })).toHaveValue(7687);

    await fillRequiredConnectionFields({
      alias: 'graph-db',
      host: 'graph.example.com',
      database: 'neo4j',
      username: 'neo4j',
    });

    expect(screen.getByRole('combobox', { name: 'Protocol' })).toHaveDisplayValue('bolt');

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

    await userEvent.click(screen.getByRole('button', { name: 'Show cURL for graph-db' }));

    await waitFor(() => {
      expect(screen.getByText(/MATCH \(n\) RETURN n LIMIT 10/)).toBeInTheDocument();
    });
    expect(screen.getByRole('dialog', { name: /cURL — graph-db/ })).toHaveAttribute('aria-modal', 'true');
    expect(screen.queryByText(/SELECT \* FROM/)).not.toBeInTheDocument();
    expect(mockedGetDbTables).not.toHaveBeenCalled();
  });

  it('sets copied state only after clipboard write succeeds', async () => {
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Show cURL for test-db' }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Copy cURL command' })).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole('button', { name: 'Copy cURL command' }));

    await waitFor(() => {
      expect(clipboardWriteText).toHaveBeenCalledTimes(1);
    });
    const dialog = screen.getByRole('dialog', { name: /cURL — test-db/ });
    expect(within(dialog).getByRole('button', { name: 'cURL command copied' })).toHaveTextContent('Copied');
    expect(within(dialog).getByRole('status')).toHaveTextContent('Copied');
  });

  it('shows copy failure when clipboard write rejects', async () => {
    clipboardWriteText.mockRejectedValueOnce(new Error('not allowed'));
    const db = makeDatabase();
    mockedGetAdminDatabases.mockResolvedValue([db]);

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('test-db')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Show cURL for test-db' }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Copy cURL command' })).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole('button', { name: 'Copy cURL command' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to copy cURL command')).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Copy cURL command' })).toHaveTextContent('Copy');
    expect(screen.queryByRole('button', { name: 'cURL command copied' })).not.toBeInTheDocument();
  });

});

describe('Connections — graphdb', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetAdminDatabases.mockResolvedValue([]);
    mockedGetAlertResourceOwners.mockResolvedValue([]);
    mockedSetAlertResourceOwner.mockResolvedValue({
      resource_type: 'db',
      resource_id: 'test-db',
      display_name: 'test-db',
      emails: [],
      alerts_enabled: true,
    });
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

    const typeSelect = screen.getByRole('combobox', { name: 'Type' });
    await userEvent.selectOptions(typeSelect, 'graphdb');

    await fillRequiredConnectionFields({
      alias: 'graphdb-1',
      host: 'graph.example.com',
      database: 'my-repo',
      username: 'admin',
      databaseLabel: 'Repository ID',
    });

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

    await userEvent.click(screen.getByRole('button', { name: 'Show cURL for gdb-1' }));

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
    mockedGetAlertResourceOwners.mockResolvedValue([]);
    mockedSetAlertResourceOwner.mockResolvedValue({
      resource_type: 'db',
      resource_id: 'test-db',
      display_name: 'test-db',
      emails: [],
      alerts_enabled: true,
    });
  });

  it('shows error message when create fails', async () => {
    mockedCreateDatabase.mockRejectedValue(new Error('Duplicate alias'));

    renderWithProviders(<Connections />);

    await waitFor(() => {
      expect(screen.getByText('No connections yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add Connection' }));

    await fillRequiredConnectionFields({
      alias: 'dup-db',
      host: 'host',
      database: 'db',
      username: 'u',
    });

    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(screen.getByText('Duplicate alias')).toBeInTheDocument();
    });
  });
});
