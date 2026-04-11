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
      makeRole({ id: 2, name: 'developer', description: 'Dev', is_system: false, permissions: ['query.execute'] }),
    ]);

    renderWithProviders(<Roles />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    expect(screen.getByText('developer')).toBeInTheDocument();
  });

  it('renders empty state when loading', () => {
    mockedGetRoles.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<Roles />);

    expect(screen.getByText('Loading roles...')).toBeInTheDocument();
  });

  it('system roles do not have delete button', async () => {
    mockedGetRoles.mockResolvedValue([makeRole()]);

    renderWithProviders(<Roles />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('non-system roles have delete button', async () => {
    mockedGetRoles.mockResolvedValue([
      makeRole({ id: 2, name: 'developer', description: 'Dev', is_system: false, permissions: ['query.execute'] }),
    ]);

    renderWithProviders(<Roles />);

    await waitFor(() => {
      expect(screen.getByText('developer')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('opens create modal on add button click', async () => {
    renderWithProviders(<Roles />);

    await userEvent.click(screen.getByRole('button', { name: '+ Add Role' }));

    expect(screen.getByRole('heading', { name: 'Add Role' })).toBeInTheDocument();
  });
});
