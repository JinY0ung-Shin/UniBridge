vi.mock('../components/useAuth', () => ({
  useAuth: vi.fn(() => ({
    username: 'currentadmin',
    authenticated: true,
    token: 'fake',
    initialized: true,
    logout: vi.fn(),
  })),
}));

vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getUsers: vi.fn(),
  createKeycloakUser: vi.fn(),
  changeUserRole: vi.fn(),
  resetUserPassword: vi.fn(),
  toggleUserEnabled: vi.fn(),
  deleteKeycloakUser: vi.fn(),
  getAuthRoles: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getUsers, getAuthRoles } from '../api/client';
import Users from '../pages/Users';
import { renderWithProviders, makeUser, ADMIN_PERMISSIONS, VIEWER_PERMISSIONS } from './helpers';

const mockedGetUsers = vi.mocked(getUsers);
const mockedGetAuthRoles = vi.mocked(getAuthRoles);

describe('Users', () => {
  beforeEach(() => {
    mockedGetUsers.mockResolvedValue({ users: [], total: 0 });
    mockedGetAuthRoles.mockResolvedValue([]);
  });

  it('renders users table', async () => {
    const user = makeUser();
    mockedGetUsers.mockResolvedValue({ users: [user], total: 1 });

    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });

    await waitFor(() => {
      expect(screen.getByText('testuser')).toBeInTheDocument();
    });

    expect(screen.getByText('test@example.com')).toBeInTheDocument();
  });

  it('renders empty state when loading', () => {
    mockedGetUsers.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });

    expect(screen.getByText('Loading users...')).toBeInTheDocument();
  });

  it('shows action buttons for admin users', async () => {
    const user = makeUser();
    mockedGetUsers.mockResolvedValue({ users: [user], total: 1 });

    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });

    await waitFor(() => {
      expect(screen.getByText('testuser')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: 'Role' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('hides action buttons for viewer users', async () => {
    const user = makeUser();
    mockedGetUsers.mockResolvedValue({ users: [user], total: 1 });

    renderWithProviders(<Users />, { permissions: VIEWER_PERMISSIONS });

    await waitFor(() => {
      expect(screen.getByText('testuser')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: 'Role' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '+ Add User' })).not.toBeInTheDocument();
  });

  it('hides actions for current user row', async () => {
    const currentUser = makeUser({ username: 'currentadmin' });
    mockedGetUsers.mockResolvedValue({ users: [currentUser], total: 1 });

    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });

    await waitFor(() => {
      expect(screen.getByText('currentadmin')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: 'Role' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });
});
