import { useEffect, useRef, useState } from 'react';
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
import type { ApiKey } from '../api/client';
import { useToast } from '../components/useToast';
import { formatKST } from '../utils/time';
import './ApiKeys.css';

const MS_PER_DAY = 24 * 60 * 60 * 1000;

function daysUntil(iso: string): number {
  return Math.ceil((new Date(iso).getTime() - Date.now()) / MS_PER_DAY);
}

function cacheableApiKey(result: ApiKey): ApiKey {
  if (!result.key_created || !result.api_key) return result;
  return { ...result, api_key: `***${result.api_key.slice(-4)}` };
}

function MyApiKey() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copyTimeoutRef = useRef<number | null>(null);

  const keyQuery = useQuery({ queryKey: ['my-api-key'], queryFn: getMyApiKey });

  const createMut = useMutation({
    mutationFn: createMyApiKey,
    onSuccess: (result) => {
      queryClient.setQueryData(['my-api-key'], cacheableApiKey(result));
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
      queryClient.setQueryData(['my-api-key'], cacheableApiKey(result));
      if (result.api_key) setRevealedKey(result.api_key);
    },
    onError: () => addToast({ type: 'error', title: t('myApiKey.regenerateFailed') }),
  });

  const renewMut = useMutation({
    mutationFn: renewMyApiKey,
    onSuccess: (result) => {
      queryClient.setQueryData(['my-api-key'], result);
      addToast({ type: 'success', title: t('myApiKey.renewSuccess') });
    },
    onError: () => addToast({ type: 'error', title: t('myApiKey.renewFailed') }),
  });

  const deleteMut = useMutation({
    mutationFn: deleteMyApiKey,
    onSuccess: () => {
      setRevealedKey(null);
      setCopied(false);
      queryClient.setQueryData(['my-api-key'], null);
      addToast({ type: 'success', title: t('myApiKey.deleteSuccess') });
    },
    onError: () => addToast({ type: 'error', title: t('myApiKey.deleteFailed') }),
  });

  const apiKey = keyQuery.data ?? null;

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

  function handleCreate() {
    clearCopyTimer();
    setRevealedKey(null);
    setCopied(false);
    createMut.mutate();
  }

  function handleRegenerate() {
    if (window.confirm(t('myApiKey.regenerateConfirm'))) {
      clearCopyTimer();
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
      clearCopyTimer();
      deleteMut.mutate();
    }
  }

  async function handleCopy() {
    if (revealedKey) {
      try {
        clearCopyTimer();
        await navigator.clipboard.writeText(revealedKey);
        setCopied(true);
        copyTimeoutRef.current = window.setTimeout(() => {
          setCopied(false);
          copyTimeoutRef.current = null;
        }, 2000);
      } catch {
        setCopied(false);
        addToast({ type: 'error', title: t('myApiKey.copyFailed') });
      }
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

      {keyQuery.isLoading && <div className="loading-message" role="status">{t('myApiKey.loading')}</div>}
      {keyQuery.isError && <div className="error-banner" role="alert">{t('myApiKey.loadFailed')}</div>}

      {revealedKey && (
        <div className="key-created-banner" role="status" aria-live="polite">
          <p>{t('myApiKey.keyCreatedMessage')}</p>
          <div className="key-display">
            <code>{revealedKey}</code>
            <button
              type="button"
              className="copy-btn"
              onClick={handleCopy}
              aria-label={copied ? t('myApiKey.copiedRevealedKey') : t('myApiKey.copyRevealedKey')}
            >
              {copied ? t('myApiKey.copied') : t('myApiKey.copy')}
            </button>
          </div>
        </div>
      )}

      {!keyQuery.isLoading && !keyQuery.isError && apiKey === null && (
        <div className="empty-state">
          <h3>{t('myApiKey.noKey')}</h3>
          <p>{t('myApiKey.noKeyDesc')}</p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleCreate}
            disabled={isBusy}
            aria-busy={createMut.isPending}
          >
            {createMut.isPending ? t('common.saving') : t('myApiKey.createKey')}
          </button>
        </div>
      )}

      {!keyQuery.isLoading && !keyQuery.isError && apiKey && (
        <div className="table-container my-api-key-panel">
          {isExpired && <div className="error-banner" role="alert">{t('myApiKey.expiredBanner')}</div>}
          <table className="data-table">
            <tbody>
              <tr>
                <th scope="row">{t('myApiKey.keyName')}</th>
                <td className="cell-alias">{apiKey.name}</td>
              </tr>
              <tr>
                <th scope="row">{t('myApiKey.apiKey')}</th>
                <td className="cell-key">{apiKey.api_key || '—'}</td>
              </tr>
              <tr>
                <th scope="row">{t('myApiKey.scope')}</th>
                <td>{t('myApiKey.scopeSummary')}</td>
              </tr>
              <tr>
                <th scope="row">{t('myApiKey.expiresAt')}</th>
                <td>
                  {apiKey.expires_at ? (
                    <>
                      {formatKST(apiKey.expires_at)}{' '}
                      {isExpired ? (
                        <span className="tag tag-expired">
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
          <p className="form-hint my-api-key-renew-note">{t('myApiKey.renewDesc')}</p>
          <div className="action-buttons my-api-key-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleRenew}
              disabled={isBusy}
              aria-busy={renewMut.isPending}
            >
              {renewMut.isPending ? t('common.saving') : t('myApiKey.renew')}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={handleRegenerate}
              disabled={isBusy}
              aria-busy={regenerateMut.isPending}
            >
              {regenerateMut.isPending ? t('common.saving') : t('myApiKey.regenerate')}
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={handleDelete}
              disabled={isBusy}
              aria-busy={deleteMut.isPending}
            >
              {deleteMut.isPending ? t('common.saving') : t('myApiKey.delete')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default MyApiKey;
