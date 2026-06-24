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
    mocks.getRoles.mockResolvedValue(['admin', 'user']);
    mocks.getUsers.mockResolvedValue({
      users: [makeUser({ id: 'u-1', username: 'alice', enabled: true, role: 'user' })],
      total: 1,
    });
  });

  it('debounced search triggers refetch with new query', async () => {
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const initialCalls = mocks.getUsers.mock.calls.length;
    const search = screen.getByRole('searchbox', { name: /Search|검색/i });
    await userEvent.type(search, 'bob');
    await waitFor(
      () => {
        expect(mocks.getUsers.mock.calls.length).toBeGreaterThan(initialCalls);
        expect(mocks.getUsers.mock.calls.at(-1)?.[0]).toEqual({ search: 'bob' });
      },
      { timeout: 2000 },
    );
  });

  it('shows no-results state when search returns no users', async () => {
    mocks.getUsers.mockImplementation(async (params) => {
      if (params?.search) return { users: [], total: 0 };
      return {
        users: [makeUser({ id: 'u-1', username: 'alice', enabled: true, role: 'user' })],
        total: 1,
      };
    });

    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    await userEvent.type(screen.getByRole('searchbox', { name: /Search|검색/i }), 'nobody');

    await waitFor(
      () => expect(screen.getByText(/No matching users|일치하는 사용자/i)).toBeInTheDocument(),
      { timeout: 2000 },
    );
    expect(screen.queryByText('alice')).not.toBeInTheDocument();

    const search = screen.getByRole('searchbox', { name: /Search|검색/i });
    await userEvent.click(screen.getByRole('button', { name: /Clear search|검색 지우기/i }));

    await waitFor(
      () => expect(screen.getByText('alice')).toBeInTheDocument(),
      { timeout: 2000 },
    );
    expect(search).toHaveValue('');
  });

  it('opens create modal, validates short password, then submits', async () => {
    mocks.create.mockResolvedValue(makeUser());
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Add User|사용자 추가/i }));
    const dialog = await screen.findByRole('dialog', { name: /Add User|사용자 추가/i });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByRole('textbox', { name: /Username|사용자 이름/i })).toBeInTheDocument();

    const usernameInput = screen.getByRole('textbox', { name: /Username|사용자 이름/i });
    const pwInput = screen.getByLabelText(/Password \(min 8 characters\)|비밀번호 \(최소 8자\)/i);
    expect(usernameInput).toHaveAttribute('id', 'user-create-username');
    expect(screen.getByRole('textbox', { name: /Email/i })).toHaveAttribute('id', 'user-create-email');
    expect(pwInput).toHaveAttribute('id', 'user-create-password');
    expect(screen.getByRole('combobox', { name: /Role|역할/i })).toHaveAttribute('id', 'user-create-role');

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

    fireEvent.click(screen.getByRole('button', { name: 'Change role for alice' }));
    const dialog = await screen.findByRole('dialog', { name: /Change.*Role|역할.*변경/i });
    expect(dialog).toHaveAttribute('aria-modal', 'true');

    const select = screen.getByRole('combobox', { name: /Role|역할/i });
    expect(select).toHaveAttribute('id', 'user-change-role');
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
    const dialog = await screen.findByRole('dialog', { name: /Reset.*Password|비밀번호.*재설정/i });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByLabelText(/New Password \(min 8 characters\)|새 비밀번호 \(최소 8자\)/i)).toBeInTheDocument();

    const pwInput = screen.getByLabelText(/New Password \(min 8 characters\)|새 비밀번호 \(최소 8자\)/i);
    expect(pwInput).toHaveAttribute('id', 'user-reset-password');
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

    fireEvent.click(screen.getByRole('button', { name: 'Disable user alice' }));
    await waitFor(() => expect(mocks.toggle).toHaveBeenCalledWith('u-1', false));
    cs.mockRestore();
  });

  it('toggle enable cancelled does nothing', async () => {
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Disable user alice' }));
    expect(mocks.toggle).not.toHaveBeenCalled();
    cs.mockRestore();
  });

  it('shows pending feedback only on the active toggle row', async () => {
    mocks.getUsers.mockResolvedValue({
      users: [
        makeUser({ id: 'u-1', username: 'alice', enabled: true, role: 'user' }),
        makeUser({ id: 'u-2', username: 'bob', enabled: true, role: 'user' }),
      ],
      total: 2,
    });
    mocks.toggle.mockReturnValue(new Promise<Awaited<ReturnType<typeof toggleUserEnabled>>>(() => {}));
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const aliceToggle = screen.getByRole('button', { name: 'Disable user alice' });
    const bobToggle = screen.getByRole('button', { name: 'Disable user bob' });
    fireEvent.click(aliceToggle);

    await waitFor(() => {
      expect(aliceToggle).toHaveAttribute('aria-busy', 'true');
      expect(aliceToggle).toHaveTextContent('Saving...');
    });
    expect(bobToggle).toHaveAttribute('aria-busy', 'false');
    expect(bobToggle).toHaveTextContent('Disable');

    cs.mockRestore();
  });

  it('delete confirms and calls API', async () => {
    mocks.remove.mockResolvedValue();
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Delete user alice' }));
    await waitFor(() => expect(mocks.remove).toHaveBeenCalled());
    expect(mocks.remove.mock.calls[0][0]).toBe('u-1');
    cs.mockRestore();
  });

  it('shows pending feedback only on the active delete row', async () => {
    mocks.getUsers.mockResolvedValue({
      users: [
        makeUser({ id: 'u-1', username: 'alice', enabled: true, role: 'user' }),
        makeUser({ id: 'u-2', username: 'bob', enabled: true, role: 'user' }),
      ],
      total: 2,
    });
    mocks.remove.mockReturnValue(new Promise<Awaited<ReturnType<typeof deleteKeycloakUser>>>(() => {}));
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const aliceDelete = screen.getByRole('button', { name: 'Delete user alice' });
    const bobDelete = screen.getByRole('button', { name: 'Delete user bob' });
    fireEvent.click(aliceDelete);

    await waitFor(() => {
      expect(aliceDelete).toHaveAttribute('aria-busy', 'true');
      expect(aliceDelete).toHaveTextContent('Deleting...');
    });
    expect(bobDelete).toHaveAttribute('aria-busy', 'false');
    expect(bobDelete).toHaveTextContent('Delete');

    cs.mockRestore();
  });

  it('delete error path shows toast with API detail', async () => {
    mocks.remove.mockRejectedValue({ response: { data: { detail: 'forbidden' } } });
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Delete user alice' }));
    await waitFor(() => expect(screen.getByText('forbidden')).toBeInTheDocument());
    cs.mockRestore();
  });

  it('shows error banner when fetch users fails', async () => {
    mocks.getUsers.mockRejectedValue(new Error('boom'));
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() =>
      expect(screen.getByText(/Failed to load|불러오지 못/i)).toBeInTheDocument(),
    );
  });

  it('renders user with no role (pending) and no email', async () => {
    mocks.getUsers.mockResolvedValue({
      users: [makeUser({ id: 'u-2', username: 'no-role', enabled: false, role: null, email: null })],
      total: 1,
    });
    renderWithProviders(<Users />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('no-role')).toBeInTheDocument());
    // email is null -> em dash; role is null -> "pending" badge (approval-gated)
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.getByText(/Pending|대기/)).toBeInTheDocument();
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
