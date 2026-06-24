import { useEffect, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getAdminDatabases,
  createDatabase,
  updateDatabase,
  deleteDatabase,
  testDatabase,
  getDbTables,
  getAlertResourceOwners,
  setAlertResourceOwner,
  type DatabaseConfig,
} from '../api/client';
import ResourceModal from '../components/ResourceModal';
import { useToast } from '../components/useToast';
import { useCanWrite } from '../components/useCanWrite';
import './Connections.css';

function parseEmails(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((email) => email.trim())
    .filter(Boolean);
}

const DEFAULT_PORTS: Record<string, number> = {
  postgres: 5432,
  mssql: 1433,
  clickhouse: 8123,
  neo4j: 7687,
  graphdb: 7200,
};

const NEO4J_PROTOCOLS = ['bolt', 'bolt+s', 'bolt+ssc', 'neo4j', 'neo4j+s', 'neo4j+ssc'] as const;

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
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const canWrite = useCanWrite('query.databases.write');
  // Assignee editing is an alert-config concern, gated on the alert permissions
  // (separate from query.databases.write), so a DB-only writer never hits a 403.
  const canReadAlerts = useCanWrite('alerts.read');
  const canManageAlerts = useCanWrite('alerts.write');

  const [showModal, setShowModal] = useState(false);
  const [editingAlias, setEditingAlias] = useState<string | null>(null);
  const [form, setForm] = useState<DatabaseConfig>({ ...emptyForm });
  // null = field untouched (follow the loaded value); string = user-edited draft.
  const [assigneesDraft, setAssigneesDraft] = useState<string | null>(null);
  const { addToast } = useToast();
  const [testResults, setTestResults] = useState<Record<string, { status: string }>>({});
  const [connectionSearch, setConnectionSearch] = useState('');

  const dbsQuery = useQuery({
    queryKey: ['admin-databases'],
    queryFn: getAdminDatabases,
  });

  const ownersQuery = useQuery({
    queryKey: ['alert-resource-owners'],
    queryFn: getAlertResourceOwners,
    enabled: canReadAlerts,
  });

  function ownerEmailsFor(alias: string): string[] {
    return (ownersQuery.data ?? []).find(
      (o) => o.resource_type === 'db' && o.resource_id === alias,
    )?.emails ?? [];
  }

  // Owners must be loaded before we can safely edit/save assignees for an
  // existing DB; a fresh DB (create) has no existing owner, so '' is correct.
  const assigneesReady = editingAlias === null ? true : ownersQuery.isSuccess;
  // Loaded assignee text for the DB currently in the modal; the field follows
  // this until the user edits (derive-during-render — no setState-in-effect).
  const loadedAssigneeText = editingAlias === null ? '' : ownerEmailsFor(editingAlias).join(', ');
  const assigneesValue = assigneesDraft ?? loadedAssigneeText;
  const databaseLabel = form.db_type === 'graphdb'
    ? t('connections.repositoryId')
    : t('connections.database');

  async function saveAssignees(alias: string, isCreate: boolean) {
    if (!canManageAlerts || !(isCreate || ownersQuery.isSuccess)) return;
    if (assigneesDraft === null) return; // untouched → nothing to persist
    const next = parseEmails(assigneesDraft);
    const baseline = isCreate ? [] : ownerEmailsFor(alias);
    // Only write when the value actually changed — never clobber assignees we
    // failed to load (would otherwise send [] and delete them server-side).
    if (JSON.stringify(next) === JSON.stringify(baseline)) return;
    try {
      await setAlertResourceOwner('db', alias, { emails: next });
      queryClient.invalidateQueries({ queryKey: ['alert-resource-owners'] });
    } catch {
      addToast({ type: 'error', title: `${alias} — ${t('connections.assignees')}`, message: t('common.errorOccurred') });
    }
  }

  const createMutation = useMutation({
    mutationFn: (data: DatabaseConfig) => createDatabase(data),
    onSuccess: async (_data, variables) => {
      await saveAssignees(variables.alias, true);
      queryClient.invalidateQueries({ queryKey: ['admin-databases'] });
      closeModal();
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ alias, data }: { alias: string; data: Partial<DatabaseConfig> }) =>
      updateDatabase(alias, data),
    onSuccess: async (_data, variables) => {
      await saveAssignees(variables.alias, false);
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
      setTestResults((prev) => ({ ...prev, [alias]: { status: data.status } }));
      addToast({
        type: data.status === 'ok' ? 'success' : 'error',
        title: `${alias} — ${data.status === 'ok' ? t('common.ok') : t('common.error')}`,
        message: data.message,
      });
    },
    onError: (_err, alias) => {
      setTestResults((prev) => ({ ...prev, [alias]: { status: 'error' } }));
      addToast({ type: 'error', title: `${alias} — ${t('connections.testFailed')}` });
    },
  });

  const databases = dbsQuery.data ?? [];
  const normalizedConnectionSearch = connectionSearch.trim().toLowerCase();
  const filteredDatabases = normalizedConnectionSearch
    ? databases.filter((db) => [
        db.alias,
        db.db_type,
        db.host,
        String(db.port),
        db.database,
        testResults[db.alias]?.status === 'error' ? t('common.error') : '',
        testResults[db.alias]?.status && testResults[db.alias]?.status !== 'error' ? t('common.ok') : '',
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(normalizedConnectionSearch))
    : databases;
  const curlCopyTimeoutRef = useRef<number | null>(null);

  function clearCurlCopyTimer() {
    if (curlCopyTimeoutRef.current !== null) {
      window.clearTimeout(curlCopyTimeoutRef.current);
      curlCopyTimeoutRef.current = null;
    }
  }

  useEffect(() => {
    return () => {
      clearCurlCopyTimer();
    };
  }, []);

  function openCreate() {
    setForm({ ...emptyForm });
    setAssigneesDraft(null);
    setEditingAlias(null);
    setShowModal(true);
  }

  function openEdit(db: DatabaseConfig) {
    setForm({ ...db, password: '' });
    setAssigneesDraft(null); // field follows the loaded value via assigneesValue
    setEditingAlias(db.alias);
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingAlias(null);
    setForm({ ...emptyForm });
    setAssigneesDraft(null);
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
    if (window.confirm(t('connections.deleteConfirm', { alias }))) {
      deleteMutation.mutate(alias);
    }
  }

  const [curlModal, setCurlModal] = useState<{ alias: string; curl: string } | null>(null);
  const [curlCopied, setCurlCopied] = useState(false);

  async function handleCurl(db: DatabaseConfig) {
    clearCurlCopyTimer();
    const alias = db.alias;
    let sampleQuery = 'MATCH (n) RETURN n LIMIT 10';
    let tableName = '<TABLE>';
    if (db.db_type === 'graphdb') {
      sampleQuery = 'SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 10';
    } else if (db.db_type !== 'neo4j') {
      try {
        const tables = await getDbTables(alias);
        if (tables.length > 0) tableName = tables[0];
      } catch { /* use placeholder */ }
      sampleQuery = `SELECT * FROM ${tableName} LIMIT 10`;
    }
    const base = `${window.location.origin}/api/query/execute`;
    const body = JSON.stringify({ database: alias, sql: sampleQuery }, null, 2);
    const curl = `curl -k -X POST \\\n  -H 'Content-Type: application/json' \\\n  -H 'apikey: <YOUR_API_KEY>' \\\n  '${base}' \\\n  -d '${body}'`;
    setCurlModal({ alias, curl });
    setCurlCopied(false);
  }

  async function handleCurlCopy() {
    if (!curlModal) return;
    clearCurlCopyTimer();
    try {
      await navigator.clipboard.writeText(curlModal.curl);
      setCurlCopied(true);
      curlCopyTimeoutRef.current = window.setTimeout(() => {
        setCurlCopied(false);
        curlCopyTimeoutRef.current = null;
      }, 2000);
    } catch {
      setCurlCopied(false);
      addToast({ type: 'error', title: t('connections.copyFailed') });
    }
  }

  function closeCurlModal() {
    clearCurlCopyTimer();
    setCurlCopied(false);
    setCurlModal(null);
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
          <h1>{t('connections.title')}</h1>
          <p className="page-subtitle">{t('connections.subtitle')}</p>
        </div>
        {(databases.length > 0 || canWrite) && (
          <div className="page-header__actions connections-header-actions">
            {databases.length > 0 && (
              <input
                className="connection-search-input"
                type="search"
                value={connectionSearch}
                onChange={(event) => setConnectionSearch(event.target.value)}
                placeholder={t('connections.searchPlaceholder')}
                aria-label={t('connections.searchPlaceholder')}
              />
            )}
            {canWrite && (
              <button type="button" className="btn btn-primary" onClick={openCreate}>
                {t('connections.addConnection')}
              </button>
            )}
          </div>
        )}
      </div>

      {dbsQuery.isLoading && <div className="loading-message" role="status">{t('connections.loadingConnections')}</div>}

      {dbsQuery.isError && (
        <div className="error-banner" role="alert">{t('connections.loadFailed')}</div>
      )}

      {databases.length > 0 && filteredDatabases.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('connections.alias')}</th>
                <th scope="col">{t('common.type')}</th>
                <th scope="col">{t('connections.hostPort')}</th>
                <th scope="col">{t('connections.database')}</th>
                <th scope="col">{t('connections.poolSize')}</th>
                <th scope="col">{t('common.status')}</th>
                <th scope="col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {filteredDatabases.map((db) => {
                const testResult = testResults[db.alias];
                const isTesting = testMutation.isPending && testMutation.variables === db.alias;
                const isDeleting = deleteMutation.isPending && deleteMutation.variables === db.alias;
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
                        <span className={`badge ${testResult.status === 'error' ? 'badge-error' : 'badge-ok'}`}>
                          {testResult.status === 'error' ? t('common.error') : t('common.ok')}
                        </span>
                      ) : (
                        <span className="badge badge-unknown">--</span>
                      )}
                    </td>
                    <td>
                      <div className="action-buttons">
                        <button
                          type="button"
                          className="btn btn-sm btn-secondary"
                          aria-label={t('connections.testConnection', { alias: db.alias })}
                          onClick={() => handleTest(db.alias)}
                          disabled={testMutation.isPending}
                          aria-busy={isTesting}
                        >
                          {isTesting ? t('common.testing') : t('common.test')}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-outline"
                          aria-label={t('connections.showCurl', { alias: db.alias })}
                          onClick={() => handleCurl(db)}
                        >
                          cURL
                        </button>
                        {canWrite && (
                          <>
                            <button
                              type="button"
                              className="btn btn-sm btn-secondary"
                              aria-label={t('connections.editConnection', { alias: db.alias })}
                              onClick={() => openEdit(db)}
                            >
                              {t('common.edit')}
                            </button>
                            <button
                              type="button"
                              className="btn btn-sm btn-danger"
                              aria-label={t('connections.deleteConnection', { alias: db.alias })}
                              onClick={() => handleDelete(db.alias)}
                              disabled={deleteMutation.isPending}
                              aria-busy={isDeleting}
                            >
                              {isDeleting ? t('common.deleting') : t('common.delete')}
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {!dbsQuery.isLoading && databases.length > 0 && filteredDatabases.length === 0 && !dbsQuery.isError && (
        <div className="empty-state">
          <h3>{t('connections.noSearchResults')}</h3>
          <p>{t('connections.noSearchResultsDesc')}</p>
          <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setConnectionSearch('')}>
            {t('common.clearSearch')}
          </button>
        </div>
      )}

      {!dbsQuery.isLoading && databases.length === 0 && !dbsQuery.isError && (
        <div className="empty-state">
          <h3>{t('connections.noConnections')}</h3>
          <p>{t('connections.noConnectionsDesc')}</p>
        </div>
      )}

      {/* Modal */}
      {canWrite && showModal && (
        <ResourceModal
          title={editingAlias ? t('connections.editAlias', { alias: editingAlias }) : t('connections.addTitle')}
          onClose={closeModal}
          closeLabel={t('common.close')}
        >
          <form onSubmit={handleSubmit}>
            <div className="form-grid">
              <div className="form-group">
                <label htmlFor="connection-alias">{t('connections.alias')}</label>
                <input
                  id="connection-alias"
                  type="text"
                  value={form.alias}
                  onChange={(e) => updateField('alias', e.target.value)}
                  required
                  disabled={!!editingAlias}
                  placeholder="e.g., main-db"
                  aria-label={t('connections.alias')}
                />
              </div>
              <div className="form-group">
                <label htmlFor="connection-db-type">{t('common.type')}</label>
                <select
                  id="connection-db-type"
                  value={form.db_type}
                  onChange={(e) => {
                    const newType = e.target.value as DatabaseConfig['db_type'];
                    let nextProtocol: DatabaseConfig['protocol'] = null;
                    let nextSecure: DatabaseConfig['secure'] = null;
                    if (newType === 'clickhouse') {
                      nextProtocol = 'http';
                      nextSecure = false;
                    } else if (newType === 'neo4j') {
                      nextProtocol = 'bolt';
                    } else if (newType === 'graphdb') {
                      nextProtocol = 'http';
                      nextSecure = null;
                    }
                    setForm((prev) => ({
                      ...prev,
                      db_type: newType,
                      port: DEFAULT_PORTS[newType] ?? prev.port,
                      protocol: nextProtocol,
                      secure: nextSecure,
                    }));
                  }}
                  aria-label={t('common.type')}
                >
                  <option value="postgres">PostgreSQL</option>
                  <option value="mssql">MS SQL</option>
                  <option value="clickhouse">ClickHouse</option>
                  <option value="neo4j">Neo4j</option>
                  <option value="graphdb">Ontotext GraphDB</option>
                </select>
              </div>
              <div className="form-group">
                <label htmlFor="connection-host">{t('connections.host')}</label>
                <input
                  id="connection-host"
                  type="text"
                  value={form.host}
                  onChange={(e) => updateField('host', e.target.value)}
                  required
                  placeholder="localhost"
                  aria-label={t('connections.host')}
                />
              </div>
              <div className="form-group">
                <label htmlFor="connection-port">{t('connections.port')}</label>
                <input
                  id="connection-port"
                  type="number"
                  value={form.port}
                  onChange={(e) => updateField('port', Number(e.target.value))}
                  required
                  aria-label={t('connections.port')}
                />
              </div>
              <div className="form-group">
                <label htmlFor="connection-database">{databaseLabel}</label>
                <input
                  id="connection-database"
                  type="text"
                  value={form.database}
                  onChange={(e) => updateField('database', e.target.value)}
                  required
                  placeholder={form.db_type === 'graphdb' ? 'my-repo' : 'mydb'}
                  aria-label={databaseLabel}
                />
              </div>
              <div className="form-group">
                <label htmlFor="connection-username">{t('connections.username')}</label>
                <input
                  id="connection-username"
                  type="text"
                  value={form.username}
                  onChange={(e) => updateField('username', e.target.value)}
                  required
                  placeholder="dbuser"
                  aria-label={t('connections.username')}
                />
              </div>
              <div className="form-group form-group--full">
                <label htmlFor="connection-password">
                  {t('connections.password')}{' '}
                  {editingAlias && (
                    <span id="connection-password-hint" className="hint">
                      {t('connections.passwordHint')}
                    </span>
                  )}
                </label>
                <input
                  id="connection-password"
                  type="password"
                  value={form.password ?? ''}
                  onChange={(e) => updateField('password', e.target.value)}
                  placeholder="********"
                  aria-label={t('connections.password')}
                  aria-describedby={editingAlias ? 'connection-password-hint' : undefined}
                />
              </div>
              {form.db_type === 'clickhouse' && (
                <div className="form-group">
                  <label htmlFor="connection-protocol">{t('connections.protocol')}</label>
                  <select
                    id="connection-protocol"
                    value={form.protocol ?? 'http'}
                    onChange={(e) => {
                      const proto = e.target.value as 'http' | 'https';
                      const isSecure = proto === 'https';
                      setForm((prev) => ({
                        ...prev,
                        protocol: proto,
                        secure: isSecure,
                        port: isSecure ? 8443 : 8123,
                      }));
                    }}
                    aria-label={t('connections.protocol')}
                  >
                    <option value="http">HTTP</option>
                    <option value="https">HTTPS</option>
                  </select>
                </div>
              )}
              {form.db_type === 'neo4j' && (
                <div className="form-group">
                  <label htmlFor="connection-protocol">{t('connections.protocol')}</label>
                  <select
                    id="connection-protocol"
                    value={form.protocol ?? 'bolt'}
                    onChange={(e) => {
                      const proto = e.target.value as (typeof NEO4J_PROTOCOLS)[number];
                      setForm((prev) => ({
                        ...prev,
                        protocol: proto,
                        secure: null,
                      }));
                    }}
                    aria-label={t('connections.protocol')}
                  >
                    {NEO4J_PROTOCOLS.map((p) => (
                      <option key={p} value={p}>{p}</option>
                    ))}
                  </select>
                </div>
              )}
              {form.db_type === 'graphdb' && (
                <div className="form-group">
                  <label htmlFor="connection-protocol">{t('connections.protocol')}</label>
                  <select
                    id="connection-protocol"
                    value={form.protocol ?? 'http'}
                    onChange={(e) =>
                      updateField('protocol', e.target.value as DatabaseConfig['protocol'])
                    }
                    aria-label={t('connections.protocol')}
                  >
                    <option value="http">http</option>
                    <option value="https">https</option>
                  </select>
                </div>
              )}
              {form.db_type !== 'clickhouse' && form.db_type !== 'neo4j' && form.db_type !== 'graphdb' && (
                <>
                  <div className="form-group">
                    <label htmlFor="connection-pool-size">{t('connections.poolSize')}</label>
                    <input
                      id="connection-pool-size"
                      type="number"
                      value={form.pool_size}
                      onChange={(e) => updateField('pool_size', Number(e.target.value))}
                      min={1}
                      max={100}
                      aria-label={t('connections.poolSize')}
                    />
                  </div>
                  <div className="form-group">
                    <label htmlFor="connection-max-overflow">{t('connections.maxOverflow')}</label>
                    <input
                      id="connection-max-overflow"
                      type="number"
                      value={form.max_overflow}
                      onChange={(e) => updateField('max_overflow', Number(e.target.value))}
                      min={0}
                      max={100}
                      aria-label={t('connections.maxOverflow')}
                    />
                  </div>
                </>
              )}
              {canReadAlerts && (
                <div className="form-group form-group--full">
                  <label htmlFor="connection-assignees">{t('connections.assignees')}</label>
                  <textarea
                    id="connection-assignees"
                    value={assigneesValue}
                    onChange={(e) => setAssigneesDraft(e.target.value)}
                    rows={2}
                    disabled={!canManageAlerts || !assigneesReady}
                    aria-label={t('connections.assignees')}
                    aria-describedby="connection-assignees-hint"
                    placeholder={
                      editingAlias !== null && !assigneesReady
                        ? t('common.loading')
                        : 'alice@example.com, bob@example.com'
                    }
                  />
                  <span id="connection-assignees-hint" className="hint">{t('connections.assigneesHint')}</span>
                </div>
              )}
            </div>

            {(createMutation.isError || updateMutation.isError) && (
              <div className="form-error" role="alert">
                {(createMutation.error as Error)?.message ||
                  (updateMutation.error as Error)?.message ||
                  t('common.errorOccurred')}
              </div>
            )}

            <div className="modal-actions">
              <button type="button" className="btn btn-secondary" onClick={closeModal}>
                {t('common.cancel')}
              </button>
              <button type="submit" className="btn btn-primary" disabled={isSaving} aria-busy={isSaving}>
                {isSaving ? t('common.saving') : editingAlias ? t('common.update') : t('common.create')}
              </button>
            </div>
          </form>
        </ResourceModal>
      )}

      {curlModal && (
        <ResourceModal
          title={`cURL — ${curlModal.alias}`}
          onClose={closeCurlModal}
          closeLabel={t('common.close')}
          className="modal--sm"
        >
          <div className="curl-block">
            <pre className="curl-code">{curlModal.curl}</pre>
            <button
              type="button"
              className="btn btn-sm btn-secondary curl-copy-btn"
              onClick={handleCurlCopy}
              aria-label={curlCopied ? t('gatewayRoutes.curlCopiedLabel') : t('gatewayRoutes.curlCopyLabel')}
            >
              {curlCopied ? t('gatewayRoutes.curlCopied') : t('gatewayRoutes.curlCopy')}
            </button>
            <span className="visually-hidden" role="status" aria-live="polite">
              {curlCopied ? t('gatewayRoutes.curlCopied') : ''}
            </span>
          </div>
        </ResourceModal>
      )}
    </div>
  );
}

export default Connections;
