import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getPermissions,
  getAdminDatabases,
  getDbTables,
  updatePermission,
  deletePermission,
  type Permission,
} from '../api/client';
import { useCanWrite } from '../components/useCanWrite';
import './Permissions.css';

const OPERATIONS = [
  { key: 'allow_select' as const, label: 'SELECT' },
  { key: 'allow_insert' as const, label: 'INSERT' },
  { key: 'allow_update' as const, label: 'UPDATE' },
  { key: 'allow_delete' as const, label: 'DELETE' },
];

function Permissions() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const canWrite = useCanWrite('query.permissions.write');

  const [newRole, setNewRole] = useState('');
  const [newDbAlias, setNewDbAlias] = useState('');
  const [editingTablesFor, setEditingTablesFor] = useState<string | null>(null);
  const [availableTables, setAvailableTables] = useState<string[]>([]);
  const [selectedTables, setSelectedTables] = useState<string[]>([]);
  const [tablesLoading, setTablesLoading] = useState(false);

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
    if (window.confirm(t('permissions.removeConfirm', { role: perm.role, db: perm.db_alias }))) {
      deleteMut.mutate(perm.id);
    }
  }

  async function handleEditTables(perm: Permission) {
    const key = `${perm.role}:${perm.db_alias}`;
    setEditingTablesFor(key);
    setSelectedTables(perm.allowed_tables ?? []);
    setTablesLoading(true);
    try {
      const tables = await getDbTables(perm.db_alias);
      setAvailableTables(tables);
    } catch {
      setAvailableTables([]);
    } finally {
      setTablesLoading(false);
    }
  }

  function handleToggleTable(table: string) {
    setSelectedTables((prev) =>
      prev.includes(table) ? prev.filter((t) => t !== table) : [...prev, table]
    );
  }

  function handleSaveTables(perm: Permission) {
    updateMut.mutate({
      ...perm,
      allowed_tables: selectedTables.length > 0 ? selectedTables : null,
    });
    setEditingTablesFor(null);
  }

  function handleCancelTables() {
    setEditingTablesFor(null);
  }

  return (
    <div className="permissions">
      <div className="page-header">
        <h1>{t('permissions.title')}</h1>
        <p className="page-subtitle">{t('permissions.subtitle')}</p>
      </div>

      {canWrite && (
        <div className="add-perm-row">
          <input
            type="text"
            placeholder={t('permissions.roleName')}
            value={newRole}
            onChange={(e) => setNewRole(e.target.value)}
            className="perm-input"
          />
          <select
            value={newDbAlias}
            onChange={(e) => setNewDbAlias(e.target.value)}
            className="perm-select"
          >
            <option value="">{t('permissions.selectDatabase')}</option>
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
            {t('permissions.addPermission')}
          </button>
        </div>
      )}

      {permsQuery.isLoading && <div className="loading-message">{t('permissions.loadingPermissions')}</div>}

      {permsQuery.isError && (
        <div className="error-banner">{t('permissions.loadFailed')}</div>
      )}

      {permissions.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('permissions.role')}</th>
                <th>{t('connections.database')}</th>
                {OPERATIONS.map((op) => (
                  <th key={op.key} className="th-center">{op.label}</th>
                ))}
                <th>{t('permissions.allowedTables')}</th>
                <th>{t('common.actions')}</th>
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
	                        disabled={!canWrite || updateMut.isPending}
                      />
                    </td>
                  ))}
                  <td>
                    {editingTablesFor === `${perm.role}:${perm.db_alias}` ? (
                      <div className="table-selector">
                        {tablesLoading ? (
                          <span>{t('common.loading')}</span>
                        ) : (
                          <>
                            <div className="table-checkboxes">
                              {availableTables.map((table) => (
                                <label key={table} className="table-checkbox-label">
                                  <input
                                    type="checkbox"
                                    checked={selectedTables.includes(table)}
                                    onChange={() => handleToggleTable(table)}
                                  />
                                  <span>{table}</span>
                                </label>
                              ))}
                              {availableTables.length === 0 && (
                                <span className="hint">{t('permissions.noTablesFound')}</span>
                              )}
                            </div>
                            <div className="table-selector-actions">
                              <button className="btn btn-sm btn-primary" onClick={() => handleSaveTables(perm)}>
                                {t('common.save')}
                              </button>
                              <button className="btn btn-sm" onClick={handleCancelTables}>
                                {t('common.cancel')}
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                    ) : (
                      <div className="table-display">
                        {perm.allowed_tables && perm.allowed_tables.length > 0 ? (
                          <span className="table-tags">
                            {perm.allowed_tables.map((t) => (
                              <span key={t} className="table-tag">{t}</span>
                            ))}
                          </span>
                        ) : (
                          <span className="hint">{t('permissions.allTables')}</span>
                        )}
	                        {canWrite && (
	                          <button
	                            className="btn btn-sm btn-link"
	                            onClick={() => handleEditTables(perm)}
	                          >
	                            {t('common.edit')}
	                          </button>
	                        )}
                      </div>
                    )}
                  </td>
	                  <td>
	                    {canWrite && (
	                      <button
	                        className="btn btn-sm btn-danger"
	                        onClick={() => handleDelete(perm)}
	                        disabled={deleteMut.isPending}
	                      >
	                        {t('common.remove')}
	                      </button>
	                    )}
	                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!permsQuery.isLoading && permissions.length === 0 && !permsQuery.isError && (
        <div className="empty-state">
          <h3>{t('permissions.noPermissions')}</h3>
          <p>{t('permissions.noPermissionsDesc')}</p>
        </div>
      )}
    </div>
  );
}

export default Permissions;
