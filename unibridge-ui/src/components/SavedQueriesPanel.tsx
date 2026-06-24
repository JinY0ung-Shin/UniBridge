import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { deleteSavedQuery, getSavedQueries, type SavedQuery } from '../api/client';
import { useToast } from './useToast';

interface SavedQueriesPanelProps {
  onLoad: (sql: string, databaseAlias: string | null) => void;
}

function truncateSql(sql: string, maxLen = 80) {
  return sql.length > maxLen ? sql.slice(0, maxLen) + '...' : sql;
}

function SavedQueriesPanel({ onLoad }: SavedQueriesPanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { addToast } = useToast();
  const [savedSearch, setSavedSearch] = useState('');

  const savedQueriesQuery = useQuery({
    queryKey: ['saved-queries'],
    queryFn: getSavedQueries,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteSavedQuery(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['saved-queries'] });
    },
    onError: () => {
      addToast({ type: 'error', title: t('queryPlayground.deleteFailed') });
    },
  });

  function handleDelete(saved: SavedQuery) {
    if (window.confirm(t('queryPlayground.deleteConfirm', { name: saved.name }))) {
      deleteMutation.mutate(saved.id);
    }
  }

  const items = savedQueriesQuery.data ?? [];
  const normalizedSavedSearch = savedSearch.trim().toLowerCase();
  const filteredItems = normalizedSavedSearch
    ? items.filter((saved) => [
        saved.name,
        saved.description,
        saved.database_alias,
        saved.sql_text,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(normalizedSavedSearch))
    : items;

  return (
    <div className="workspace-panel">
      <div className="workspace-panel-header">
        <span className="workspace-panel-hint">{t('queryPlayground.savedHint')}</span>
        <div className="workspace-panel-actions">
          {items.length > 0 && (
            <input
              className="saved-search-input"
              type="search"
              value={savedSearch}
              onChange={(event) => setSavedSearch(event.target.value)}
              placeholder={t('queryPlayground.savedSearchPlaceholder')}
              aria-label={t('queryPlayground.savedSearchPlaceholder')}
            />
          )}
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            aria-label={t('queryPlayground.refreshSavedQueries')}
            title={t('queryPlayground.refreshSavedQueries')}
            onClick={() => savedQueriesQuery.refetch()}
          >
            {t('common.refresh')}
          </button>
        </div>
      </div>

      {savedQueriesQuery.isLoading && (
        <div className="loading-message" role="status">{t('common.loading')}</div>
      )}
      {savedQueriesQuery.isError && (
        <div className="error-banner" role="alert">{t('queryPlayground.savedLoadFailed')}</div>
      )}

      {items.length > 0 && filteredItems.length > 0 && (
        <div className="table-container">
          <table className="data-table saved-table">
            <thead>
              <tr>
                <th scope="col">{t('common.name')}</th>
                <th scope="col">{t('connections.database')}</th>
                <th scope="col">{t('auditLogs.sql')}</th>
                <th scope="col">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {filteredItems.map((saved) => {
                const isDeleting = deleteMutation.isPending && deleteMutation.variables === saved.id;
                return (
                <tr key={saved.id}>
                  <td className="saved-name" title={saved.description || undefined}>
                    {saved.name}
                  </td>
                  <td>{saved.database_alias ?? '—'}</td>
                  <td className="cell-sql mono">{truncateSql(saved.sql_text)}</td>
                  <td>
                    <div className="saved-actions">
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        aria-label={t('queryPlayground.loadSavedQuery', { name: saved.name })}
                        title={t('queryPlayground.loadSavedQuery', { name: saved.name })}
                        onClick={() => onLoad(saved.sql_text, saved.database_alias)}
                      >
                        {t('queryPlayground.load')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        aria-label={t('queryPlayground.deleteSavedQuery', { name: saved.name })}
                        title={t('queryPlayground.deleteSavedQuery', { name: saved.name })}
                        disabled={deleteMutation.isPending}
                        aria-busy={isDeleting}
                        onClick={() => handleDelete(saved)}
                      >
                        {isDeleting ? t('common.deleting') : t('common.delete')}
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

      {!savedQueriesQuery.isLoading && !savedQueriesQuery.isError && items.length > 0 && filteredItems.length === 0 && (
        <div className="empty-state">
          <h3>{t('queryPlayground.savedNoSearchResults')}</h3>
          <p>{t('queryPlayground.savedNoSearchResultsDesc')}</p>
          <button type="button" className="btn btn-secondary empty-state-action" onClick={() => setSavedSearch('')}>
            {t('common.clearSearch')}
          </button>
        </div>
      )}

      {!savedQueriesQuery.isLoading && !savedQueriesQuery.isError && items.length === 0 && (
        <div className="empty-state">
          <h3>{t('queryPlayground.savedEmpty')}</h3>
          <p>{t('queryPlayground.savedEmptyDesc')}</p>
        </div>
      )}
    </div>
  );
}

export default SavedQueriesPanel;
