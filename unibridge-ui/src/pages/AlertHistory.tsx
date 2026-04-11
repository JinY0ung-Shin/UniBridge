import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getAlertHistory, type AlertHistoryEntry } from '../api/client';
import './AlertHistory.css';

const PAGE_SIZE = 50;

function AlertHistory() {
  const { t } = useTranslation();
  const [page, setPage] = useState(0);

  const [filterForm, setFilterForm] = useState({
    alert_type: '',
    target: '',
  });

  const [appliedFilterForm, setAppliedFilterForm] = useState({
    alert_type: '',
    target: '',
  });

  const historyQuery = useQuery({
    queryKey: ['alert-history', appliedFilterForm, page],
    queryFn: () =>
      getAlertHistory({
        alert_type: appliedFilterForm.alert_type || undefined,
        target: appliedFilterForm.target || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    refetchInterval: 30_000,
  });

  const entries: AlertHistoryEntry[] = historyQuery.data ?? [];
  const hasMore = entries.length === PAGE_SIZE;

  function applyFilters() {
    setPage(0);
    setAppliedFilterForm({ ...filterForm });
  }

  function goToPage(newPage: number) {
    setPage(newPage);
  }

  function formatTimestamp(ts: string) {
    try {
      return new Date(ts).toLocaleString();
    } catch {
      return ts;
    }
  }

  return (
    <div className="alert-history">
      <div className="page-header">
        <h1>{t('alerts.historyTitle')}</h1>
        <p className="page-subtitle">{t('alerts.historySubtitle')}</p>
      </div>

      {/* Filter bar */}
      <div className="filter-bar">
        <select
          value={filterForm.alert_type}
          onChange={(e) => setFilterForm((f) => ({ ...f, alert_type: e.target.value }))}
          className="filter-select"
        >
          <option value="">{t('alerts.filterAlertType')}</option>
          <option value="triggered">{t('alerts.triggered')}</option>
          <option value="resolved">{t('alerts.resolved')}</option>
        </select>
        <input
          type="text"
          placeholder={t('alerts.filterTarget')}
          value={filterForm.target}
          onChange={(e) => setFilterForm((f) => ({ ...f, target: e.target.value }))}
          className="filter-input"
          onKeyDown={(e) => e.key === 'Enter' && applyFilters()}
        />
        <button className="btn btn-primary" onClick={applyFilters}>
          {t('common.search')}
        </button>
      </div>

      {historyQuery.isLoading && (
        <div className="loading-message">{t('common.loading')}</div>
      )}

      {historyQuery.isError && (
        <div className="error-banner">{t('alerts.loadFailed')}</div>
      )}

      {entries.length > 0 && (
        <>
          <div className="table-container">
            <table className="data-table alert-history-table">
              <thead>
                <tr>
                  <th>{t('alerts.sentAt')}</th>
                  <th>{t('alerts.filterAlertType')}</th>
                  <th>{t('alerts.target')}</th>
                  <th>{t('alerts.message')}</th>
                  <th>{t('common.status')}</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.id}>
                    <td className="cell-timestamp">{formatTimestamp(entry.sent_at)}</td>
                    <td>
                      <span
                        className={`badge ${
                          entry.alert_type === 'triggered' ? 'badge-error' : 'badge-ok'
                        }`}
                      >
                        {entry.alert_type === 'triggered'
                          ? t('alerts.triggered')
                          : t('alerts.resolved')}
                      </span>
                    </td>
                    <td>{entry.target}</td>
                    <td className="cell-message">{entry.message}</td>
                    <td>
                      {entry.success === null ? (
                        <span className="badge badge-neutral">—</span>
                      ) : entry.success ? (
                        <span className="badge badge-ok">{t('alerts.success')}</span>
                      ) : (
                        <span className="badge badge-error">{t('alerts.failed')}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          <div className="pagination">
            <button
              className="btn btn-sm btn-secondary"
              disabled={page === 0}
              onClick={() => goToPage(page - 1)}
            >
              {t('common.previous')}
            </button>
            <span className="page-info">
              {t('common.page', { page: page + 1 })}
            </span>
            <button
              className="btn btn-sm btn-secondary"
              disabled={!hasMore}
              onClick={() => goToPage(page + 1)}
            >
              {t('common.next')}
            </button>
          </div>
        </>
      )}

      {!historyQuery.isLoading && entries.length === 0 && !historyQuery.isError && (
        <div className="empty-state">
          <h3>{t('alerts.noHistory')}</h3>
        </div>
      )}
    </div>
  );
}

export default AlertHistory;
