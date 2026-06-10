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

  return (
    <div className="workspace-panel">
      <div className="workspace-panel-header">
        <span className="workspace-panel-hint">{t('queryPlayground.savedHint')}</span>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={() => savedQueriesQuery.refetch()}
        >
          {t('common.refresh')}
        </button>
      </div>

      {savedQueriesQuery.isLoading && (
        <div className="loading-message">{t('common.loading')}</div>
      )}
      {savedQueriesQuery.isError && (
        <div className="error-banner">{t('queryPlayground.savedLoadFailed')}</div>
      )}

      {items.length > 0 && (
        <div className="table-container">
          <table className="data-table saved-table">
            <thead>
              <tr>
                <th>{t('common.name')}</th>
                <th>{t('connections.database')}</th>
                <th>{t('auditLogs.sql')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((saved) => (
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
                        onClick={() => onLoad(saved.sql_text, saved.database_alias)}
                      >
                        {t('queryPlayground.load')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        disabled={deleteMutation.isPending}
                        onClick={() => handleDelete(saved)}
                      >
                        {t('common.delete')}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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
