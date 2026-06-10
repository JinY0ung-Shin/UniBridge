import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getQueryHistory } from '../api/client';
import { formatKST } from '../utils/time';

const HISTORY_LIMIT = 50;

interface QueryHistoryPanelProps {
  onLoad: (sql: string, databaseAlias: string | null) => void;
}

function truncateSql(sql: string, maxLen = 80) {
  return sql.length > maxLen ? sql.slice(0, maxLen) + '...' : sql;
}

function QueryHistoryPanel({ onLoad }: QueryHistoryPanelProps) {
  const { t } = useTranslation();

  const historyQuery = useQuery({
    queryKey: ['query-history'],
    queryFn: () => getQueryHistory({ limit: HISTORY_LIMIT }),
  });

  const items = historyQuery.data?.items ?? [];

  return (
    <div className="workspace-panel">
      <div className="workspace-panel-header">
        <span className="workspace-panel-hint">{t('queryPlayground.historyHint')}</span>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={() => historyQuery.refetch()}
        >
          {t('common.refresh')}
        </button>
      </div>

      {historyQuery.isLoading && <div className="loading-message">{t('common.loading')}</div>}
      {historyQuery.isError && (
        <div className="error-banner">{t('queryPlayground.historyLoadFailed')}</div>
      )}

      {items.length > 0 && (
        <div className="table-container">
          <table className="data-table history-table">
            <thead>
              <tr>
                <th>{t('auditLogs.timestamp')}</th>
                <th>{t('connections.database')}</th>
                <th>{t('auditLogs.sql')}</th>
                <th>{t('common.status')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((entry) => (
                <tr
                  key={entry.id}
                  className="history-row"
                  title={t('queryPlayground.loadIntoEditor')}
                  onClick={() => onLoad(entry.sql, entry.database_alias)}
                >
                  <td className="cell-timestamp">{formatKST(entry.timestamp)}</td>
                  <td>{entry.database_alias}</td>
                  <td className="cell-sql mono">{truncateSql(entry.sql)}</td>
                  <td>
                    <span
                      className={`badge ${entry.status === 'success' ? 'badge-ok' : 'badge-error'}`}
                    >
                      {entry.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!historyQuery.isLoading && !historyQuery.isError && items.length === 0 && (
        <div className="empty-state">
          <h3>{t('queryPlayground.historyEmpty')}</h3>
          <p>{t('queryPlayground.historyEmptyDesc')}</p>
        </div>
      )}
    </div>
  );
}

export default QueryHistoryPanel;
