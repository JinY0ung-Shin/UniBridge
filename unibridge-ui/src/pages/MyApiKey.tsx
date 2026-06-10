import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AxiosError } from 'axios';
import {
  getMyApiKey,
  createMyApiKey,
  regenerateMyApiKey,
  renewMyApiKey,
  deleteMyApiKey,
} from '../api/client';
import { useToast } from '../components/useToast';
import { formatKST } from '../utils/time';
import './ApiKeys.css';

const MS_PER_DAY = 24 * 60 * 60 * 1000;

function daysUntil(iso: string): number {
  return Math.ceil((new Date(iso).getTime() - Date.now()) / MS_PER_DAY);
}

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

  const renewMut = useMutation({
    mutationFn: renewMyApiKey,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['my-api-key'] });
      addToast({ type: 'success', title: t('myApiKey.renewSuccess') });
    },
    onError: () => addToast({ type: 'error', title: t('myApiKey.renewFailed') }),
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

  function handleRenew() {
    renewMut.mutate();
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

  const isBusy =
    createMut.isPending || regenerateMut.isPending || renewMut.isPending || deleteMut.isPending;

  const daysLeft = apiKey?.expires_at ? daysUntil(apiKey.expires_at) : null;
  const isExpired = daysLeft !== null && daysLeft <= 0;

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
          {isExpired && <div className="error-banner">{t('myApiKey.expiredBanner')}</div>}
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
              <tr>
                <th>{t('myApiKey.expiresAt')}</th>
                <td>
                  {apiKey.expires_at ? (
                    <>
                      {formatKST(apiKey.expires_at)}{' '}
                      {isExpired ? (
                        <span className="tag" style={{ color: 'var(--accent-red)' }}>
                          {t('myApiKey.expired')}
                        </span>
                      ) : (
                        <span className="tag">{t('myApiKey.daysLeft', { count: daysLeft })}</span>
                      )}
                    </>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            </tbody>
          </table>
          <p className="form-hint" style={{ marginTop: 12 }}>{t('myApiKey.renewDesc')}</p>
          <div className="action-buttons" style={{ marginTop: 8 }}>
            <button className="btn btn-primary" onClick={handleRenew} disabled={isBusy}>
              {renewMut.isPending ? t('common.saving') : t('myApiKey.renew')}
            </button>
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
