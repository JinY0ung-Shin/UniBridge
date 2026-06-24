import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getRoles,
  createRole,
  updateRole,
  deleteRole,
  getAllPermissions,
  type RoleInfo,
} from '../api/client';
import ResourceModal from '../components/ResourceModal';
import DataTablePageHeader from '../components/DataTablePageHeader';
import { useCanWrite } from '../components/useCanWrite';
import { useResourceMutation } from '../components/useResourceMutation';
import './Roles.css';

const ROLES_KEY = ['roles'];

function groupPermissions(perms: string[]): Record<string, string[]> {
  const groups: Record<string, string[]> = {};
  for (const p of perms) {
    const parts = p.split('.');
    const category = parts[0];
    groups[category] = groups[category] || [];
    groups[category].push(p);
  }
  return groups;
}

function Roles() {
  const { t } = useTranslation();
  const canWrite = useCanWrite('admin.roles.write');

  const [showModal, setShowModal] = useState(false);
  const [editingRole, setEditingRole] = useState<RoleInfo | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [selectedPerms, setSelectedPerms] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [roleSearch, setRoleSearch] = useState('');

  const rolesQuery = useQuery({ queryKey: ROLES_KEY, queryFn: getRoles });
  const permsQuery = useQuery({ queryKey: ['all-permissions'], queryFn: getAllPermissions });

  const createMutation = useResourceMutation({
    mutationFn: (data: { name: string; description: string; permissions: string[] }) => createRole(data),
    invalidateKey: ROLES_KEY,
    onSuccess: () => closeModal(),
    errorMode: { kind: 'setError', setError, fallback: t('roles.createFailed') },
  });

  const updateMutation = useResourceMutation({
    mutationFn: ({ id, body }: { id: number; body: { description?: string; permissions?: string[] } }) =>
      updateRole(id, body),
    invalidateKey: ROLES_KEY,
    onSuccess: () => closeModal(),
    errorMode: { kind: 'setError', setError, fallback: t('roles.updateFailed') },
  });

  const deleteMutation = useResourceMutation({
    mutationFn: (id: number) => deleteRole(id),
    invalidateKey: ROLES_KEY,
    errorMode: { kind: 'toast', title: t('roles.deleteFailed') },
  });

  const roles = rolesQuery.data ?? [];
  const allPerms = permsQuery.data ?? [];
  const permGroups = groupPermissions(allPerms);
  const normalizedRoleSearch = roleSearch.trim().toLowerCase();
  const filteredRoles = normalizedRoleSearch
    ? roles.filter((role) => [
        role.name,
        role.description,
        role.is_system ? t('roles.system') : '',
        ...role.permissions,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(normalizedRoleSearch))
    : roles;

  function openCreate() {
    setEditingRole(null);
    setName('');
    setDescription('');
    setSelectedPerms(new Set());
    setError('');
    setShowModal(true);
  }

  function openEdit(role: RoleInfo) {
    setEditingRole(role);
    setName(role.name);
    setDescription(role.description);
    setSelectedPerms(new Set(role.permissions));
    setError('');
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingRole(null);
    setError('');
  }

  function togglePerm(perm: string) {
    setSelectedPerms((prev) => {
      const next = new Set(prev);
      if (next.has(perm)) {
        next.delete(perm);
      } else {
        next.add(perm);
      }
      return next;
    });
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    const perms = Array.from(selectedPerms);

    if (editingRole) {
      updateMutation.mutate({
        id: editingRole.id,
        body: { description, permissions: perms },
      });
    } else {
      if (!name.trim()) return;
      createMutation.mutate({ name: name.trim(), description, permissions: perms });
    }
  }

  function handleDelete(role: RoleInfo) {
    if (window.confirm(t('roles.deleteConfirm', { name: role.name }))) {
      deleteMutation.mutate(role.id);
    }
  }

  const isSaving = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="roles-page">
      <DataTablePageHeader
        title={t('roles.title')}
        subtitle={t('roles.subtitle')}
        canAdd={canWrite}
        addLabel={t('roles.addRole')}
        onAdd={openCreate}
        extra={roles.length > 0 ? (
          <input
            className="role-search-input"
            type="search"
            value={roleSearch}
            onChange={(event) => setRoleSearch(event.target.value)}
            placeholder={t('roles.searchPlaceholder')}
            aria-label={t('roles.searchPlaceholder')}
          />
        ) : null}
      />

      {rolesQuery.isLoading && <div className="loading-message" role="status">{t('roles.loadingRoles')}</div>}
      {rolesQuery.isError && <div className="error-banner" role="alert">{t('roles.loadFailed')}</div>}

      {roles.length > 0 && filteredRoles.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('common.name')}</th>
                <th scope="col">{t('roles.description')}</th>
                <th scope="col">{t('permissions.title')}</th>
                <th scope="col">{t('common.type')}</th>
                <th scope="col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {filteredRoles.map((role) => {
                const isDeleting = deleteMutation.isPending && deleteMutation.variables === role.id;
                return (
                  <tr key={role.id}>
                    <td className="cell-alias">{role.name}</td>
                    <td>{role.description || '—'}</td>
                    <td className="perm-count">{t('roles.permissionCount', { count: role.permissions.length })}</td>
                    <td>{role.is_system ? <span className="system-badge">{t('roles.system')}</span> : '—'}</td>
                    <td>
                      {canWrite && (
                        <div className="action-buttons">
                          <button
                            type="button"
                            className="btn btn-sm btn-secondary"
                            aria-label={t('roles.editRole', { name: role.name })}
                            onClick={() => openEdit(role)}
                          >
                            {t('common.edit')}
                          </button>
                          {!role.is_system && (
                            <button
                              type="button"
                              className="btn btn-sm btn-danger"
                              aria-label={t('roles.deleteRole', { name: role.name })}
                              onClick={() => handleDelete(role)}
                              disabled={deleteMutation.isPending}
                              aria-busy={isDeleting}
                            >
                              {isDeleting ? t('common.deleting') : t('common.delete')}
                            </button>
                          )}
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

      {!rolesQuery.isLoading && roles.length > 0 && filteredRoles.length === 0 && !rolesQuery.isError && (
        <div className="empty-state">
          <h3>{t('roles.noSearchResults')}</h3>
          <p>{t('roles.noSearchResultsDesc')}</p>
          <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setRoleSearch('')}>
            {t('common.clearSearch')}
          </button>
        </div>
      )}

      {!rolesQuery.isLoading && roles.length === 0 && !rolesQuery.isError && (
        <div className="empty-state">
          <h3>{t('roles.noRoles')}</h3>
          <p>{t('roles.noRolesDesc')}</p>
        </div>
      )}

      {canWrite && showModal && (
        <ResourceModal
          title={editingRole ? t('roles.editTitle', { name: editingRole.name }) : t('roles.addTitle')}
          onClose={closeModal}
          closeLabel={t('common.close')}
          style={{ width: 600 }}
        >
          <form onSubmit={handleSubmit}>
            <div className="form-grid">
              <div className="form-group form-group--full">
                <label htmlFor="role-name">{t('common.name')}</label>
                <input
                  id="role-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="role-name"
                  required
                  disabled={!!editingRole}
                  aria-label={t('common.name')}
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="role-description">{t('roles.description')}</label>
                <input
                  id="role-description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Role description"
                  aria-label={t('roles.description')}
                />
              </div>
            </div>

            <div className="perm-grid">
              {Object.entries(permGroups).map(([category, perms]) => (
                <div key={category} className="perm-category">
                  <div className="perm-category-title">{category}</div>
                  <div className="perm-checks">
                    {perms.map((perm) => (
                      <label key={perm} className="perm-check">
                        <input
                          type="checkbox"
                          checked={selectedPerms.has(perm)}
                          onChange={() => togglePerm(perm)}
                        />
                        <span className="perm-name">{perm.split('.').slice(1).join('.')}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {error && <div className="form-error" role="alert">{error}</div>}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
              <button type="submit" className="btn btn-primary" disabled={isSaving} aria-busy={isSaving}>
                {isSaving ? t('common.saving') : editingRole ? t('common.update') : t('common.create')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}
    </div>
  );
}

export default Roles;
