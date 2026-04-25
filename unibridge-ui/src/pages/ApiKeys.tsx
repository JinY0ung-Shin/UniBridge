import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getApiKeys,
  createApiKey,
  updateApiKey,
  deleteApiKey,
  getAdminDatabases,
  getGatewayRoutes,
  type ApiKey,
} from '../api/client';
import { useToast } from '../components/useToast';
import './ApiKeys.css';

function generateKey(): string {
  return 'key-' + crypto.randomUUID().replace(/-/g, '');
}

interface FormState {
  name: string;
  description: string;
  apiKey: string;
  allowedDatabases: string[];
  allowedRoutes: string[];
}

const emptyForm: FormState = {
  name: '',
  description: '',
  apiKey: '',
  allowedDatabases: [],
  allowedRoutes: [],
};

function ApiKeys() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  const [showModal, setShowModal] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>({ ...emptyForm });
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const keysQuery = useQuery({ queryKey: ['api-keys'], queryFn: getApiKeys });
  const dbsQuery = useQuery({ queryKey: ['admin-databases'], queryFn: getAdminDatabases });
  const routesQuery = useQuery({ queryKey: ['gateway-routes'], queryFn: getGatewayRoutes });

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

  function openCreate() {
    setForm({ ...emptyForm, apiKey: generateKey() });
    setEditingName(null);
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function openEdit(k: ApiKey) {
    setForm({
      name: k.name,
      description: k.description,
      apiKey: '',
      allowedDatabases: k.allowed_databases,
      allowedRoutes: k.allowed_routes,
    });
    setEditingName(k.name);
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingName(null);
    setCreatedKey(null);
    setCopied(false);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (editingName) {
      const body: Record<string, unknown> = {
        description: form.description,
        allowed_databases: form.allowedDatabases,
        allowed_routes: form.allowedRoutes,
      };
      if (form.apiKey.trim()) body.api_key = form.apiKey.trim();
      updateMut.mutate({ name: editingName, body });
    } else {
      createMut.mutate({
        name: form.name.trim(),
        description: form.description,
        api_key: form.apiKey.trim() || undefined,
        allowed_databases: form.allowedDatabases,
        allowed_routes: form.allowedRoutes,
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
      await navigator.clipboard.writeText(createdKey);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
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

  return (
    <div className="api-keys">
      <div className="page-header">
        <div>
          <h1>{t('apiKeys.title')}</h1>
          <p className="page-subtitle">{t('apiKeys.subtitle')}</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>{t('apiKeys.addKey')}</button>
      </div>

      {keysQuery.isLoading && <div className="loading-message">{t('apiKeys.loadingKeys')}</div>}
      {keysQuery.isError && <div className="error-banner">{t('apiKeys.loadFailed')}</div>}

      {keys.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('apiKeys.keyName')}</th>
                <th>{t('apiKeys.description')}</th>
                <th>{t('apiKeys.apiKey')}</th>
                <th>{t('apiKeys.allowedDatabases')}</th>
                <th>{t('apiKeys.allowedRoutes')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.name}>
                  <td className="cell-alias">{k.name}</td>
                  <td>{k.description || '\u2014'}</td>
                  <td className="cell-key">{k.api_key || '\u2014'}</td>
                  <td><div className="cell-tags">{renderTags(k.allowed_databases)}</div></td>
                  <td><div className="cell-tags">{renderTags(k.allowed_routes)}</div></td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-sm btn-secondary" onClick={() => openEdit(k)}>{t('common.edit')}</button>
                      <button className="btn btn-sm btn-danger" onClick={() => handleDelete(k)} disabled={deleteMut.isPending}>{t('common.delete')}</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!keysQuery.isLoading && keys.length === 0 && !keysQuery.isError && (
        <div className="empty-state">
          <h3>{t('apiKeys.noKeys')}</h3>
          <p>{t('apiKeys.noKeysDesc')}</p>
        </div>
      )}

      {showModal && (
        <div className="modal-overlay" onClick={createdKey ? undefined : closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingName ? t('apiKeys.editTitle') : t('apiKeys.addTitle')}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>

            {createdKey ? (
              <>
                <div className="key-created-banner">
                  <p>{t('apiKeys.keyCreatedMessage')}</p>
                  <div className="key-display">
                    <code>{createdKey}</code>
                    <button className="copy-btn" onClick={handleCopy}>
                      {copied ? t('apiKeys.copied') : t('apiKeys.copy')}
                    </button>
                  </div>
                </div>
                <div className="modal-actions">
                  <button className="btn btn-primary" onClick={closeModal}>{t('common.done')}</button>
                </div>
              </>
            ) : (
              <form onSubmit={handleSubmit}>
                <div className="form-grid">
                  <div className="form-group">
                    <label>{t('apiKeys.keyName')}</label>
                    <input
                      value={form.name}
                      onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
                      placeholder="my-app"
                      required
                      disabled={!!editingName}
                    />
                  </div>
                  <div className="form-group">
                    <label>{t('apiKeys.description')}</label>
                    <input
                      value={form.description}
                      onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))}
                      placeholder={t('apiKeys.descriptionPlaceholder')}
                    />
                  </div>
                  <div className="form-group form-group--full">
                    <label>{t('apiKeys.apiKey')}</label>
                    <input
                      value={form.apiKey}
                      onChange={(e) => setForm((p) => ({ ...p, apiKey: e.target.value }))}
                      placeholder={editingName ? t('apiKeys.apiKeyPlaceholderEdit') : t('apiKeys.apiKeyPlaceholderNew')}
                    />
                    <button type="button" className="btn btn-sm btn-secondary generate-btn" onClick={() => setForm((p) => ({ ...p, apiKey: generateKey() }))}>
                      {t('apiKeys.generateKey')}
                    </button>
                  </div>
                  <div className="form-group form-group--full">
                    <label>{t('apiKeys.allowedDatabases')}</label>
                    <div className="checkbox-list">
                      {databases.length === 0 && <div className="checkbox-list-empty">{t('apiKeys.noneSelected')}</div>}
                      {databases.map((db) => (
                        <label key={db.alias} className="checkbox-list-item">
                          <input
                            type="checkbox"
                            checked={form.allowedDatabases.includes(db.alias)}
                            onChange={() => toggleDb(db.alias)}
                          />
                          <span className="checkbox-list-label">{db.alias}</span>
                          <span className="tag">{db.db_type}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                  <div className="form-group form-group--full">
                    <label>{t('apiKeys.allowedRoutes')}</label>
                    <div className="checkbox-list">
                      {routes.length === 0 && <div className="checkbox-list-empty">{t('apiKeys.noneSelected')}</div>}
                      {routes.map((r) => (
                        <label key={r.id} className="checkbox-list-item">
                          <input
                            type="checkbox"
                            checked={form.allowedRoutes.includes(r.id)}
                            onChange={() => toggleRoute(r.id)}
                          />
                          <span className="checkbox-list-label">{r.name || r.id}</span>
                          <span className="tag">{r.uri}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                </div>

                <div className="modal-actions">
                  <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
                  <button type="submit" className="btn btn-primary" disabled={isSaving}>
                    {isSaving ? t('common.saving') : editingName ? t('common.update') : t('common.create')}
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default ApiKeys;
