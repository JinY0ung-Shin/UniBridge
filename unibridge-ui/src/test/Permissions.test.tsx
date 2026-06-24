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
  updatePermission,
  deletePermission,
} from '../api/client';
import Permissions from '../pages/Permissions';
import { renderWithProviders } from './helpers';

const mockedGetPermissions = vi.mocked(getPermissions);
const mockedGetAdminDatabases = vi.mocked(getAdminDatabases);
const mockedUpdatePermission = vi.mocked(updatePermission);
const mockedDeletePermission = vi.mocked(deletePermission);

const samplePermission = {
  id: 1,
  role: 'admin',
  db_alias: 'test-db',
  allow_select: true,
  allow_insert: false,
  allow_update: false,
  allow_delete: false,
  allowed_tables: null,
};

describe('Permissions', () => {
  beforeEach(() => {
    mockedGetPermissions.mockResolvedValue([]);
    mockedGetAdminDatabases.mockResolvedValue([]);
    mockedUpdatePermission.mockResolvedValue(samplePermission);
    mockedDeletePermission.mockResolvedValue(undefined);
  });

  it('renders loading state', () => {
    mockedGetPermissions.mockReturnValue(new Promise(() => {}));
    renderWithProviders(<Permissions />);
    expect(screen.getByText('Loading permissions...')).toBeInTheDocument();
  });

  it('renders permissions table', async () => {
    mockedGetPermissions.mockResolvedValue([samplePermission]);

    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    expect(screen.getByText('test-db')).toBeInTheDocument();
  });

  it('filters permissions by search text', async () => {
    mockedGetPermissions.mockResolvedValue([
      samplePermission,
      {
        id: 2,
        role: 'analyst',
        db_alias: 'warehouse',
        allow_select: true,
        allow_insert: false,
        allow_update: false,
        allow_delete: false,
        allowed_tables: ['events'],
      },
    ]);

    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    const search = screen.getByRole('searchbox', { name: /search permissions/i });
    await userEvent.type(search, 'events');

    expect(screen.queryByText('admin')).not.toBeInTheDocument();
    expect(screen.getByText('analyst')).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, 'missing');
    expect(screen.getByText(/No matching permissions|일치하는 권한/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Clear search|검색 지우기/i }));
    expect(screen.getByText('admin')).toBeInTheDocument();
    expect(screen.getByText('analyst')).toBeInTheDocument();
  });

  it('disables or hides write controls for users with read-only permission access', async () => {
    mockedGetPermissions.mockResolvedValue([samplePermission]);

    renderWithProviders(<Permissions />, {
      permissions: ['query.permissions.read', 'query.databases.read'],
    });

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    expect(screen.queryByPlaceholderText('Role name')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add Permission' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();
    for (const checkbox of screen.getAllByRole('checkbox')) {
      expect(checkbox).toBeDisabled();
    }
  });

  it('renders empty state when no permissions', async () => {
    mockedGetPermissions.mockResolvedValue([]);

    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('No permissions configured')).toBeInTheDocument();
    });
  });

  it('renders add permission controls', async () => {
    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('No permissions configured')).toBeInTheDocument();
    });

    expect(screen.getByRole('textbox', { name: 'Role name' })).toHaveAttribute('id', 'permission-role-name');
    expect(screen.getByRole('combobox', { name: 'Select database...' })).toHaveAttribute('id', 'permission-database');
    expect(screen.getByRole('button', { name: 'Add Permission' })).toBeInTheDocument();
  });

  it('calls updatePermission when toggling checkbox', async () => {
    mockedGetPermissions.mockResolvedValue([samplePermission]);

    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    // The checkboxes are in order: SELECT (checked), INSERT, UPDATE, DELETE
    await userEvent.click(screen.getByRole('checkbox', { name: 'Toggle INSERT for admin on test-db' }));

    await waitFor(() => {
      expect(mockedUpdatePermission).toHaveBeenCalledWith(
        expect.objectContaining({ allow_insert: true }),
      );
    });
  });

  it('calls deletePermission after confirmation', async () => {
    mockedGetPermissions.mockResolvedValue([samplePermission]);

    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Remove permission for admin on test-db' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeletePermission).toHaveBeenCalledWith(1);
    });

    vi.restoreAllMocks();
  });
});
