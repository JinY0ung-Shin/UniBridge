import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getUsers,
  createKeycloakUser,
  changeUserRole,
  resetUserPassword,
  toggleUserEnabled,
  deleteKeycloakUser,
  getAuthRoles,
  type KeycloakUser,
} from '../api/client';
import { useCanWrite } from '../components/useCanWrite';
import { useAuth } from '../components/useAuth';
import { useToast } from '../components/useToast';
import ResourceModal from '../components/ResourceModal';
import './Users.css';

function roleBadgeClass(role: string | null): string {
  if (!role) return 'role-badge role-badge--pending';
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
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { username: currentUsername } = useAuth();
  const { addToast } = useToast();
  const canWrite = useCanWrite('admin.users.write');

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
    onError: (err: unknown) => setError(extractErrorMessage(err, t('users.createFailed'))),
  });

  const changeRoleMutation = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) => changeUserRole(userId, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
      closeModal();
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('users.changeRoleFailed'))),
  });

  const resetPasswordMutation = useMutation({
    mutationFn: ({ userId, password, temporary }: { userId: string; password: string; temporary: boolean }) =>
      resetUserPassword(userId, { password, temporary }),
    onSuccess: () => {
      closeModal();
    },
    onError: (err: unknown) => setError(extractErrorMessage(err, t('users.resetPasswordFailed'))),
  });

  const toggleEnabledMutation = useMutation({
    mutationFn: ({ userId, enabled }: { userId: string; enabled: boolean }) =>
      toggleUserEnabled(userId, enabled),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
    },
    onError: (err: unknown) => {
      addToast({ type: 'error', title: extractErrorMessage(err, t('users.toggleFailed')) });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteKeycloakUser,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] });
    },
    onError: (err: unknown) => {
      addToast({ type: 'error', title: extractErrorMessage(err, t('users.deleteFailed')) });
    },
  });

  const users = usersQuery.data?.users ?? [];
  const roles = rolesQuery.data ?? [];
  // Prefer 'user' as the default when assigning a role (approving), so an admin
  // never accidentally grants 'admin' from the default selection.
  const defaultAssignRole = roles.includes('user') ? 'user' : (roles[0] ?? '');

  function openCreate() {
    setModalMode('create');
    setSelectedUser(null);
    setNewUsername('');
    setNewEmail('');
    setNewPassword('');
    setNewRole(defaultAssignRole);
    setError('');
  }

  function openRoleChange(user: KeycloakUser) {
    setModalMode('role');
    setSelectedUser(user);
    setChangeRoleValue(user.role ?? defaultAssignRole);
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

  function handleToggleEnabled(user: KeycloakUser) {
    const msg = user.enabled
      ? t('users.disableConfirm', { name: user.username })
      : t('users.enableConfirm', { name: user.username });
    if (window.confirm(msg)) {
      toggleEnabledMutation.mutate({ userId: user.id, enabled: !user.enabled });
    }
  }

  function handleDelete(user: KeycloakUser) {
    if (window.confirm(t('users.deleteConfirm', { name: user.username }))) {
      deleteMutation.mutate(user.id);
    }
  }

  const isSaving = createMutation.isPending || changeRoleMutation.isPending || resetPasswordMutation.isPending;

  return (
    <div className="users-page">
      <div className="page-header">
        <div>
          <h1>{t('users.title')}</h1>
          <p className="page-subtitle">{t('users.subtitle')}</p>
        </div>
        {canWrite && (
          <button type="button" className="btn btn-primary" onClick={openCreate}>{t('users.addUser')}</button>
        )}
      </div>

      <div className="search-bar">
        <input
          type="search"
          placeholder={t('users.searchPlaceholder')}
          aria-label={t('users.searchPlaceholder')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {usersQuery.isLoading && <div className="loading-message" role="status">{t('users.loadingUsers')}</div>}
      {usersQuery.isError && <div className="error-banner" role="alert">{t('users.loadFailed')}</div>}

      {users.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('connections.username')}</th>
                <th scope="col">{t('users.email')}</th>
                <th scope="col">{t('users.role')}</th>
                <th scope="col">{t('common.status')}</th>
                <th scope="col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => {
                const isToggling = toggleEnabledMutation.isPending && toggleEnabledMutation.variables?.userId === user.id;
                const isDeleting = deleteMutation.isPending && deleteMutation.variables === user.id;
                return (
                <tr key={user.id} className={user.enabled ? '' : 'row-disabled'}>
                  <td className="cell-alias">{user.username}</td>
                  <td>{user.email || '—'}</td>
                  <td>
                    <span className={roleBadgeClass(user.role)}>
                      {user.role || t('users.pending')}
                    </span>
                  </td>
                  <td>
                    <span className={user.enabled ? 'status-active' : 'status-disabled'}>
                      {user.enabled ? t('common.active') : t('common.disabled')}
                    </span>
                  </td>
                  <td>
                    {canWrite && user.username !== currentUsername && (
                      <div className="action-buttons">
                        <button
                          type="button"
                          className="btn btn-sm btn-secondary"
                          aria-label={t('users.changeRoleFor', { name: user.username })}
                          onClick={() => openRoleChange(user)}
                        >
                          {t('users.role')}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-secondary"
                          aria-label={t('users.resetPasswordFor', { name: user.username })}
                          onClick={() => openPasswordReset(user)}
                        >
                          {t('users.resetPw')}
                        </button>
                        <button
                          type="button"
                          className={`btn btn-sm ${user.enabled ? 'btn-warning' : 'btn-success'}`}
                          aria-label={t(user.enabled ? 'users.disableUser' : 'users.enableUser', { name: user.username })}
                          onClick={() => handleToggleEnabled(user)}
                          disabled={toggleEnabledMutation.isPending}
                          aria-busy={isToggling}
                        >
                          {isToggling ? t('common.saving') : user.enabled ? t('users.disable') : t('users.enable')}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          aria-label={t('users.deleteUser', { name: user.username })}
                          onClick={() => handleDelete(user)}
                          disabled={deleteMutation.isPending}
                          aria-busy={isDeleting}
                        >
                          {isDeleting ? t('common.deleting') : t('common.delete')}
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {!usersQuery.isLoading && users.length === 0 && !usersQuery.isError && (
        <div className="empty-state">
          <h3>{debouncedSearch ? t('users.noSearchResults') : t('users.noUsers')}</h3>
          <p>{debouncedSearch ? t('users.noSearchResultsDesc') : t('users.noUsersDesc')}</p>
          {debouncedSearch && (
            <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setSearch('')}>
              {t('common.clearSearch')}
            </button>
          )}
        </div>
      )}

      {/* Create User Modal */}
      {modalMode === 'create' && (
        <ResourceModal title={t('users.addTitle')} onClose={closeModal} closeLabel={t('common.close')}>
          <form onSubmit={handleCreateSubmit}>
            <div className="form-grid">
              <div className="form-group form-group--full">
                <label htmlFor="user-create-username">{t('connections.username')}</label>
                <input
                  id="user-create-username"
                  value={newUsername}
                  onChange={(e) => setNewUsername(e.target.value)}
                  placeholder="username"
                  aria-label={t('connections.username')}
                  required
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="user-create-email">{t('users.emailOptional')}</label>
                <input
                  id="user-create-email"
                  type="email"
                  value={newEmail}
                  onChange={(e) => setNewEmail(e.target.value)}
                  placeholder="user@example.com"
                  aria-label={t('users.emailOptional')}
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="user-create-password">{t('users.passwordMin')}</label>
                <input
                  id="user-create-password"
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  placeholder="********"
                  aria-label={t('users.passwordMin')}
                  minLength={8}
                  required
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="user-create-role">{t('users.role')}</label>
                <select id="user-create-role" value={newRole} onChange={(e) => setNewRole(e.target.value)} aria-label={t('users.role')} required>
                  {roles.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              </div>
            </div>

            {error && <div className="form-error" role="alert">{error}</div>}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={isSaving}
                aria-busy={createMutation.isPending}
              >
                {createMutation.isPending ? t('users.creating') : t('common.create')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}

      {/* Change Role Modal */}
      {modalMode === 'role' && selectedUser && (
        <ResourceModal
          title={t('users.changeRoleTitle', { name: selectedUser.username })}
          onClose={closeModal}
          closeLabel={t('common.close')}
        >
          <form onSubmit={handleRoleSubmit}>
            <div className="form-grid">
              <div className="form-group form-group--full">
                <label htmlFor="user-change-role">{t('users.role')}</label>
                <select id="user-change-role" value={changeRoleValue} onChange={(e) => setChangeRoleValue(e.target.value)} aria-label={t('users.role')} required>
                  {roles.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              </div>
            </div>

            {error && <div className="form-error" role="alert">{error}</div>}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={isSaving}
                aria-busy={changeRoleMutation.isPending}
              >
                {changeRoleMutation.isPending ? t('common.saving') : t('users.updateRole')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}

      {/* Reset Password Modal */}
      {modalMode === 'password' && selectedUser && (
        <ResourceModal
          title={t('users.resetPasswordTitle', { name: selectedUser.username })}
          onClose={closeModal}
          closeLabel={t('common.close')}
        >
          <form onSubmit={handlePasswordSubmit}>
            <div className="form-grid">
              <div className="form-group form-group--full">
                <label htmlFor="user-reset-password">{t('users.newPassword')}</label>
                <input
                  id="user-reset-password"
                  type="password"
                  value={resetPwValue}
                  onChange={(e) => setResetPwValue(e.target.value)}
                  placeholder="********"
                  aria-label={t('users.newPassword')}
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
                  {t('users.temporary')}
                </label>
              </div>
            </div>

            {error && <div className="form-error" role="alert">{error}</div>}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={isSaving}
                aria-busy={resetPasswordMutation.isPending}
              >
                {resetPasswordMutation.isPending ? t('users.resetting') : t('users.resetPassword')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}
    </div>
  );
}

export default Users;
