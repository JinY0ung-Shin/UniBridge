import { useEffect, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getApiKeys,
  createApiKey,
  updateApiKey,
  deleteApiKey,
  getAdminDatabases,
  getGatewayRoutes,
  getS3Connections,
  getNasConnections,
  type ApiKey,
} from '../api/client';
import { useToast } from '../components/useToast';
import { useCanWrite } from '../components/useCanWrite';
import { usePermissions } from '../components/usePermissions';
import ResourceModal from '../components/ResourceModal';
import { formatKST } from '../utils/time';
import './ApiKeys.css';

function generateKey(): string {
  return 'key-' + crypto.randomUUID().replace(/-/g, '');
}

interface FormState {
  name: string;
  description: string;
  apiKey: string;
  isMaster: boolean;
  allowedDatabases: string[];
  allowedRoutes: string[];
  rateLimit: string;
  allowInsert: boolean;
  allowUpdate: boolean;
  allowDelete: boolean;
  allowedTables: string;
}

const emptyForm: FormState = {
  name: '',
  description: '',
  apiKey: '',
  isMaster: false,
  allowedDatabases: [],
  allowedRoutes: [],
  rateLimit: '',
  allowInsert: false,
  allowUpdate: false,
  allowDelete: false,
  allowedTables: '',
};

function isMasterAccess(key: ApiKey): boolean {
  return Boolean(
    key.is_master
      || (key.allowed_databases.includes('*') && key.allowed_routes.includes('*')),
  );
}

