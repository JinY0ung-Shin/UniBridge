vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  updateApiKey: vi.fn(),
  deleteApiKey: vi.fn(),
  getAdminDatabases: vi.fn(),
  getGatewayRoutes: vi.fn(),
  getS3Connections: vi.fn(),
  getNasConnections: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getApiKeys,
  getAdminDatabases,
  getGatewayRoutes,
  getS3Connections,
  getNasConnections,
  createApiKey,
  deleteApiKey,
} from '../api/client';
import ApiKeys from '../pages/ApiKeys';
import {
  renderWithProviders,
  makeApiKey,
  makeDatabase,
  makeGatewayRoute,
  makeS3Connection,
  makeNasConnection,
} from './helpers';

const mockedGetApiKeys = vi.mocked(getApiKeys);
const mockedGetAdminDatabases = vi.mocked(getAdminDatabases);
const mockedGetGatewayRoutes = vi.mocked(getGatewayRoutes);
const mockedGetS3Connections = vi.mocked(getS3Connections);
const mockedGetNasConnections = vi.mocked(getNasConnections);
const mockedCreateApiKey = vi.mocked(createApiKey);
const mockedDeleteApiKey = vi.mocked(deleteApiKey);
const clipboardWriteText = vi.fn();

async function typeKeyName(name: string) {
  await userEvent.type(screen.getByRole('textbox', { name: 'Name' }), name);
}

