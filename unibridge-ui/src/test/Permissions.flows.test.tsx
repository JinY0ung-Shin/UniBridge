vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getPermissions: vi.fn(),
  getAdminDatabases: vi.fn(),
  getDbTables: vi.fn(),
  updatePermission: vi.fn(),
  deletePermission: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getPermissions,
  getAdminDatabases,
  getDbTables,
  updatePermission,
} from '../api/client';
import Permissions from '../pages/Permissions';
import { renderWithProviders, makeDatabase } from './helpers';

const mockedGetPermissions = vi.mocked(getPermissions);
const mockedGetAdminDatabases = vi.mocked(getAdminDatabases);
const mockedGetDbTables = vi.mocked(getDbTables);
const mockedUpdatePermission = vi.mocked(updatePermission);

const samplePerm = {
  id: 1,
  role: 'analyst',
  db_alias: 'db-1',
  allow_select: true,
  allow_insert: false,
  allow_update: false,
  allow_delete: false,
  allowed_tables: ['users', 'orders'],
};

describe('Permissions flows', () => {
  beforeEach(() => {
    mockedGetPermissions.mockResolvedValue([samplePerm]);
    mockedGetAdminDatabases.mockResolvedValue([makeDatabase({ alias: 'db-1' })]);
    mockedGetDbTables.mockResolvedValue(['users', 'orders', 'logs']);
    mockedUpdatePermission.mockResolvedValue(samplePerm);
  });

  it('shows error banner when permissions query fails', async () => {
    mockedGetPermissions.mockRejectedValueOnce(new Error('boom'));
    renderWithProviders(<Permissions />);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load permissions/)).toBeInTheDocument();
    });
  });

  it('renders allowed_tables as tags', async () => {
    renderWithProviders(<Permissions />);
    await waitFor(() => {
      expect(screen.getByText('users')).toBeInTheDocument();
    });
    expect(screen.getByText('orders')).toBeInTheDocument();
  });

  it('add permission button submits a new permission and clears inputs', async () => {
    mockedGetPermissions.mockResolvedValue([]);
    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('No permissions configured')).toBeInTheDocument();
    });

    await userEvent.type(screen.getByPlaceholderText('Role name'), 'new-role');
    await userEvent.selectOptions(screen.getByRole('combobox'), 'db-1');
    await userEvent.click(screen.getByRole('button', { name: 'Add Permission' }));

    await waitFor(() => {
      expect(mockedUpdatePermission).toHaveBeenCalledWith(
        expect.objectContaining({
          role: 'new-role',
          db_alias: 'db-1',
          allow_select: true,
        }),
      );
    });
    expect((screen.getByPlaceholderText('Role name') as HTMLInputElement).value).toBe('');
  });

  it('Add Permission button disabled when role or db are missing', async () => {
    mockedGetPermissions.mockResolvedValue([]);
    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('No permissions configured')).toBeInTheDocument();
    });

    const addBtn = screen.getByRole('button', { name: 'Add Permission' });
    expect(addBtn).toBeDisabled();

    await userEvent.type(screen.getByPlaceholderText('Role name'), 'a');
    expect(addBtn).toBeDisabled();  // db not yet selected
  });

  it('Edit tables loads available tables and saves selection', async () => {
    renderWithProviders(<Permissions />);
    await waitFor(() => {
      expect(screen.getByText('analyst')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));

    await waitFor(() => {
      expect(mockedGetDbTables).toHaveBeenCalledWith('db-1');
    });
    // available tables should appear once loading completes
    await waitFor(() => {
      expect(screen.getByText('logs')).toBeInTheDocument();
    });

    // Toggle "users" off then save
    const usersCheckbox = screen.getAllByRole('checkbox').find(
      (c) => (c as HTMLInputElement).nextElementSibling?.textContent === 'users',
    );
    expect(usersCheckbox).toBeDefined();
    await userEvent.click(usersCheckbox!);
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockedUpdatePermission).toHaveBeenCalledWith(
        expect.objectContaining({ allowed_tables: ['orders'] }),
      );
    });
  });

  it('Edit tables cancel returns to display mode', async () => {
    renderWithProviders(<Permissions />);
    await waitFor(() => {
      expect(screen.getByText('analyst')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    });
  });

  it('Edit tables handles getDbTables failure gracefully', async () => {
    mockedGetDbTables.mockRejectedValue(new Error('no tables'));
    renderWithProviders(<Permissions />);
    await waitFor(() => {
      expect(screen.getByText('analyst')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await waitFor(() => {
      expect(screen.getByText('No tables found')).toBeInTheDocument();
    });
  });
});
