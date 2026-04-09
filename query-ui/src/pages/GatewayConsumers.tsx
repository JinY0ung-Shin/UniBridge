import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  getGatewayConsumers,
  saveGatewayConsumer,
  deleteGatewayConsumer,
  type GatewayConsumer,
} from '../api/client';
import './GatewayConsumers.css';

function generateKey(): string {
  return 'key-' + crypto.randomUUID().replace(/-/g, '');
}

function GatewayConsumers() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [showModal, setShowModal] = useState(false);
  const [editingUsername, setEditingUsername] = useState<string | null>(null);
  const [username, setUsername] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState('');
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const consumersQuery = useQuery({
    queryKey: ['gateway-consumers'],
    queryFn: getGatewayConsumers,
  });

  const saveMutation = useMutation({
    mutationFn: (data: { username: string; body: Record<string, unknown> }) =>
      saveGatewayConsumer(data.username, data.body),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['gateway-consumers'] });
      if (result.key_created && result.api_key) {
        setCreatedKey(result.api_key);
      } else {
        closeModal();
      }
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        setError(axiosErr.response?.data?.detail ?? t('gatewayConsumers.saveFailed'));
      } else {
        setError(t('gatewayConsumers.saveFailed'));
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (uname: string) => deleteGatewayConsumer(uname),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['gateway-consumers'] });
    },
    onError: (err: unknown) => {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string } } };
        alert(axiosErr.response?.data?.detail ?? t('gatewayConsumers.deleteFailed'));
      }
    },
  });

  const consumers = consumersQuery.data?.items ?? [];

  function openCreate() {
    setEditingUsername(null);
    setUsername('');
    setApiKey(generateKey());
    setError('');
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function openEdit(c: GatewayConsumer) {
    setEditingUsername(c.username);
    setUsername(c.username);
    setApiKey('');
    setError('');
    setCreatedKey(null);
    setCopied(false);
    setShowModal(true);
  }

  function closeModal() {
    setShowModal(false);
    setEditingUsername(null);
    setCreatedKey(null);
    setError('');
    setCopied(false);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim()) return;
    const body: Record<string, unknown> = {};
    if (apiKey.trim()) {
      body.api_key = apiKey.trim();
    }
    setError('');
    saveMutation.mutate({ username: username.trim(), body });
  }

  function handleDelete(c: GatewayConsumer) {
    if (window.confirm(t('gatewayConsumers.deleteConfirm', { name: c.username }))) {
      deleteMutation.mutate(c.username);
    }
  }

  async function handleCopy() {
    if (createdKey) {
      await navigator.clipboard.writeText(createdKey);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  return (
    <div className="gateway-consumers">
      <div className="page-header">
        <div>
          <h1>{t('gatewayConsumers.title')}</h1>
          <p className="page-subtitle">{t('gatewayConsumers.subtitle')}</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>{t('gatewayConsumers.addConsumer')}</button>
      </div>

      {consumersQuery.isLoading && <div className="loading-message">{t('gatewayConsumers.loadingConsumers')}</div>}
      {consumersQuery.isError && <div className="error-banner">{t('gatewayConsumers.loadFailed')}</div>}

      {consumers.length > 0 && (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('gatewayConsumers.username')}</th>
                <th>{t('gatewayConsumers.apiKey')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {consumers.map((c) => (
                <tr key={c.username}>
                  <td className="cell-alias">{c.username}</td>
                  <td className="cell-key">{c.api_key || '—'}</td>
                  <td>
                    <div className="action-buttons">
                      <button className="btn btn-sm btn-secondary" onClick={() => openEdit(c)}>{t('common.edit')}</button>
                      <button className="btn btn-sm btn-danger" onClick={() => handleDelete(c)} disabled={deleteMutation.isPending}>{t('common.delete')}</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!consumersQuery.isLoading && consumers.length === 0 && !consumersQuery.isError && (
        <div className="empty-state">
          <h3>{t('gatewayConsumers.noConsumers')}</h3>
          <p>{t('gatewayConsumers.noConsumersDesc')}</p>
        </div>
      )}

      {showModal && (
        <div className="modal-overlay" onClick={createdKey ? undefined : closeModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{editingUsername ? t('gatewayConsumers.editTitle') : t('gatewayConsumers.addTitle')}</h2>
              <button className="modal-close" onClick={closeModal}>&times;</button>
            </div>

            {createdKey ? (
              <>
                <div className="key-created-banner">
                  <p>{t('gatewayConsumers.keyCreatedMessage')}</p>
                  <div className="key-display">
                    <code>{createdKey}</code>
                    <button className="copy-btn" onClick={handleCopy}>
                      {copied ? t('gatewayConsumers.copied') : t('gatewayConsumers.copy')}
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
                  <div className="form-group form-group--full">
                    <label>{t('gatewayConsumers.username')}</label>
                    <input
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      placeholder="my-app"
                      required
                      disabled={!!editingUsername}
                    />
                  </div>
                  <div className="form-group form-group--full">
                    <label>{t('gatewayConsumers.apiKey')}</label>
                    <input
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder={editingUsername ? t('gatewayConsumers.apiKeyPlaceholderEdit') : t('gatewayConsumers.apiKeyPlaceholderNew')}
                    />
                    <button
                      type="button"
                      className="btn btn-sm btn-secondary generate-btn"
                      onClick={() => setApiKey(generateKey())}
                    >
                      {t('gatewayConsumers.generateKey')}
                    </button>
                  </div>
                </div>

                {error && <div className="form-error">{error}</div>}

                <div className="modal-actions">
                  <button type="button" className="btn btn-secondary" onClick={closeModal}>{t('common.cancel')}</button>
                  <button type="submit" className="btn btn-primary" disabled={saveMutation.isPending}>
                    {saveMutation.isPending ? t('common.saving') : editingUsername ? t('common.update') : t('common.create')}
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

export default GatewayConsumers;
