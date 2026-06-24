import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { KeyboardEvent } from 'react';
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

  function handleHistoryKeyDown(
    event: KeyboardEvent<HTMLTableRowElement>,
    sql: string,
    databaseAlias: string | null,
  ) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    onLoad(sql, databaseAlias);
  }

  return (
    <div className="workspace-panel">
      <div className="workspace-panel-header">
        <span className="workspace-panel-hint">{t('queryPlayground.historyHint')}</span>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          aria-label={t('queryPlayground.refreshHistory')}
          title={t('queryPlayground.refreshHistory')}
          onClick={() => historyQuery.refetch()}
        >
          {t('common.refresh')}
        </button>
      </div>

      {historyQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {historyQuery.isError && (
        <div className="error-banner" role="alert">{t('queryPlayground.historyLoadFailed')}</div>
      )}

      {items.length > 0 && (
        <div className="table-container">
          <table className="data-table history-table">
            <thead>
              <tr>
                <th scope="col">{t('auditLogs.timestamp')}</th>
                <th scope="col">{t('connections.database')}</th>
                <th scope="col">{t('auditLogs.sql')}</th>
                <th scope="col">{t('common.status')}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((entry) => (
                <tr
                  key={entry.id}
                  className="history-row"
                  title={t('queryPlayground.loadIntoEditor')}
                  tabIndex={0}
                  role="button"
                  aria-label={`${t('queryPlayground.loadIntoEditor')}: ${truncateSql(entry.sql, 40)}`}
                  onClick={() => onLoad(entry.sql, entry.database_alias)}
                  onKeyDown={(event) => handleHistoryKeyDown(event, entry.sql, entry.database_alias)}
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
