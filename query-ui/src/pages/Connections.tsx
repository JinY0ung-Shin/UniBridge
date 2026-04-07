import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getAdminDatabases,
  createDatabase,
  updateDatabase,
  deleteDatabase,
  testDatabase,
  type DatabaseConfig,
} from '../api/client';
import './Connections.css';

const emptyForm: DatabaseConfig = {
  alias: '',
  db_type: 'postgres',
  host: '',
  port: 5432,
  database: '',
  username: '',
  password: '',
  pool_size: 5,
  max_overflow: 3,
  query_timeout: 30,
};

function Connections() {
  const queryClient = useQueryClient();

  const [showModal, setShowModal] = useState(false);
  const [editingAlias, setEditingAlias] = useState<string | null>(null);
  const [form, setForm] = useState<DatabaseConfig>({ ...emptyForm });
  const [testResults, setTestResults] = useState<Record<string, { status: string; message: string }>>({});

  const dbsQuery = useQuery({
    queryKey: ['admin-databases'],
    queryFn: getAdminDatabases,
  });

  const createMutation = useMutation({
    mutationFn: (data: DatabaseConfig) => createDatabase(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-databases'] });
      closeModal();
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ alias, data }: { alias: string; data: Partial<DatabaseConfig> }) =>
      updateDatabase(alias, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-databases'] });
      closeModal();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (alias: string) => deleteDatabase(alias),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-databases'] });
    },
  });

  const testMutation = useMutation({
    mutationFn: (alias: string) => testDatabase(alias),
    onSuccess: (data, alias) => {
      setTestResults((prev) => ({ ...prev, [alias]: data }));
    },
    onError: (_err, alias) => {
      setTestResults((prev) => ({
        ...prev,
        [alias]: { status: 'error', message: 'Connection test failed' },
      }));
    },
  });

  const databases = dbsQuery.data ?? [];

  function openCreate() {
    setForm({ ...emptyForm });
    setEditingAlias(null);
    setShowModal(true);
  }

  function openEdit(db: DatabaseConfig) {
    setForm({ ...db, password: '' });
    setEditingAlias(db.alias);
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingAlias(null);
    setForm({ ...emptyForm });
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (editingAlias) {
      const { password, ...rest } = form;
      const data = password ? form : rest;
      updateMutation.mutate({ alias: editingAlias, data });
    } else {
      createMutation.mutate(form);
    }
  }

  function handleDelete(alias: string) {
    if (window.confirm(`Delete database "${alias}"? This action cannot be undone.`)) {
      deleteMutation.mutate(alias);
    }
  }

  function handleTest(alias: string) {
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[alias];
      return next;
    });
    testMutation.mutate(alias);
  }

  function updateField<K extends keyof DatabaseConfig>(key: K, value: DatabaseConfig[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const isSaving = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="connections">
      <div className="page-header">
        <div>
          <h1>Connections</h1>
          <p className="page-subtitle">Manage database connections</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>
          + Add Connection
        </button>
      </div>

      {dbsQuery.isLoading && <div className="loading-message">Loading connections...</div>}

      {dbsQuery.isError && (
        <div className="error-banner">Failed to load database connections.</div>
      )}

      {databases.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>Alias</th>
                <th>Type</th>
                <th>Host:Port</th>
                <th>Database</th>
                <th>Pool Size</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {databases.map((db) => {
                const testResult = testResults[db.alias];
                return (
                  <tr key={db.alias}>
                    <td className="cell-alias">{db.alias}</td>
                    <td>
                      <span className="badge-type">{db.db_type}</span>
                    </td>
                    <td className="mono">{db.host}:{db.port}</td>
                    <td>{db.database}</td>
                    <td>{db.pool_size}</td>
                    <td>
                      {testResult ? (
                        <span className={`status-badge ${testResult.status === 'error' ? 'status-badge--error' : 'status-badge--ok'}`}>
                          {testResult.status === 'error' ? 'Error' : 'OK'}
                        </span>
                      ) : (
                        <span className="status-badge status-badge--unknown">--</span>
                      )}
                    </td>
                    <td>
                      <div className="action-buttons">
                        <button
                          className="btn btn-sm btn-outline"
                          onClick={() => handleTest(db.alias)}
                          disabled={testMutation.isPending}
                        >
                          Test
                        </button>
                        <button
                          className="btn btn-sm btn-outline"
                          onClick={() => openEdit(db)}
                        >
                          Edit
                        </button>
                        <button
                          className="btn btn-sm btn-danger-outline"
                          onClick={() => handleDelete(db.alias)}
                          disabled={deleteMutation.isPending}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {!dbsQuery.isLoading && databases.length === 0 && !dbsQuery.isError && (
        <div className="empty-state">
          <h3>No connections yet</h3>
          <p>Click "Add Connection" to register your first database.</p>
        </div>
      )}

      {/* Modal */}
      {showModal && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingAlias ? `Edit "${editingAlias}"` : 'Add Connection'}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>
            <form onSubmit={handleSubmit}>
              <div className="form-grid">
                <div className="form-group">
                  <label>Alias</label>
                  <input
                    type="text"
                    value={form.alias}
                    onChange={(e) => updateField('alias', e.target.value)}
                    required
                    disabled={!!editingAlias}
                    placeholder="e.g., main-db"
                  />
                </div>
                <div className="form-group">
                  <label>Type</label>
                  <select
                    value={form.db_type}
                    onChange={(e) => updateField('db_type', e.target.value as 'postgres' | 'mssql')}
                  >
                    <option value="postgres">PostgreSQL</option>
                    <option value="mssql">MS SQL</option>
                  </select>
                </div>
                <div className="form-group">
                  <label>Host</label>
                  <input
                    type="text"
                    value={form.host}
                    onChange={(e) => updateField('host', e.target.value)}
                    required
                    placeholder="localhost"
                  />
                </div>
                <div className="form-group">
                  <label>Port</label>
                  <input
                    type="number"
                    value={form.port}
                    onChange={(e) => updateField('port', Number(e.target.value))}
                    required
                  />
                </div>
                <div className="form-group">
                  <label>Database</label>
                  <input
                    type="text"
                    value={form.database}
                    onChange={(e) => updateField('database', e.target.value)}
                    required
                    placeholder="mydb"
                  />
                </div>
                <div className="form-group">
                  <label>Username</label>
                  <input
                    type="text"
                    value={form.username}
                    onChange={(e) => updateField('username', e.target.value)}
                    required
                    placeholder="dbuser"
                  />
                </div>
                <div className="form-group form-group--full">
                  <label>Password {editingAlias && <span className="hint">(leave blank to keep current)</span>}</label>
                  <input
                    type="password"
                    value={form.password ?? ''}
                    onChange={(e) => updateField('password', e.target.value)}
                    placeholder="********"
                  />
                </div>
                <div className="form-group">
                  <label>Pool Size</label>
                  <input
                    type="number"
                    value={form.pool_size}
                    onChange={(e) => updateField('pool_size', Number(e.target.value))}
                    min={1}
                    max={100}
                  />
                </div>
                <div className="form-group">
                  <label>Max Overflow</label>
                  <input
                    type="number"
                    value={form.max_overflow}
                    onChange={(e) => updateField('max_overflow', Number(e.target.value))}
                    min={0}
                    max={100}
                  />
                </div>
              </div>

              {(createMutation.isError || updateMutation.isError) && (
                <div className="form-error">
                  {(createMutation.error as Error)?.message ||
                    (updateMutation.error as Error)?.message ||
                    'An error occurred'}
                </div>
              )}

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary" disabled={isSaving}>
                  {isSaving ? 'Saving...' : editingAlias ? 'Update' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default Connections;
