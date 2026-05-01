vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getRoles: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
  getAllPermissions: vi.fn(),
}));

import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getRoles,
  createRole,
  updateRole,
  deleteRole,
  getAllPermissions,
} from '../api/client';
import Roles from '../pages/Roles';
import { renderWithProviders, makeRole, ADMIN_PERMISSIONS, VIEWER_PERMISSIONS } from './helpers';

const mocks = {
  list: vi.mocked(getRoles),
  create: vi.mocked(createRole),
  update: vi.mocked(updateRole),
  remove: vi.mocked(deleteRole),
  perms: vi.mocked(getAllPermissions),
};

describe('Roles page flows', () => {
  beforeEach(() => {
    Object.values(mocks).forEach((m) => m.mockReset());
    mocks.perms.mockResolvedValue([
      'query.execute',
      'query.databases.read',
      'admin.roles.write',
    ]);
    mocks.list.mockResolvedValue([
      makeRole({ id: 1, name: 'admin', is_system: true, permissions: ['query.execute'] }),
      makeRole({ id: 2, name: 'custom', is_system: false, description: 'My role', permissions: ['query.databases.read'] }),
    ]);
  });

  it('renders roles list with badges and permission count', async () => {
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('admin')).toBeInTheDocument());
    expect(screen.getByText('custom')).toBeInTheDocument();
    expect(screen.getByText('My role')).toBeInTheDocument();
    expect(screen.getByText(/System|시스템/)).toBeInTheDocument();
  });

  it('shows empty dash for role without description', async () => {
    mocks.list.mockResolvedValue([
      makeRole({ id: 3, name: 'no-desc', description: '', permissions: [] }),
    ]);
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('no-desc')).toBeInTheDocument());
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('hides write actions for viewer', async () => {
    renderWithProviders(<Roles />, { permissions: VIEWER_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('admin')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /Add Role|역할 추가/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Edit$|^편집$/ })).not.toBeInTheDocument();
  });

  it('opens create modal and submits new role with toggled permissions', async () => {
    mocks.create.mockResolvedValue(makeRole());
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('admin')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Add Role|역할 추가/i }));
    await waitFor(() => expect(screen.getByPlaceholderText('role-name')).toBeInTheDocument());

    const nameInput = screen.getByPlaceholderText('role-name');
    await userEvent.type(nameInput, 'analyst');

    const descInput = screen.getByPlaceholderText('Role description');
    await userEvent.type(descInput, 'Read-only analytics');

    // Toggle permission checkbox
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    fireEvent.click(checkboxes[1]); // toggle back to test set delete branch

    fireEvent.submit(nameInput.closest('form')!);
    await waitFor(() => expect(mocks.create).toHaveBeenCalled());
    expect(mocks.create.mock.calls[0][0].name).toBe('analyst');
    expect(mocks.create.mock.calls[0][0].description).toBe('Read-only analytics');
  });

  it('create with empty name short-circuits without API call', async () => {
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('admin')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Add Role|역할 추가/i }));
    await waitFor(() => expect(screen.getByPlaceholderText('role-name')).toBeInTheDocument());
    const form = screen.getByPlaceholderText('role-name').closest('form')!;
    // Submit with empty name (should be blocked by required attribute, but our handler also guards trim)
    fireEvent.submit(form);
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it('opens edit modal with name disabled and submits update', async () => {
    mocks.update.mockResolvedValue(makeRole({ id: 2 }));
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('custom')).toBeInTheDocument());

    const editButtons = screen.getAllByRole('button', { name: /^Edit$|^편집$/ });
    // Edit the second (custom) role to allow delete button visibility too
    fireEvent.click(editButtons[1]);
    await waitFor(() => {
      const nameInput = screen.getByPlaceholderText('role-name') as HTMLInputElement;
      expect(nameInput.disabled).toBe(true);
      expect(nameInput.value).toBe('custom');
    });

    fireEvent.submit(screen.getByPlaceholderText('role-name').closest('form')!);
    await waitFor(() => expect(mocks.update).toHaveBeenCalled());
    expect(mocks.update.mock.calls[0][0]).toBe(2);
  });

  it('create error path shows API detail', async () => {
    mocks.create.mockRejectedValue({ response: { data: { detail: 'name taken' } } });
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('admin')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Add Role|역할 추가/i }));
    await waitFor(() => expect(screen.getByPlaceholderText('role-name')).toBeInTheDocument());
    const nameInput = screen.getByPlaceholderText('role-name');
    await userEvent.type(nameInput, 'dup');
    fireEvent.submit(nameInput.closest('form')!);
    await waitFor(() => expect(screen.getByText('name taken')).toBeInTheDocument());
  });

  it('create generic error shows fallback', async () => {
    mocks.create.mockRejectedValue(new Error('boom'));
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('admin')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /Add Role|역할 추가/i }));
    await waitFor(() => expect(screen.getByPlaceholderText('role-name')).toBeInTheDocument());
    const nameInput = screen.getByPlaceholderText('role-name');
    await userEvent.type(nameInput, 'oops');
    fireEvent.submit(nameInput.closest('form')!);
    await waitFor(() => {
      expect(screen.getByText(/Failed to create|역할.*생성/i)).toBeInTheDocument();
    });
  });

  it('update error path shows fallback', async () => {
    mocks.update.mockRejectedValue(new Error('boom'));
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('custom')).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole('button', { name: /^Edit$|^편집$/ })[1]);
    await waitFor(() => expect(screen.getByPlaceholderText('role-name')).toBeInTheDocument());
    fireEvent.submit(screen.getByPlaceholderText('role-name').closest('form')!);
    await waitFor(() => {
      expect(screen.getByText(/Failed to update|역할.*업데이트|업데이트.*실패/i)).toBeInTheDocument();
    });
  });

  it('update with API detail error', async () => {
    mocks.update.mockRejectedValue({ response: { data: { detail: 'cannot edit' } } });
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('custom')).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole('button', { name: /^Edit$|^편집$/ })[1]);
    await waitFor(() => expect(screen.getByPlaceholderText('role-name')).toBeInTheDocument());
    fireEvent.submit(screen.getByPlaceholderText('role-name').closest('form')!);
    await waitFor(() => expect(screen.getByText('cannot edit')).toBeInTheDocument());
  });

  it('delete confirmed calls API', async () => {
    mocks.remove.mockResolvedValue();
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('custom')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    await waitFor(() => expect(mocks.remove).toHaveBeenCalled());
    expect(mocks.remove.mock.calls[0][0]).toBe(2);
    cs.mockRestore();
  });

  it('delete cancelled does nothing', async () => {
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('custom')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    expect(mocks.remove).not.toHaveBeenCalled();
    cs.mockRestore();
  });

  it('delete error path alerts API detail', async () => {
    mocks.remove.mockRejectedValue({ response: { data: { detail: 'cannot delete' } } });
    const cs = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => expect(screen.getByText('custom')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^Delete$|^삭제$/ }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith('cannot delete'));
    alertSpy.mockRestore();
    cs.mockRestore();
  });

  it('shows error banner when fetch fails', async () => {
    mocks.list.mockRejectedValue(new Error('boom'));
    renderWithProviders(<Roles />, { permissions: ADMIN_PERMISSIONS });
    await waitFor(() => {
      expect(screen.getByText(/Failed to load|불러오지 못/i)).toBeInTheDocument();
    });
  });
});
