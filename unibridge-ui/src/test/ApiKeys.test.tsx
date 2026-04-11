vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  updateApiKey: vi.fn(),
  deleteApiKey: vi.fn(),
  getAdminDatabases: vi.fn(),
  getGatewayRoutes: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getApiKeys, getAdminDatabases, getGatewayRoutes, createApiKey, deleteApiKey } from '../api/client';
import ApiKeys from '../pages/ApiKeys';
import { renderWithProviders, makeApiKey } from './helpers';

const mockedGetApiKeys = vi.mocked(getApiKeys);
const mockedGetAdminDatabases = vi.mocked(getAdminDatabases);
const mockedGetGatewayRoutes = vi.mocked(getGatewayRoutes);
const mockedCreateApiKey = vi.mocked(createApiKey);
const mockedDeleteApiKey = vi.mocked(deleteApiKey);

describe('ApiKeys', () => {
  beforeEach(() => {
    mockedGetApiKeys.mockResolvedValue([]);
    mockedGetAdminDatabases.mockResolvedValue([]);
    mockedGetGatewayRoutes.mockResolvedValue({ items: [], total: 0 });
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

    expect(screen.getByRole('heading', { name: 'Add API Key' })).toBeInTheDocument();
  });

  it('opens edit modal on edit button click', async () => {
    const key = makeApiKey();
    mockedGetApiKeys.mockResolvedValue([key]);

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('my-app')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Edit API Key' })).toBeInTheDocument();
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
      created_at: '2026-04-11T00:00:00Z',
    });

    renderWithProviders(<ApiKeys />);

    await waitFor(() => {
      expect(screen.getByText('No API keys')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: '+ Add API Key' }));

    await userEvent.type(screen.getByPlaceholderText('my-app'), 'new-app');
    await userEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedCreateApiKey).toHaveBeenCalledTimes(1);
    });

    // After creation, the key should be displayed
    await waitFor(() => {
      expect(screen.getByText('key-secret-12345')).toBeInTheDocument();
    });
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

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeleteApiKey).toHaveBeenCalledWith('my-app', expect.anything());
    });

    vi.restoreAllMocks();
  });
});
