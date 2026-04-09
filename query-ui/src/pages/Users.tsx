import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getUsers,
  createKeycloakUser,
  changeUserRole,
  resetUserPassword,
  deleteKeycloakUser,
  getAuthRoles,
  type KeycloakUser,
} from '../api/client';
import { usePermissions } from '../components/PermissionContext';
import { useAuth } from '../components/AuthProvider';
import './Users.css';

function roleBadgeClass(role: string | null): string {
  if (!role) return 'role-badge role-badge--default';
  const r = role.toLowerCase();
  if (r === 'admin') return 'role-badge role-badge--admin';
  if (r === 'developer') return 'role-badge role-badge--developer';
  if (r === 'viewer') return 'role-badge role-badge--viewer';
  return 'role-badge role-badge--default';
}

function extractErrorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const axiosErr = err as { response?: { data?: { detail?: string } } };
    return axiosErr.response?.data?.detail ?? fallback;
  }
  return fallback;
}

type ModalMode = 'create' | 'role' | 'password';

function Users() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const { username: currentUsername } = useAuth();
  const canWrite = permissions.includes('admin.roles.write');

  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [modalMode, setModalMode] = useState<ModalMode | null>(null);
  const [selectedUser, setSelectedUser] = useState<KeycloakUser | null>(null);
  const [error, setError] = useState('');

  // Create form state
  const [newUsername, setNewUsername] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newRole, setNewRole] = useState('');

  // Role change state
  const [changeRoleValue, setChangeRoleValue] = useState('');

  // Password reset state
  const [resetPwValue, setResetPwValue] = useState('');
  const [resetPwTemporary, setResetPwTemporary] = useState(true);

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const usersQuery = useQuery({
    queryKey: ['users', debouncedSearch],
    queryFn: () => getUsers(debouncedSearch ? { search: debouncedSearch } : undefined),
  });

  const rolesQuery = useQuery({
    queryKey: ['auth-roles'],
    queryFn: getAuthRoles,
  });

  const createMutation = useMutation({
    mutationFn: createKeycloakUser,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      closeModal();
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, 'Failed to create user')),
  });

  const changeRoleMutation = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) => changeUserRole(userId, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      closeModal();
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, 'Failed to change role')),
  });

  const resetPasswordMutation = useMutation({
    mutationFn: ({ userId, password, temporary }: { userId: string; password: string; temporary: boolean }) =>
      resetUserPassword(userId, { password, temporary }),
    onSuccess: () => {
      closeModal();
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, 'Failed to reset password')),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteKeycloakUser,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
    },
    onError: (err: unknown) => {
      alert(extractErrorMessage(err, 'Failed to delete user'));
    },
  });

  const users = usersQuery.data?.users ?? [];
  const roles = rolesQuery.data ?? [];

  function openCreate() {
    setModalMode('create');
    setSelectedUser(null);
    setNewUsername('');
    setNewEmail('');
    setNewPassword('');
    setNewRole(roles[0] ?? '');
    setError('');
  }

  function openRoleChange(user: KeycloakUser) {
    setModalMode('role');
    setSelectedUser(user);
    setChangeRoleValue(user.role ?? roles[0] ?? '');
    setError('');
  }

  function openPasswordReset(user: KeycloakUser) {
    setModalMode('password');
    setSelectedUser(user);
    setResetPwValue('');
    setResetPwTemporary(true);
    setError('');
  }

  function closeModal() {
    setModalMode(null);
    setSelectedUser(null);
    setError('');
  }

  function handleCreateSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    if (!newUsername.trim() || !newPassword || newPassword.length < 8) return;
    createMutation.mutate({
      username: newUsername.trim(),
      email: newEmail.trim() || undefined,
      password: newPassword,
      role: newRole,
    });
  }

  function handleRoleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    if (!selectedUser || !changeRoleValue) return;
    changeRoleMutation.mutate({ userId: selectedUser.id, role: changeRoleValue });
  }

  function handlePasswordSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    if (!selectedUser || !resetPwValue || resetPwValue.length < 8) return;
    resetPasswordMutation.mutate({
      userId: selectedUser.id,
      password: resetPwValue,
      temporary: resetPwTemporary,
    });
  }

  function handleDelete(user: KeycloakUser) {
    if (window.confirm(`Delete user "${user.username}"?`)) {
      deleteMutation.mutate(user.id);
    }
  }

  const isSaving = createMutation.isPending || changeRoleMutation.isPending || resetPasswordMutation.isPending;

  return (
    <div className="users-page">
      <div className="page-header">
        <div>
          <h1>Users</h1>
          <p className="page-subtitle">Manage Keycloak users and roles</p>
        </div>
        {canWrite && (
          <button className="btn btn-primary" onClick={openCreate}>+ Add User</button>
        )}
      </div>

      <div className="search-bar">
        <input
          type="text"
          placeholder="Search users..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {usersQuery.isLoading && <div className="loading-message">Loading users...</div>}
      {usersQuery.isError && <div className="error-banner">Failed to load users.</div>}

      {users.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Username</th>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id} className={user.enabled ? '' : 'row-disabled'}>
                  <td className="cell-alias">{user.username}</td>
                  <td>{user.email || '—'}</td>
                  <td>
                    <span className={roleBadgeClass(user.role)}>
                      {user.role || '—'}
                    </span>
                  </td>
                  <td>
                    <span className={user.enabled ? 'status-active' : 'status-disabled'}>
                      {user.enabled ? 'Active' : 'Disabled'}
                    </span>
                  </td>
                  <td>
                    {canWrite && user.username !== currentUsername && (
                      <div className="action-buttons">
                        <button className="btn btn-sm btn-secondary" onClick={() => openRoleChange(user)}>Role</button>
                        <button className="btn btn-sm btn-secondary" onClick={() => openPasswordReset(user)}>Reset PW</button>
                        <button
                          className="btn btn-sm btn-danger"
                          onClick={() => handleDelete(user)}
                          disabled={deleteMutation.isPending}
                        >
                          Delete
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create User Modal */}
      {modalMode === 'create' && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Add User</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handleCreateSubmit}>
              <div className="form-grid">
                <div className="form-group form-group--full">
                  <label>Username</label>
                  <input
                    value={newUsername}
                    onChange={(e) => setNewUsername(e.target.value)}
                    placeholder="username"
                    required
                  />
                </div>
                <div className="form-group form-group--full">
                  <label>Email (optional)</label>
                  <input
                    type="email"
                    value={newEmail}
                    onChange={(e) => setNewEmail(e.target.value)}
                    placeholder="user@example.com"
                  />
                </div>
                <div className="form-group form-group--full">
                  <label>Password (min 8 characters)</label>
                  <input
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="********"
                    minLength={8}
                    required
                  />
                </div>
                <div className="form-group form-group--full">
                  <label>Role</label>
                  <select value={newRole} onChange={(e) => setNewRole(e.target.value)} required>
                    {roles.map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                  </select>
                </div>
              </div>

              {error && <div className="form-error">{error}</div>}

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={isSaving}>
                  {createMutation.isPending ? 'Creating...' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Change Role Modal */}
      {modalMode === 'role' && selectedUser && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Change Role for &quot;{selectedUser.username}&quot;</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handleRoleSubmit}>
              <div className="form-grid">
                <div className="form-group form-group--full">
                  <label>Role</label>
                  <select value={changeRoleValue} onChange={(e) => setChangeRoleValue(e.target.value)} required>
                    {roles.map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                  </select>
                </div>
              </div>

              {error && <div className="form-error">{error}</div>}

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={isSaving}>
                  {changeRoleMutation.isPending ? 'Saving...' : 'Update Role'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Reset Password Modal */}
      {modalMode === 'password' && selectedUser && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Reset Password for &quot;{selectedUser.username}&quot;</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handlePasswordSubmit}>
              <div className="form-grid">
                <div className="form-group form-group--full">
                  <label>New Password (min 8 characters)</label>
                  <input
                    type="password"
                    value={resetPwValue}
                    onChange={(e) => setResetPwValue(e.target.value)}
                    placeholder="********"
                    minLength={8}
                    required
                  />
                </div>
                <div className="form-group form-group--full">
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <input
                      type="checkbox"
                      checked={resetPwTemporary}
                      onChange={(e) => setResetPwTemporary(e.target.checked)}
                    />
                    Temporary (user must change on next login)
                  </label>
                </div>
              </div>

              {error && <div className="form-error">{error}</div>}

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>Cancel</button>
                <button type="submit" className="btn btn-primary" disabled={isSaving}>
                  {resetPasswordMutation.isPending ? 'Resetting...' : 'Reset Password'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default Users;