describe('ApiKeys', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    });
    clipboardWriteText.mockResolvedValue(undefined);
    mockedGetApiKeys.mockResolvedValue([]);
    mockedGetAdminDatabases.mockResolvedValue([]);
    mockedGetGatewayRoutes.mockResolvedValue({ items: [], total: 0 });
    mockedGetS3Connections.mockResolvedValue([]);
    mockedGetNasConnections.mockResolvedValue([]);
  });

  it('renders loading state', () => {
    mockedGetApiKeys.mockReturnValue(new Promise(() => {}));
    renderWithProviders(<ApiKeys />);
    expect(screen.getByText('Loading API keys...')).toBeInTheDocument();
  });

  it('renders API keys table', async () => {
    const key = makeApiKey();
    mockedGetApiKeys.mockResolvedValue([key]);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    expect(screen.getByText('Test API key')).toBeInTheDocument();
  });

  it('renders master keys as all access in the table', async () => {
    mockedGetApiKeys.mockResolvedValue([
      makeApiKey({
        is_master: true,
        allowed_databases: ['*'],
        allowed_routes: ['*'],
      }),
    ]);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    expect(screen.getAllByText('All access')).toHaveLength(2);
  });

  it('filters API keys by search text', async () => {
    mockedGetApiKeys.mockResolvedValue([
      makeApiKey({ name: 'orders-client', description: 'Order service', allowed_databases: ['orders-db'] }),
      makeApiKey({ name: 'billing-client', description: 'Billing service', allowed_databases: ['billing-db'] }),
    ]);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('orders-client')).toBeInTheDocument();
    });

    await userEvent.type(screen.getByRole('searchbox', { name: 'Search API keys...' }), 'billing');

    expect(screen.queryByText('orders-client')).not.toBeInTheDocument();
    expect(screen.getByText('billing-client')).toBeInTheDocument();

    await userEvent.clear(screen.getByRole('searchbox', { name: 'Search API keys...' }));
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search API keys...' }), 'missing');

    expect(screen.getByText('No matching API keys')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    expect(screen.getByText('orders-client')).toBeInTheDocument();
    expect(screen.getByText('billing-client')).toBeInTheDocument();
  });

  it('hides write actions for users with read-only API key permission', async () => {
    const key = makeApiKey();
    mockedGetApiKeys.mockResolvedValue([key]);

    renderWithProviders(<ApiKeys />, {
      permissions: ['apikeys.read'],
    });

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: '+ Add API Key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('renders empty state when no keys', async () => {
    mockedGetApiKeys.mockResolvedValue([]);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });
  });

  it('opens create modal on add button click', async () => {
    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));

    const dialog = screen.getByRole('dialog', { name: 'Add API Key' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveAttribute('id', 'api-key-name');
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveAttribute('id', 'api-key-description');
    expect(screen.getByRole('textbox', { name: 'API Key' })).toHaveAttribute('id', 'api-key-secret');
    expect(screen.getByRole('group', { name: 'Allowed Data Sources' })).toHaveAttribute(
      'aria-labelledby',
      'api-key-allowed-databases-label',
    );
    expect(screen.getByRole('group', { name: 'Allowed Routes' })).toHaveAttribute(
      'aria-labelledby',
      'api-key-allowed-routes-label',
    );
  });

  it('opens edit modal on edit button click', async () => {
    const key = makeApiKey();
    mockedGetApiKeys.mockResolvedValue([key]);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit API key my-app' }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Edit API Key' })).toBeInTheDocument();
    });
  });

  it('renders structured checkbox rows in create modal', async () => {
    mockedGetAdminDatabases.mockResolvedValue([
      makeDatabase({ alias: 'analytics-db', db_type: 'postgres' }),
    ]);
    mockedGetGatewayRoutes.mockResolvedValue({
      items: [makeGatewayRoute({ id: 'route-1', name: 'Users API', uri: '/api/users/very/long/path/*' })],
      total: 1,
    });

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));

    const databaseOption = screen.getByRole('checkbox', { name: /analytics-db/i }).closest('label');
    const routeOption = screen.getByRole('checkbox', { name: /users api/i }).closest('label');

    expect(databaseOption).toHaveClass('checkbox-list-item');
    expect(routeOption).toHaveClass('checkbox-list-item');

    expect(within(databaseOption!).getByText('analytics-db')).toHaveClass('checkbox-list-label');
    expect(within(databaseOption!).getByText('postgres')).toHaveClass('tag');
    expect(within(routeOption!).getByText('Users API')).toHaveClass('checkbox-list-label');
    expect(within(routeOption!).getByText('/api/users/very/long/path/*')).toHaveClass('tag');
  });

  it('renders structured checkbox rows in edit modal', async () => {
    mockedGetApiKeys.mockResolvedValue([
      makeApiKey({
        allowed_databases: ['analytics-db'],
        allowed_routes: ['route-1'],
      }),
    ]);
    mockedGetAdminDatabases.mockResolvedValue([
      makeDatabase({ alias: 'analytics-db', db_type: 'postgres' }),
    ]);
    mockedGetGatewayRoutes.mockResolvedValue({
      items: [makeGatewayRoute({ id: 'route-1', name: 'Users API', uri: '/api/users/very/long/path/*' })],
      total: 1,
    });

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit API key my-app' }));

    const databaseOption = screen.getByRole('checkbox', { name: /analytics-db/i }).closest('label');
    const routeOption = screen.getByRole('checkbox', { name: /users api/i }).closest('label');

    expect(databaseOption).toHaveClass('checkbox-list-item');
    expect(routeOption).toHaveClass('checkbox-list-item');
    expect(screen.getByRole('checkbox', { name: /analytics-db/i })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /users api/i })).toBeChecked();

    expect(within(databaseOption!).getByText('analytics-db')).toHaveClass('checkbox-list-label');
    expect(within(databaseOption!).getByText('postgres')).toHaveClass('tag');
    expect(within(routeOption!).getByText('Users API')).toHaveClass('checkbox-list-label');
    expect(within(routeOption!).getByText('/api/users/very/long/path/*')).toHaveClass('tag');
  });

  it('includes S3 connection aliases in allowed databases when creating a key', async () => {
    mockedGetAdminDatabases.mockResolvedValue([
      makeDatabase({ alias: 'postgres', db_type: 'postgres' }),
    ]);
    mockedGetS3Connections.mockResolvedValue([
      makeS3Connection({ alias: 'lakes3' }),
    ]);
    mockedCreateApiKey.mockResolvedValue({
      name: 'new-app',
      description: '',
      api_key: 'key-secret-12345',
      key_created: true,
      allowed_databases: ['postgres', 'lakes3'],
      allowed_routes: [],
      rate_limit_per_minute: null,
      owner: null,
      created_at: '2026-04-11T00:00:00Z',
    });

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));
    await typeKeyName('new-app');
    await userEvent.click(screen.getByRole('checkbox', { name: /lakes3/i }));
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateApiKey).toHaveBeenCalledWith(
        expect.objectContaining({
          allowed_databases: ['lakes3'],
        }),
        expect.anything(),
      );
    });
  });

  it('includes NAS connection aliases in allowed databases when creating a key', async () => {
    mockedGetNasConnections.mockResolvedValue([
      makeNasConnection({ alias: 'company-nas' }),
    ]);
    mockedGetGatewayRoutes.mockResolvedValue({
      items: [makeGatewayRoute({ id: 'nas-api', name: 'nas-api', uri: '/api/nas/*' })],
      total: 1,
    });
    mockedCreateApiKey.mockResolvedValue({
      name: 'nas-client',
      description: '',
      api_key: 'key-secret-12345',
      key_created: true,
      allowed_databases: ['company-nas'],
      allowed_routes: ['nas-api'],
      rate_limit_per_minute: null,
      owner: null,
      created_at: '2026-04-11T00:00:00Z',
    });

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));
    await typeKeyName('nas-client');
    await userEvent.click(screen.getByRole('checkbox', { name: /company-nas/i }));
    await userEvent.click(screen.getByRole('checkbox', { name: /nas-api/i }));
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateApiKey).toHaveBeenCalledWith(
        expect.objectContaining({
          allowed_databases: ['company-nas'],
          allowed_routes: ['nas-api'],
        }),
        expect.anything(),
      );
    });
  });

  it('creates a master key with wildcard access', async () => {
    mockedGetAdminDatabases.mockResolvedValue([
      makeDatabase({ alias: 'postgres', db_type: 'postgres' }),
    ]);
    mockedGetGatewayRoutes.mockResolvedValue({
      items: [makeGatewayRoute({ id: 'query-api', name: 'query-api', uri: '/api/query/*' })],
      total: 1,
    });
    mockedCreateApiKey.mockResolvedValue({
      name: 'master-client',
      description: '',
      api_key: 'key-secret-12345',
      key_created: true,
      is_master: true,
      allowed_databases: ['*'],
      allowed_routes: ['*'],
      rate_limit_per_minute: null,
      owner: null,
      created_at: '2026-04-11T00:00:00Z',
    });

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));
    await typeKeyName('master-client');
    await userEvent.click(screen.getByRole('checkbox', { name: /master key/i }));
    expect(screen.getByRole('checkbox', { name: /postgres/i })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /query-api/i })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateApiKey).toHaveBeenCalledWith(
        expect.objectContaining({
          is_master: true,
          allowed_databases: ['*'],
          allowed_routes: ['*'],
        }),
        expect.anything(),
      );
    });
  });

  it('calls createApiKey and shows created key', async () => {
    mockedCreateApiKey.mockResolvedValue({
      name: 'new-app',
      description: '',
      api_key: 'key-secret-12345',
      key_created: true,
      allowed_databases: [],
      allowed_routes: [],
      rate_limit_per_minute: null,
      owner: null,
      created_at: '2026-04-11T00:00:00Z',
    });

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));

    await typeKeyName('new-app');
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateApiKey).toHaveBeenCalledTimes(1);
    });

    // After creation, the key should be displayed
    await waitFor(() => {
      expect(screen.getByText('key-secret-12345')).toBeInTheDocument();
    });
    expect(screen.getByRole('status')).toHaveTextContent('API key created');

    await userEvent.click(screen.getByRole('button', { name: 'Copy generated API key' }));
    await waitFor(() => {
      expect(clipboardWriteText).toHaveBeenCalledWith('key-secret-12345');
    });
    expect(screen.getByRole('button', { name: 'Generated API key copied' })).toHaveTextContent('Copied!');
  });

  it('shows copy failure feedback for a newly created key', async () => {
    clipboardWriteText.mockRejectedValueOnce(new Error('blocked'));
    mockedCreateApiKey.mockResolvedValue({
      name: 'new-app',
      description: '',
      api_key: 'key-secret-12345',
      key_created: true,
      allowed_databases: [],
      allowed_routes: [],
      rate_limit_per_minute: null,
      owner: null,
      created_at: '2026-04-11T00:00:00Z',
    });

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));
    await typeKeyName('new-app');
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(screen.getByText('key-secret-12345')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Copy generated API key' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to copy API key')).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Copy generated API key' })).toHaveTextContent('Copy');
    expect(screen.queryByRole('button', { name: 'Generated API key copied' })).not.toBeInTheDocument();
  });

  it('calls deleteApiKey after confirmation', async () => {
    const key = makeApiKey();
    mockedGetApiKeys.mockResolvedValue([key]);
    mockedDeleteApiKey.mockResolvedValue(undefined);

    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Delete API key my-app' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeleteApiKey).toHaveBeenCalledWith('my-app', expect.anything());
    });

    vi.restoreAllMocks();
  });

  it('shows pending feedback only on the active delete row', async () => {
    mockedGetApiKeys.mockResolvedValue([
      makeApiKey({ name: 'orders-client' }),
      makeApiKey({ name: 'billing-client' }),
    ]);
    mockedDeleteApiKey.mockReturnValue(new Promise<Awaited<ReturnType<typeof deleteApiKey>>>(() => {}));
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('orders-client')).toBeInTheDocument();
    });

    const ordersDelete = screen.getByRole('button', { name: 'Delete API key orders-client' });
    const billingDelete = screen.getByRole('button', { name: 'Delete API key billing-client' });
    await userEvent.click(ordersDelete);

    expect(ordersDelete).toHaveAttribute('aria-busy', 'true');
    expect(ordersDelete).toHaveTextContent('Deleting...');
    expect(billingDelete).toHaveAttribute('aria-busy', 'false');
    expect(billingDelete).toHaveTextContent('Delete');

    vi.restoreAllMocks();
  });

  it('renders expiry column: dash for admin keys, date for expiring keys', async () => {
    mockedGetApiKeys.mockResolvedValue([
      makeApiKey(),
      makeApiKey({ name: 'self-key', expires_at: '2026-07-10T00:00:00Z' }),
    ]);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    expect(screen.getByRole('columnheader', { name: 'Expires' })).toBeInTheDocument();

    const adminRow = screen.getByText('my-app').closest('tr');
    expect(within(adminRow!).getByText('\u2014')).toBeInTheDocument();

    const selfRow = screen.getByText('self-key').closest('tr');
    // 2026-07-10T00:00:00Z formatted as KST (2026-07-10 09:00:00)
    expect(within(selfRow!).getByText(/2026\. 07\. 10\./)).toBeInTheDocument();
  });

  it('sends write flags and allowed tables when creating a key', async () => {
    mockedCreateApiKey.mockResolvedValue(
      makeApiKey({ name: 'writer-app', api_key: 'key-secret-999', key_created: true }),
    );

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));
    await typeKeyName('writer-app');
    expect(screen.getByRole('group', { name: 'Write Permissions' })).toHaveAttribute(
      'aria-describedby',
      'api-key-write-permissions-hint',
    );
    expect(document.getElementById('api-key-write-permissions-hint')).toHaveTextContent(
      'DDL is never allowed',
    );
    expect(screen.getByRole('textbox', { name: 'Allowed Tables' })).toHaveAttribute(
      'aria-describedby',
      'api-key-allowed-tables-hint',
    );
    expect(document.getElementById('api-key-allowed-tables-hint')).toHaveTextContent(
      'Leave empty to allow all tables',
    );
    expect(screen.getByRole('spinbutton', { name: 'Rate limit (per min)' })).toHaveAttribute(
      'aria-describedby',
      'api-key-rate-limit-hint',
    );
    expect(document.getElementById('api-key-rate-limit-hint')).toHaveTextContent(
      'Leave empty for unlimited',
    );
    await userEvent.click(screen.getByRole('checkbox', { name: 'Allow INSERT' }));
    await userEvent.click(screen.getByRole('checkbox', { name: 'Allow DELETE' }));
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Allowed Tables' }),
      'orders, line_items',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateApiKey).toHaveBeenCalledWith(
        expect.objectContaining({
          allow_insert: true,
          allow_update: false,
          allow_delete: true,
          allowed_tables: ['orders', 'line_items'],
        }),
        expect.anything(),
      );
    });
  });

  it('omits table restriction (null) when allowed tables left empty', async () => {
    mockedCreateApiKey.mockResolvedValue(
      makeApiKey({ name: 'plain-app', api_key: 'key-secret-000', key_created: true }),
    );

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));
    await typeKeyName('plain-app');
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateApiKey).toHaveBeenCalledWith(
        expect.objectContaining({
          allow_insert: false,
          allow_update: false,
          allow_delete: false,
          allowed_tables: null,
        }),
        expect.anything(),
      );
    });
  });

  it('prefills write flags and allowed tables in edit modal', async () => {
    mockedGetApiKeys.mockResolvedValue([
      makeApiKey({
        allow_insert: true,
        allow_update: false,
        allow_delete: true,
        allowed_tables: ['orders', 'users'],
      }),
    ]);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit API key my-app' }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Edit API Key' })).toBeInTheDocument();
    });

    expect(screen.getByRole('checkbox', { name: 'Allow INSERT' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Allow UPDATE' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Allow DELETE' })).toBeChecked();
    expect(screen.getByRole('textbox', { name: 'Allowed Tables' })).toHaveValue('orders, users');
  });
});
