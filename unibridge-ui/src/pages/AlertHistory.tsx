import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getAlertHistory, type AlertHistoryEntry } from '../api/client';
import { formatKST } from '../utils/time';
import './AuditLogs.css';
import './AlertHistory.css';

const PAGE_SIZE = 50;
const EMPTY_FILTER_FORM = {
  alert_type: '',
  target: '',
};

function AlertHistory() {
  const { t } = useTranslation();
  const [page, setPage] = useState(0);

  const [filterForm, setFilterForm] = useState({ ...EMPTY_FILTER_FORM });

  const [appliedFilterForm, setAppliedFilterForm] = useState({ ...EMPTY_FILTER_FORM });

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
  const hasFilterValues =
    Object.values(filterForm).some(Boolean) || Object.values(appliedFilterForm).some(Boolean);

  function applyFilters() {
    setPage(0);
    setAppliedFilterForm({ ...filterForm });
  }

  function resetFilters() {
    setPage(0);
    setFilterForm({ ...EMPTY_FILTER_FORM });
    setAppliedFilterForm({ ...EMPTY_FILTER_FORM });
  }

  function goToPage(newPage: number) {
    setPage(newPage);
  }

  return (
    <div className="alert-history">
      <div className="page-header">
        <h1>{t('alerts.historyTitle')}</h1>
        <p className="page-subtitle">{t('alerts.historySubtitle')}</p>
      </div>

      {/* Filter bar */}
      <div className="filter-bar">
        <div className="filter-field">
          <label htmlFor="alert-history-type-filter">{t('alerts.filterAlertType')}</label>
          <select
            id="alert-history-type-filter"
            value={filterForm.alert_type}
            onChange={(e) => setFilterForm((f) => ({ ...f, alert_type: e.target.value }))}
            className="filter-select"
          >
            <option value="">{t('alerts.filterAlertType')}</option>
            <option value="triggered">{t('alerts.triggered')}</option>
            <option value="resolved">{t('alerts.resolved')}</option>
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="alert-history-target-filter">{t('alerts.filterTarget')}</label>
          <input
            id="alert-history-target-filter"
            type="text"
            placeholder={t('alerts.filterTarget')}
            value={filterForm.target}
            onChange={(e) => setFilterForm((f) => ({ ...f, target: e.target.value }))}
            className="filter-input"
            onKeyDown={(e) => e.key === 'Enter' && applyFilters()}
          />
        </div>
        <div className="filter-actions">
          <button type="button" className="btn btn-primary" onClick={applyFilters}>
            {t('common.search')}
          </button>
          <button type="button" className="btn btn-secondary" onClick={resetFilters} disabled={!hasFilterValues}>
            {t('common.resetFilters')}
          </button>
        </div>
      </div>

      {historyQuery.isLoading && (
        <div className="loading-message" role="status">{t('common.loading')}</div>
      )}

      {historyQuery.isError && (
        <div className="error-banner" role="alert">{t('alerts.loadFailed')}</div>
      )}

      {entries.length > 0 && (
        <>
          <div className="table-container">
            <table className="data-table alert-history-table">
              <thead>
                <tr>
                  <th scope="col">{t('alerts.sentAt')}</th>
                  <th scope="col">{t('alerts.filterAlertType')}</th>
                  <th scope="col">{t('alerts.target')}</th>
                  <th scope="col">{t('alerts.message')}</th>
                  <th scope="col">{t('common.status')}</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.id}>
                    <td className="cell-timestamp">{formatKST(entry.sent_at)}</td>
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
                    <td>{entry.display_target || entry.target}</td>
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
              type="button"
              className="btn btn-sm btn-secondary"
              aria-label={t('common.previousPage')}
              title={t('common.previousPage')}
              disabled={page === 0}
              onClick={() => goToPage(page - 1)}
            >
              {t('common.previous')}
            </button>
            <span className="page-info" role="status" aria-live="polite">
              {t('common.page', { page: page + 1 })}
            </span>
            <button
              type="button"
              className="btn btn-sm btn-secondary"
              aria-label={t('common.nextPage')}
              title={t('common.nextPage')}
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
