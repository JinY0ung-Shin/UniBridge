vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getRoles: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
  getAllPermissions: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getRoles, getAllPermissions } from '../api/client';
import Roles from '../pages/Roles';
import { renderWithProviders, makeRole } from './helpers';

const mockedGetRoles = vi.mocked(getRoles);
const mockedGetAllPermissions = vi.mocked(getAllPermissions);

describe('Roles', () => {
  beforeEach(() => {
    mockedGetRoles.mockResolvedValue([]);
    mockedGetAllPermissions.mockResolvedValue([]);
  });

  it('renders roles table', async () => {
    mockedGetRoles.mockResolvedValue([
      makeRole(),
      makeRole({ id: 2, name: 'custom', description: 'Custom', is_system: false, permissions: ['query.execute'] }),
    ]);

    renderWithProviders(<Roles />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    expect(screen.getByText('custom')).toBeInTheDocument();
  });

  it('filters roles by search text', async () => {
    mockedGetRoles.mockResolvedValue([
      makeRole(),
      makeRole({ id: 2, name: 'analyst', description: 'Reports only', is_system: false, permissions: ['query.execute'] }),
    ]);

    renderWithProviders(<Roles />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    const search = screen.getByRole('searchbox', { name: /search roles/i });
    await userEvent.type(search, 'reports');

    expect(screen.queryByText('admin')).not.toBeInTheDocument();
    expect(screen.getByText('analyst')).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, 'missing');
    expect(screen.getByText(/No matching roles|일치하는 역할/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Clear search|검색 지우기/i }));
    expect(screen.getByText('admin')).toBeInTheDocument();
    expect(screen.getByText('analyst')).toBeInTheDocument();
  });

  it('renders empty state when loading', () => {
    mockedGetRoles.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<Roles />);

    expect(screen.getByText('Loading roles...')).toBeInTheDocument();
  });

  it('renders empty state when no roles exist', async () => {
    renderWithProviders(<Roles />);

    await waitFor(() => {
      expect(screen.getByText('No roles')).toBeInTheDocument();
    });
  });

  it('system roles do not have delete button', async () => {
    mockedGetRoles.mockResolvedValue([makeRole()]);

    renderWithProviders(<Roles />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: 'Edit role admin' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('non-system roles have delete button', async () => {
    mockedGetRoles.mockResolvedValue([
      makeRole({ id: 2, name: 'custom', description: 'Custom', is_system: false, permissions: ['query.execute'] }),
    ]);

    renderWithProviders(<Roles />);

    await waitFor(() => {
      expect(screen.getByText('custom')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: 'Edit role custom' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete role custom' })).toBeInTheDocument();
  });

  it('hides write actions for users with read-only role permission', async () => {
    mockedGetRoles.mockResolvedValue([
      makeRole({ id: 2, name: 'custom', description: 'Custom', is_system: false, permissions: ['query.execute'] }),
    ]);

    renderWithProviders(<Roles />, {
      permissions: ['admin.roles.read'],
    });

    await waitFor(() => {
      expect(screen.getByText('custom')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: '+ Add Role' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('opens create modal on add button click', async () => {
    renderWithProviders(<Roles />);

    await userEvent.click(screen.getByRole('button', { name: '+ Add Role' }));

    expect(screen.getByRole('heading', { name: 'Add Role' })).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Add Role' })).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveAttribute('id', 'role-name');
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveAttribute('id', 'role-description');
  });
});