function ApiKeys() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const canWrite = useCanWrite('apikeys.write');
  const { permissions } = usePermissions();
  const canReadNasConnections = permissions.includes('nas.connections.read');

  const [showModal, setShowModal] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>({ ...emptyForm });
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [keySearch, setKeySearch] = useState('');
  const copyTimeoutRef = useRef<number | null>(null);

  const keysQuery = useQuery({ queryKey: ['api-keys'], queryFn: getApiKeys });
  const dbsQuery = useQuery({ queryKey: ['admin-databases'], queryFn: getAdminDatabases });
  const routesQuery = useQuery({ queryKey: ['gateway-routes'], queryFn: getGatewayRoutes });
  const s3ConnectionsQuery = useQuery({ queryKey: ['s3-connections'], queryFn: getS3Connections });
  const nasConnectionsQuery = useQuery({
    queryKey: ['nas-connections'],
    queryFn: getNasConnections,
    enabled: canReadNasConnections,
  });

  const createMut = useMutation({
    mutationFn: createApiKey,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['api-keys'] });
      if (result.key_created && result.api_key) {
        setCreatedKey(result.api_key);
      } else {
        closeModal();
      }
    },
    onError: () => addToast({ type: 'error', title: t('apiKeys.saveFailed') }),
  });

  const updateMut = useMutation({
    mutationFn: ({ name, body }: { name: string; body: Record<string, unknown> }) => updateApiKey(name, body),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['api-keys'] });
      if (result.key_created && result.api_key) {
        setCreatedKey(result.api_key);
      } else {
        closeModal();
      }
    },
    onError: () => addToast({ type: 'error', title: t('apiKeys.saveFailed') }),
  });

  const deleteMut = useMutation({
    mutationFn: deleteApiKey,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['api-keys'] }),
    onError: () => addToast({ type: 'error', title: t('apiKeys.deleteFailed') }),
  });

  const keys = keysQuery.data ?? [];
  const databases = dbsQuery.data ?? [];
  const routes = routesQuery.data?.items ?? [];
  const s3Connections = s3ConnectionsQuery.data ?? [];
  const nasConnections = canReadNasConnections ? nasConnectionsQuery.data ?? [] : [];
  const normalizedKeySearch = keySearch.trim().toLowerCase();
  const filteredKeys = normalizedKeySearch
    ? keys.filter((key) => [
        key.name,
        key.description,
        key.api_key,
        key.owner,
        key.is_master ? t('apiKeys.allAccess') : '',
        ...key.allowed_databases,
        ...key.allowed_routes,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(normalizedKeySearch))
    : keys;

  function clearCopyTimer() {
    if (copyTimeoutRef.current !== null) {
      window.clearTimeout(copyTimeoutRef.current);
      copyTimeoutRef.current = null;
    }
  }

  useEffect(() => {
    return () => {
      clearCopyTimer();
    };
  }, []);

  function openCreate() {
    clearCopyTimer();
    setForm({ ...emptyForm, apiKey: generateKey() });
    setEditingName(null);
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function openEdit(k: ApiKey) {
    clearCopyTimer();
    const isMaster = isMasterAccess(k);
    setForm({
      name: k.name,
      description: k.description,
      apiKey: '',
      isMaster,
      allowedDatabases: isMaster ? [] : k.allowed_databases,
      allowedRoutes: isMaster ? [] : k.allowed_routes,
      rateLimit: k.rate_limit_per_minute == null ? '' : String(k.rate_limit_per_minute),
      allowInsert: Boolean(k.allow_insert),
      allowUpdate: Boolean(k.allow_update),
      allowDelete: Boolean(k.allow_delete),
      allowedTables: (k.allowed_tables ?? []).join(', '),
    });
    setEditingName(k.name);
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function closeModal() {
    clearCopyTimer();
    setShowModal(false);
    setEditingName(null);
    setCreatedKey(null);
    setCopied(false);
  }

  function parseRateLimit(value: string): number | null {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function parseAllowedTables(value: string): string[] | null {
    const tables = value
      .split(',')
      .map((tbl) => tbl.trim())
      .filter(Boolean);
    return tables.length > 0 ? tables : null;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const rateLimit = parseRateLimit(form.rateLimit);
    const allowedDatabases = form.isMaster ? ['*'] : form.allowedDatabases;
    const allowedRoutes = form.isMaster ? ['*'] : form.allowedRoutes;
    const allowedTables = parseAllowedTables(form.allowedTables);
    if (editingName) {
      const body: Record<string, unknown> = {
        description: form.description,
        is_master: form.isMaster,
        allowed_databases: allowedDatabases,
        allowed_routes: allowedRoutes,
        rate_limit_per_minute: rateLimit,
        allow_insert: form.allowInsert,
        allow_update: form.allowUpdate,
        allow_delete: form.allowDelete,
        allowed_tables: allowedTables,
      };
      if (form.apiKey.trim()) body.api_key = form.apiKey.trim();
      updateMut.mutate({ name: editingName, body });
    } else {
      createMut.mutate({
        name: form.name.trim(),
        description: form.description,
        api_key: form.apiKey.trim() || undefined,
        is_master: form.isMaster,
        allowed_databases: allowedDatabases,
        allowed_routes: allowedRoutes,
        rate_limit_per_minute: rateLimit,
        allow_insert: form.allowInsert,
        allow_update: form.allowUpdate,
        allow_delete: form.allowDelete,
        allowed_tables: allowedTables,
      });
    }
  }

  function handleDelete(k: ApiKey) {
    if (window.confirm(t('apiKeys.deleteConfirm', { name: k.name }))) {
      deleteMut.mutate(k.name);
    }
  }

  async function handleCopy() {
    if (createdKey) {
      try {
        clearCopyTimer();
        await navigator.clipboard.writeText(createdKey);
        setCopied(true);
        copyTimeoutRef.current = window.setTimeout(() => {
          setCopied(false);
          copyTimeoutRef.current = null;
        }, 2000);
      } catch {
        setCopied(false);
        addToast({ type: 'error', title: t('apiKeys.copyFailed') });
      }
    }
  }

  function toggleDb(alias: string) {
    setForm((prev) => ({
      ...prev,
      allowedDatabases: prev.allowedDatabases.includes(alias)
        ? prev.allowedDatabases.filter((d) => d !== alias)
        : [...prev.allowedDatabases, alias],
    }));
  }

  function toggleRoute(id: string) {
    setForm((prev) => ({
      ...prev,
      allowedRoutes: prev.allowedRoutes.includes(id)
        ? prev.allowedRoutes.filter((r) => r !== id)
        : [...prev.allowedRoutes, id],
    }));
  }

  function renderTags(items: string[], max = 3) {
    if (items.includes('*')) return <span className="tag tag-master">{t('apiKeys.allAccess')}</span>;
    if (items.length === 0) return <span className="tag tag-more">{t('apiKeys.noneSelected')}</span>;
    const visible = items.slice(0, max);
    const rest = items.length - max;
    return (
      <>
        {visible.map((item) => <span key={item} className="tag">{item}</span>)}
        {rest > 0 && <span className="tag tag-more">+{rest}</span>}
      </>
    );
  }

  const isSaving = createMut.isPending || updateMut.isPending;
  const accessItemClass = form.isMaster
    ? 'checkbox-list-item is-disabled'
    : 'checkbox-list-item';

  return (
    <div className="api-keys">
      <div className="page-header">
        <div>
          <h1>{t('apiKeys.title')}</h1>
          <p className="page-subtitle">{t('apiKeys.subtitle')}</p>
        </div>
        {(keys.length > 0 || canWrite) && (
          <div className="page-header__actions api-keys-header-actions">
            {keys.length > 0 && (
              <input
                className="api-key-search-input"
                type="search"
                value={keySearch}
                onChange={(event) => setKeySearch(event.target.value)}
                placeholder={t('apiKeys.searchPlaceholder')}
                aria-label={t('apiKeys.searchPlaceholder')}
              />
            )}
            {canWrite && (
              <button type="button" className="btn btn-primary" onClick={openCreate}>{t('apiKeys.addKey')}</button>
            )}
          </div>
        )}
      </div>

      {keysQuery.isLoading && <div className="loading-message" role="status">{t('apiKeys.loadingKeys')}</div>}
      {keysQuery.isError && <div className="error-banner" role="alert">{t('apiKeys.loadFailed')}</div>}

      {keys.length > 0 && filteredKeys.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">{t('apiKeys.keyName')}</th>
                <th scope="col">{t('apiKeys.description')}</th>
                <th scope="col">{t('apiKeys.apiKey')}</th>
                <th scope="col">{t('apiKeys.allowedDatabases')}</th>
                <th scope="col">{t('apiKeys.allowedRoutes')}</th>
                <th scope="col">{t('apiKeys.expiresAt')}</th>
                <th scope="col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {filteredKeys.map((k) => {
                const isDeleting = deleteMut.isPending && deleteMut.variables === k.name;
                return (
                  <tr key={k.name}>
                    <td className="cell-alias">{k.name}</td>
                    <td>{k.description || '\u2014'}</td>
                    <td className="cell-key">{k.api_key || '\u2014'}</td>
                    <td><div className="cell-tags">{renderTags(k.allowed_databases)}</div></td>
                    <td><div className="cell-tags">{renderTags(k.allowed_routes)}</div></td>
                    <td>{k.expires_at ? formatKST(k.expires_at) : '—'}</td>
                    <td>
                      {canWrite && (
                        <div className="action-buttons">
                          <button
                            type="button"
                            className="btn btn-sm btn-secondary"
                            aria-label={t('apiKeys.editKey', { name: k.name })}
                            onClick={() => openEdit(k)}
                          >
                            {t('common.edit')}
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm btn-danger"
                            aria-label={t('apiKeys.deleteKey', { name: k.name })}
                            onClick={() => handleDelete(k)}
                            disabled={deleteMut.isPending}
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

      {!keysQuery.isLoading && keys.length > 0 && filteredKeys.length === 0 && !keysQuery.isError && (
        <div className="empty-state">
          <h3>{t('apiKeys.noSearchResults')}</h3>
          <p>{t('apiKeys.noSearchResultsDesc')}</p>
          <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setKeySearch('')}>
            {t('common.clearSearch')}
          </button>
        </div>
      )}

      {!keysQuery.isLoading && keys.length === 0 && !keysQuery.isError && (
        <div className="empty-state">
          <h3>{t('apiKeys.noKeys')}</h3>
          <p>{t('apiKeys.noKeysDesc')}</p>
        </div>
      )}

      {canWrite && showModal && (
        <ResourceModal
          title={editingName ? t('apiKeys.editTitle') : t('apiKeys.addTitle')}
          onClose={closeModal}
          closeLabel={t('common.close')}
          closeOnOverlayClick={!createdKey}
          closeOnEscape={!createdKey}
        >
          {createdKey ? (
            <>
              <div className="key-created-banner" role="status" aria-live="polite">
                <p>{t('apiKeys.keyCreatedMessage')}</p>
                <div className="key-display">
                  <code>{createdKey}</code>
                  <button
                    type="button"
                    className="copy-btn"
                    onClick={handleCopy}
                    aria-label={copied ? t('apiKeys.copiedCreatedKey') : t('apiKeys.copyCreatedKey')}
                  >
                    {copied ? t('apiKeys.copied') : t('apiKeys.copy')}
                  </button>
                </div>
              </div>
              <div className="modal-actions">
                <button type="button" className="btn btn-primary" onClick={closeModal}>{t('common.done')}</button>
              </div>
            </>
          ) : (
            <form onSubmit={handleSubmit}>
              <div className="form-grid">
                <div className="form-group">
                  <label htmlFor="api-key-name">{t('apiKeys.keyName')}</label>
                  <input
                    id="api-key-name"
                    value={form.name}
                    onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
                    placeholder="my-app"
                    required
                    disabled={!!editingName}
                    aria-label={t('apiKeys.keyName')}
                  />
                </div>
                <div className="form-group">
                  <label htmlFor="api-key-description">{t('apiKeys.description')}</label>
                  <input
                    id="api-key-description"
                    value={form.description}
                    onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))}
                    placeholder={t('apiKeys.descriptionPlaceholder')}
                    aria-label={t('apiKeys.description')}
                  />
                </div>
                <div className="form-group form-group--full">
                  <label htmlFor="api-key-secret">{t('apiKeys.apiKey')}</label>
                  <input
                    id="api-key-secret"
                    value={form.apiKey}
                    onChange={(e) => setForm((p) => ({ ...p, apiKey: e.target.value }))}
                    placeholder={editingName ? t('apiKeys.apiKeyPlaceholderEdit') : t('apiKeys.apiKeyPlaceholderNew')}
                    aria-label={t('apiKeys.apiKey')}
                  />
                  <button type="button" className="btn btn-sm btn-secondary generate-btn" onClick={() => setForm((p) => ({ ...p, apiKey: generateKey() }))}>
                    {t('apiKeys.generateKey')}
                  </button>
                </div>
                <div className="form-group form-group--full">
                  <label className="checkbox-list-item master-key-toggle">
                    <input
                      type="checkbox"
                      checked={form.isMaster}
                      onChange={() => setForm((p) => ({ ...p, isMaster: !p.isMaster }))}
                    />
                    <span className="checkbox-list-label">{t('apiKeys.masterKey')}</span>
                    <span className="tag tag-master">{t('apiKeys.allAccess')}</span>
                  </label>
                </div>
                <div className="form-group form-group--full">
                  <label id="api-key-allowed-databases-label">{t('apiKeys.allowedDatabases')}</label>
                  <div className="checkbox-list" role="group" aria-labelledby="api-key-allowed-databases-label">
                    {databases.length === 0 && s3Connections.length === 0 && nasConnections.length === 0 && (
                      <div className="checkbox-list-empty">{t('apiKeys.noneSelected')}</div>
                    )}
                    {databases.map((db) => (
                      <label key={`db-${db.alias}`} className={accessItemClass}>
                        <input
                          type="checkbox"
                          checked={form.allowedDatabases.includes(db.alias)}
                          disabled={form.isMaster}
                          onChange={() => toggleDb(db.alias)}
                        />
                        <span className="checkbox-list-label">{db.alias}</span>
                        <span className="tag">{db.db_type}</span>
                      </label>
                    ))}
                    {s3Connections.map((conn) => (
                      <label key={`s3-${conn.alias}`} className={accessItemClass}>
                        <input
                          type="checkbox"
                          checked={form.allowedDatabases.includes(conn.alias)}
                          disabled={form.isMaster}
                          onChange={() => toggleDb(conn.alias)}
                        />
                        <span className="checkbox-list-label">{conn.alias}</span>
                        <span className="tag">S3</span>
                      </label>
                    ))}
                    {nasConnections.map((conn) => (
                      <label key={`nas-${conn.alias}`} className={accessItemClass}>
                        <input
                          type="checkbox"
                          checked={form.allowedDatabases.includes(conn.alias)}
                          disabled={form.isMaster}
                          onChange={() => toggleDb(conn.alias)}
                        />
                        <span className="checkbox-list-label">{conn.alias}</span>
                        <span className="tag">NAS</span>
                      </label>
                    ))}
                  </div>
                </div>
                <div className="form-group form-group--full">
                  <label id="api-key-allowed-routes-label">{t('apiKeys.allowedRoutes')}</label>
                  <div className="checkbox-list" role="group" aria-labelledby="api-key-allowed-routes-label">
                    {routes.length === 0 && <div className="checkbox-list-empty">{t('apiKeys.noneSelected')}</div>}
                    {routes.map((r) => (
                      <label key={r.id} className={accessItemClass}>
                        <input
                          type="checkbox"
                          checked={form.allowedRoutes.includes(r.id)}
                          disabled={form.isMaster}
                          onChange={() => toggleRoute(r.id)}
                        />
                        <span className="checkbox-list-label">{r.name || r.id}</span>
                        <span className="tag">{r.uri}</span>
                      </label>
                    ))}
                  </div>
                </div>
                <div className="form-group form-group--full">
                  <label id="api-key-write-permissions-label">{t('apiKeys.writePermissions')}</label>
                  <div
                    className="checkbox-list"
                    role="group"
                    aria-labelledby="api-key-write-permissions-label"
                    aria-describedby="api-key-write-permissions-hint"
                  >
                    <label className="checkbox-list-item">
                      <input
                        type="checkbox"
                        checked={form.allowInsert}
                        onChange={() => setForm((p) => ({ ...p, allowInsert: !p.allowInsert }))}
                      />
                      <span className="checkbox-list-label">{t('apiKeys.allowInsert')}</span>
                    </label>
                    <label className="checkbox-list-item">
                      <input
                        type="checkbox"
                        checked={form.allowUpdate}
                        onChange={() => setForm((p) => ({ ...p, allowUpdate: !p.allowUpdate }))}
                      />
                      <span className="checkbox-list-label">{t('apiKeys.allowUpdate')}</span>
                    </label>
                    <label className="checkbox-list-item">
                      <input
                        type="checkbox"
                        checked={form.allowDelete}
                        onChange={() => setForm((p) => ({ ...p, allowDelete: !p.allowDelete }))}
                      />
                      <span className="checkbox-list-label">{t('apiKeys.allowDelete')}</span>
                    </label>
                  </div>
                  <small id="api-key-write-permissions-hint" className="form-hint">
                    {t('apiKeys.writePermissionsHint')}
                  </small>
                </div>
                <div className="form-group form-group--full">
                  <label htmlFor="api-key-allowed-tables">{t('apiKeys.allowedTables')}</label>
                  <input
                    id="api-key-allowed-tables"
                    value={form.allowedTables}
                    onChange={(e) => setForm((p) => ({ ...p, allowedTables: e.target.value }))}
                    placeholder={t('apiKeys.allowedTablesPlaceholder')}
                    aria-label={t('apiKeys.allowedTables')}
                    aria-describedby="api-key-allowed-tables-hint"
                  />
                  <small id="api-key-allowed-tables-hint" className="form-hint">
                    {t('apiKeys.allowedTablesHint')}
                  </small>
                </div>
                <div className="form-group">
                  <label htmlFor="api-key-rate-limit">{t('apiKeys.rateLimit')}</label>
                  <input
                    id="api-key-rate-limit"
                    type="number"
                    min={1}
                    step={1}
                    value={form.rateLimit}
                    onChange={(e) => setForm((p) => ({ ...p, rateLimit: e.target.value }))}
                    placeholder={t('apiKeys.rateLimitHint')}
                    aria-label={t('apiKeys.rateLimit')}
                    aria-describedby="api-key-rate-limit-hint"
                  />
                  <small id="api-key-rate-limit-hint" className="form-hint">
                    {t('apiKeys.rateLimitHint')}
                  </small>
                </div>
              </div>

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
                <button type="submit" className="btn btn-primary" disabled={isSaving} aria-busy={isSaving}>
                  {isSaving ? t('common.saving') : editingName ? t('common.update') : t('common.create')}
                </button>
              </div>
            </form>
          )}
        </ResourceModal>
      )}
    </div>
  );
}

export default ApiKeys;
