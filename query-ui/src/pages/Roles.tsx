import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getRoles,
  createRole,
  updateRole,
  deleteRole,
  getAllPermissions,
  type RoleInfo,
} from '../api/client';
import './Roles.css';

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
  const queryClient = useQueryClient();

  const [showModal, setShowModal] = useState(false);
  const [editingRole, setEditingRole] = useState<RoleInfo | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [selectedPerms, setSelectedPerms] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');

  const rolesQuery = useQuery({
    queryKey: ['roles'],
    queryFn: getRoles,
  });

  const permsQuery = useQuery({
    queryKey: ['all-permissions'],
    queryFn: getAllPermissions,
  });

  const createMutation = useMutation({
    mutationFn: (data: { name: string; description: string; permissions: string[] }) => createRole(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['roles'] });
      closeModal();
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? t('roles.createFailed'));
      } else {
        setError(t('roles.createFailed'));
      }
    },
  });

  const updateMutation = useMutation({
    mutationFn: (data: { id: number; body: { description?: string; permissions?: string[] } }) =>
      updateRole(data.id, data.body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['roles'] });
      closeModal();
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? t('roles.updateFailed'));
      } else {
        setError(t('roles.updateFailed'));
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteRole(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['roles'] });
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        alert(axiosErr.response?.data?.detail ?? t('roles.deleteFailed'));
      }
    },
  });

  const roles = rolesQuery.data ?? [];
  const allPerms = permsQuery.data ?? [];
  const permGroups = groupPermissions(allPerms);

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
      <div className="page-header">
        <div>
          <h1>{t('roles.title')}</h1>
          <p className="page-subtitle">{t('roles.subtitle')}</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>{t('roles.addRole')}</button>
      </div>

      {rolesQuery.isLoading && <div className="loading-message">{t('roles.loadingRoles')}</div>}
      {rolesQuery.isError && <div className="error-banner">{t('roles.loadFailed')}</div>}

      {roles.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('common.name')}</th>
                <th>{t('roles.description')}</th>
                <th>{t('permissions.title')}</th>
                <th>{t('common.type')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((role) => (
                <tr key={role.id}>
                  <td className="cell-alias">{role.name}</td>
                  <td>{role.description || '—'}</td>
                  <td className="perm-count">{t('roles.permissionCount', { count: role.permissions.length })}</td>
                  <td>{role.is_system ? <span className="system-badge">{t('roles.system')}</span> : '—'}</td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-sm btn-secondary" onClick={() => openEdit(role)}>{t('common.edit')}</button>
                      {!role.is_system && (
                        <button className="btn btn-sm btn-danger" onClick={() => handleDelete(role)} disabled={deleteMutation.isPending}>{t('common.delete')}</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showModal && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" style={{ width: 600 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingRole ? t('roles.editTitle', { name: editingRole.name }) : t('roles.addTitle')}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handleSubmit}>
              <div className="form-grid">
                <div className="form-group form-group--full">
                  <label>{t('common.name')}</label>
                  <input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="role-name"
                    required
                    disabled={!!editingRole}
                  />
                </div>
                <div className="form-group form-group--full">
                  <label>{t('roles.description')}</label>
                  <input
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="Role description"
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

              {error && <div className="form-error">{error}</div>}

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
                <button type="submit" className="btn btn-primary" disabled={isSaving}>
                  {isSaving ? t('common.saving') : editingRole ? t('common.update') : t('common.create')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default Roles;
