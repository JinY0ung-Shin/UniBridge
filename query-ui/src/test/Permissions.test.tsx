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

    expect(screen.getByPlaceholderText('Role name')).toBeInTheDocument();
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add Permission' })).toBeInTheDocument();
  });

  it('calls updatePermission when toggling checkbox', async () => {
    mockedGetPermissions.mockResolvedValue([samplePermission]);

    renderWithProviders(<Permissions />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    // The checkboxes are in order: SELECT (checked), INSERT, UPDATE, DELETE
    const checkboxes = screen.getAllByRole('checkbox', { name: '' });
    // INSERT checkbox is index 1 (allow_insert: false → toggling to true)
    await userEvent.click(checkboxes[1]);

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

    await userEvent.click(screen.getByRole('button', { name: 'Remove' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeletePermission).toHaveBeenCalledWith(1);
    });

    vi.restoreAllMocks();
  });
});
