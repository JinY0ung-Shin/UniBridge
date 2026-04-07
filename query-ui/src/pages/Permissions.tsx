import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getPermissions,
  getAdminDatabases,
  updatePermission,
  deletePermission,
  type Permission,
} from '../api/client';
import './Permissions.css';

const OPERATIONS = [
  { key: 'allow_select' as const, label: 'SELECT' },
  { key: 'allow_insert' as const, label: 'INSERT' },
  { key: 'allow_update' as const, label: 'UPDATE' },
  { key: 'allow_delete' as const, label: 'DELETE' },
];

function Permissions() {
  const queryClient = useQueryClient();

  const [newRole, setNewRole] = useState('');
  const [newDbAlias, setNewDbAlias] = useState('');

  const permsQuery = useQuery({
    queryKey: ['permissions'],
    queryFn: getPermissions,
  });

  const dbsQuery = useQuery({
    queryKey: ['admin-databases'],
    queryFn: getAdminDatabases,
  });

  const updateMut = useMutation({
    mutationFn: (perm: Permission) => updatePermission(perm),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['permissions'] });
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deletePermission(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['permissions'] });
    },
  });

  const permissions = permsQuery.data ?? [];
  const databases = dbsQuery.data ?? [];
  const dbAliases = databases.map((d) => d.alias);

  function toggleOperation(perm: Permission, key: 'allow_select' | 'allow_insert' | 'allow_update' | 'allow_delete') {
    updateMut.mutate({ ...perm, [key]: !perm[key] });
  }

  function handleAdd() {
    if (!newRole.trim() || !newDbAlias.trim()) return;
    updateMut.mutate({
      role: newRole.trim(),
      db_alias: newDbAlias.trim(),
      allow_select: true,
      allow_insert: false,
      allow_update: false,
      allow_delete: false,
    });
    setNewRole('');
    setNewDbAlias('');
  }

  function handleDelete(perm: Permission) {
    if (!perm.id) return;
    if (window.confirm(`Remove permissions for role "${perm.role}" on "${perm.db_alias}"?`)) {
      deleteMut.mutate(perm.id);
    }
  }

  return (
    <div className="permissions">
      <div className="page-header">
        <h1>Permissions</h1>
        <p className="page-subtitle">Configure role-based database access</p>
      </div>

      {/* Add new permission row */}
      <div className="add-perm-row">
        <input
          type="text"
          placeholder="Role name"
          value={newRole}
          onChange={(e) => setNewRole(e.target.value)}
          className="perm-input"
        />
        <select
          value={newDbAlias}
          onChange={(e) => setNewDbAlias(e.target.value)}
          className="perm-select"
        >
          <option value="">Select database...</option>
          {dbAliases.map((alias) => (
            <option key={alias} value={alias}>
              {alias}
            </option>
          ))}
        </select>
        <button
          className="btn btn-primary"
          onClick={handleAdd}
          disabled={!newRole.trim() || !newDbAlias || updateMut.isPending}
        >
          Add Permission
        </button>
      </div>

      {permsQuery.isLoading && <div className="loading-message">Loading permissions...</div>}

      {permsQuery.isError && (
        <div className="error-banner">Failed to load permissions.</div>
      )}

      {permissions.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Role</th>
                <th>Database</th>
                {OPERATIONS.map((op) => (
                  <th key={op.key} className="th-center">{op.label}</th>
                ))}
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {permissions.map((perm) => (
                <tr key={perm.id ?? `${perm.role}-${perm.db_alias}`}>
                  <td className="cell-alias">{perm.role}</td>
                  <td>{perm.db_alias}</td>
                  {OPERATIONS.map((op) => (
                    <td key={op.key} className="td-center">
                      <input
                        type="checkbox"
                        className="perm-checkbox"
                        checked={perm[op.key]}
                        onChange={() => toggleOperation(perm, op.key)}
                        disabled={updateMut.isPending}
                      />
                    </td>
                  ))}
                  <td>
                    <button
                      className="btn btn-sm btn-danger-outline"
                      onClick={() => handleDelete(perm)}
                      disabled={deleteMut.isPending}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!permsQuery.isLoading && permissions.length === 0 && !permsQuery.isError && (
        <div className="empty-state">
          <h3>No permissions configured</h3>
          <p>Add a role-database permission using the form above.</p>
        </div>
      )}
    </div>
  );
}

export default Permissions;
