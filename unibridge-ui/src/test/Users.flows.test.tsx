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

import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getUsers,
  getAuthRoles,
  createKeycloakUser,
  changeUserRole,
  resetUserPassword,
  toggleUserEnabled,
  deleteKeycloakUser,
} from '../api/client';
import Users from '../pages/Users';
import { renderWithProviders, makeUser, ADMIN_PERMISSIONS } from './helpers';

const mocks = {
  getUsers: vi.mocked(getUsers),
  getRoles: vi.mocked(getAuthRoles),
  create: vi.mocked(createKeycloakUser),
  changeRole: vi.mocked(changeUserRole),
  reset: vi.mocked(resetUserPassword),
  toggle: vi.mocked(toggleUserEnabled),
  remove: vi.mocked(deleteKeycloakUser),
};

describe('Users page flows', () => {
  beforeEach(() => {
    Object.values(mocks).forEach((m) => m.mockReset());
    mocks.getRoles.mockResolvedValue(['admin', 'developer', 'viewer']);
    mocks.getUsers.mockResolvedValue({
      users: [makeUser({ id: 'u-1', username: 'alice', enabled: true, role: 'developer' })],
      total: 1,
    });
  });

  it('debounced search triggers refetch with new query', async () => {
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const initialCalls = mocks.getUsers.mock.calls.length;
    const search = screen.getByPlaceholderText(/Search|검색/i);
    await userEvent.type(search, 'bob');
    await waitFor(
      () => {
        expect(mocks.getUsers.mock.calls.length).toBeGreaterThan(initialCalls);
        expect(mocks.getUsers.mock.calls.at(-1)?.[0]).toEqual({ search: 'bob' });
      },
      { timeout: 2000 },
    );
  });

  it('opens create modal, validates short password, then submits', async () => {
    mocks.create.mockResolvedValue(makeUser());
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Add User|사용자 추가/i }));
    await waitFor(() => expect(screen.getByPlaceholderText('username')).toBeInTheDocument());

    const usernameInput = screen.getByPlaceholderText('username');
    const pwInputs = screen.getAllByDisplayValue(''); // empty inputs
    const pwInput = pwInputs.find((el) => (el as HTMLInputElement).type === 'password')!;

    await userEvent.type(usernameInput, 'newuser');
    await userEvent.type(pwInput, 'short'); // < 8

    fireEvent.submit(usernameInput.closest('form')!);
    expect(mocks.create).not.toHaveBeenCalled();

    await userEvent.clear(pwInput);
    await userEvent.type(pwInput, 'longpassword123');
    fireEvent.submit(usernameInput.closest('form')!);
    await waitFor(() => expect(mocks.create).toHaveBeenCalled());
    expect(mocks.create.mock.calls[0][0].username).toBe('newuser');
  });

  it('opens role-change modal and submits', async () => {
    mocks.changeRole.mockResolvedValue(makeUser());
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /^Role$|^역할$/ }));
    await waitFor(() =>
      expect(screen.getByText(/Change.*Role|역할.*변경/i)).toBeInTheDocument(),
    );

    const select = screen.getByRole('combobox');
    fireEvent.change(select, { target: { value: 'admin' } });
    fireEvent.submit(select.closest('form')!);
    await waitFor(() => expect(mocks.changeRole).toHaveBeenCalled());
    expect(mocks.changeRole.mock.calls[0]).toEqual(['u-1', 'admin']);
  });

  it('opens password-reset modal and submits', async () => {
    mocks.reset.mockResolvedValue();
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Reset.*Password|비밀번호.*재설정|Reset PW/i }));
    await waitFor(() =>
      expect(screen.getAllByDisplayValue('').length).toBeGreaterThan(0),
    );

    const pwInput = screen
      .getAllByDisplayValue('')
      .find((el) => (el as HTMLInputElement).type === 'password')!;
    await userEvent.type(pwInput, 'short');
    fireEvent.submit(pwInput.closest('form')!);
    expect(mocks.reset).not.toHaveBeenCalled();

    await userEvent.clear(pwInput);
    await userEvent.type(pwInput, 'newpassword123');
    fireEvent.submit(pwInput.closest('form')!);
    await waitFor(() => expect(mocks.reset).toHaveBeenCalled());
    expect(mocks.reset.mock.calls[0][0]).toBe('u-1');
    expect(mocks.reset.mock.calls[0][1].password).toBe('newpassword123');
    expect(mocks.reset.mock.calls[0][1].temporary).toBe(true);
  });

  it('toggle enable confirms and calls API', async () => {
    mocks.toggle.mockResolvedValue(makeUser());
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /^Disable$|^비활성화$/ }));
    await waitFor(() => expect(mocks.toggle).toHaveBeenCalledWith('u-1', false));
    cs.mockRestore();
  });

  it('toggle enable cancelled does nothing', async () => {
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Disable$|^비활성화$/ }));
    expect(mocks.toggle).not.toHaveBeenCalled();
    cs.mockRestore();
  });

  it('delete confirms and calls API', async () => {
    mocks.remove.mockResolvedValue();
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    await waitFor(() => expect(mocks.remove).toHaveBeenCalled());
    expect(mocks.remove.mock.calls[0][0]).toBe('u-1');
    cs.mockRestore();
  });

  it('delete error path calls alert', async () => {
    mocks.remove.mockRejectedValue({ response: { data: { detail: 'forbidden' } } });
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith('forbidden'));
    cs.mockRestore();
    alertSpy.mockRestore();
  });

  it('shows error banner when fetch users fails', async () => {
    mocks.getUsers.mockRejectedValue(new Error('boom'));
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() =>
      expect(screen.getByText(/Failed to load|불러오지 못/i)).toBeInTheDocument(),
    );
  });

  it('renders user with no role and no email', async () => {
    mocks.getUsers.mockResolvedValue({
      users: [makeUser({ id: 'u-2', username: 'no-role', enabled: false, role: null, email: null })],
      total: 1,
    });
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('no-role')).toBeInTheDocument());
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('admin role badge applied for admin user', async () => {
    mocks.getUsers.mockResolvedValue({
      users: [makeUser({ id: 'u-3', username: 'super', role: 'admin' })],
      total: 1,
    });
    const { container } = renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('super')).toBeInTheDocument());
    expect(container.querySelector('.role-badge--admin')).not.toBeNull();
  });

  it('viewer role badge applied for viewer user', async () => {
    mocks.getUsers.mockResolvedValue({
      users: [makeUser({ id: 'u-4', username: 'viewer-bob', role: 'viewer' })],
      total: 1,
    });
    const { container } = renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('viewer-bob')).toBeInTheDocument());
    expect(container.querySelector('.role-badge--viewer')).not.toBeNull();
  });

  it('default role badge applied for unknown role', async () => {
    mocks.getUsers.mockResolvedValue({
      users: [makeUser({ id: 'u-5', username: 'odd', role: 'custom' })],
      total: 1,
    });
    const { container } = renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('odd')).toBeInTheDocument());
    expect(container.querySelector('.role-badge--default')).not.toBeNull();
  });
});
