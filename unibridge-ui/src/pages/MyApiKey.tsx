import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AxiosError } from 'axios';
import {
  getMyApiKey,
  createMyApiKey,
  regenerateMyApiKey,
  deleteMyApiKey,
} from '../api/client';
import { useToast } from '../components/useToast';
import './ApiKeys.css';

function MyApiKey() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const keyQuery = useQuery({ queryKey: ['my-api-key'], queryFn: getMyApiKey });

  const createMut = useMutation({
    mutationFn: createMyApiKey,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['my-api-key'] });
      if (result.api_key) setRevealedKey(result.api_key);
    },
    onError: (err: unknown) => {
      if (err instanceof AxiosError && err.response?.status === 409) {
        addToast({ type: 'error', title: t('myApiKey.alreadyExists') });
        queryClient.invalidateQueries({ queryKey: ['my-api-key'] });
      } else {
        addToast({ type: 'error', title: t('myApiKey.createFailed') });
      }
    },
  });

  const regenerateMut = useMutation({
    mutationFn: regenerateMyApiKey,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['my-api-key'] });
      if (result.api_key) setRevealedKey(result.api_key);
    },
    onError: () => addToast({ type: 'error', title: t('myApiKey.regenerateFailed') }),
  });

  const deleteMut = useMutation({
    mutationFn: deleteMyApiKey,
    onSuccess: () => {
      setRevealedKey(null);
      queryClient.invalidateQueries({ queryKey: ['my-api-key'] });
    },
    onError: () => addToast({ type: 'error', title: t('myApiKey.deleteFailed') }),
  });

  const apiKey = keyQuery.data ?? null;

  function handleCreate() {
    setRevealedKey(null);
    setCopied(false);
    createMut.mutate();
  }

  function handleRegenerate() {
    if (window.confirm(t('myApiKey.regenerateConfirm'))) {
      setRevealedKey(null);
      setCopied(false);
      regenerateMut.mutate();
    }
  }

  function handleDelete() {
    if (window.confirm(t('myApiKey.deleteConfirm'))) {
      deleteMut.mutate();
    }
  }

  async function handleCopy() {
    if (revealedKey) {
      await navigator.clipboard.writeText(revealedKey);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  const isBusy = createMut.isPending || regenerateMut.isPending || deleteMut.isPending;

  return (
    <div className="api-keys">
      <div className="page-header">
        <div>
          <h1>{t('myApiKey.title')}</h1>
          <p className="page-subtitle">{t('myApiKey.subtitle')}</p>
        </div>
      </div>

      {keyQuery.isLoading && <div className="loading-message">{t('myApiKey.loading')}</div>}
      {keyQuery.isError && <div className="error-banner">{t('myApiKey.loadFailed')}</div>}

      {revealedKey && (
        <div className="key-created-banner">
          <p>{t('myApiKey.keyCreatedMessage')}</p>
          <div className="key-display">
            <code>{revealedKey}</code>
            <button className="copy-btn" onClick={handleCopy}>
              {copied ? t('myApiKey.copied') : t('myApiKey.copy')}
            </button>
          </div>
        </div>
      )}

      {!keyQuery.isLoading && !keyQuery.isError && apiKey === null && (
        <div className="empty-state">
          <h3>{t('myApiKey.noKey')}</h3>
          <p>{t('myApiKey.noKeyDesc')}</p>
          <button className="btn btn-primary" onClick={handleCreate} disabled={isBusy}>
            {createMut.isPending ? t('common.saving') : t('myApiKey.createKey')}
          </button>
        </div>
      )}

      {!keyQuery.isLoading && !keyQuery.isError && apiKey && (
        <div className="table-container">
          <table className="data-table">
            <tbody>
              <tr>
                <th>{t('myApiKey.keyName')}</th>
                <td className="cell-alias">{apiKey.name}</td>
              </tr>
              <tr>
                <th>{t('myApiKey.apiKey')}</th>
                <td className="cell-key">{apiKey.api_key || '—'}</td>
              </tr>
              <tr>
                <th>{t('myApiKey.scope')}</th>
                <td>{t('myApiKey.scopeSummary')}</td>
              </tr>
            </tbody>
          </table>
          <div className="action-buttons" style={{ marginTop: 16 }}>
            <button className="btn btn-secondary" onClick={handleRegenerate} disabled={isBusy}>
              {regenerateMut.isPending ? t('common.saving') : t('myApiKey.regenerate')}
            </button>
            <button className="btn btn-danger" onClick={handleDelete} disabled={isBusy}>
              {deleteMut.isPending ? t('common.saving') : t('myApiKey.delete')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default MyApiKey;
