import { Fragment, useState, type KeyboardEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getAuditLogs, getAdminDatabases, type AuditLogParams } from '../api/client';
import { formatKST, kstDateToUtcIso } from '../utils/time';
import './AuditLogs.css';

const PAGE_SIZE = 20;
const EMPTY_FILTER_FORM = {
  database: '',
  user: '',
  from_date: '',
  to_date: '',
};

function AuditLogs() {
  const { t } = useTranslation();
  const [page, setPage] = useState(0);
  const [expandedRow, setExpandedRow] = useState<number | null>(null);

  // form state (separate from applied filters so we don't refetch on every keystroke)
  const [filterForm, setFilterForm] = useState({ ...EMPTY_FILTER_FORM });

  const [appliedFilterForm, setAppliedFilterForm] = useState({ ...EMPTY_FILTER_FORM });

  const filters: AuditLogParams = {
    database: appliedFilterForm.database || undefined,
    user: appliedFilterForm.user || undefined,
    from_date: kstDateToUtcIso(appliedFilterForm.from_date, 'start'),
    to_date: kstDateToUtcIso(appliedFilterForm.to_date, 'end'),
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  };

  const dbsQuery = useQuery({
    queryKey: ['admin-databases'],
    queryFn: getAdminDatabases,
  });

  const logsQuery = useQuery({
    queryKey: ['audit-logs', filters],
    queryFn: () => getAuditLogs(filters),
  });

  const databases = dbsQuery.data ?? [];
  const logs = logsQuery.data ?? [];
  const hasMore = logs.length === PAGE_SIZE;
  const hasFilterValues =
    Object.values(filterForm).some(Boolean) || Object.values(appliedFilterForm).some(Boolean);

  function applyFilters() {
    setExpandedRow(null);
    setPage(0);
    setAppliedFilterForm({ ...filterForm });
  }

  function resetFilters() {
    setExpandedRow(null);
    setPage(0);
    setFilterForm({ ...EMPTY_FILTER_FORM });
    setAppliedFilterForm({ ...EMPTY_FILTER_FORM });
  }

  function goToPage(newPage: number) {
    setExpandedRow(null);
    setPage(newPage);
  }

  function toggleRow(id: number) {
    setExpandedRow((prev) => (prev === id ? null : id));
  }

  function handleRowKeyDown(event: KeyboardEvent<HTMLTableRowElement>, id: number) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    toggleRow(id);
  }

  function truncateSql(sql: string, maxLen = 80) {
    return sql.length > maxLen ? sql.slice(0, maxLen) + '...' : sql;
  }

  return (
    <div className="audit-logs">
      <div className="page-header">
        <h1>{t('auditLogs.title')}</h1>
        <p className="page-subtitle">{t('auditLogs.subtitle')}</p>
      </div>

      {/* Filter bar */}
      <div className="filter-bar">
        <div className="filter-field">
          <label htmlFor="audit-database-filter">{t('auditLogs.databaseFilter')}</label>
          <select
            id="audit-database-filter"
            value={filterForm.database}
            onChange={(e) => setFilterForm((f) => ({ ...f, database: e.target.value }))}
            className="filter-select"
          >
            <option value="">{t('auditLogs.allDatabases')}</option>
            {databases.map((db) => (
              <option key={db.alias} value={db.alias}>
                {db.alias}
              </option>
            ))}
          </select>
        </div>
        <div className="filter-field">
          <label htmlFor="audit-user-filter">{t('auditLogs.userFilter')}</label>
          <input
            id="audit-user-filter"
            type="text"
            placeholder={t('auditLogs.user')}
            value={filterForm.user}
            onChange={(e) => setFilterForm((f) => ({ ...f, user: e.target.value }))}
            className="filter-input"
          />
        </div>
        <div className="filter-field">
          <label htmlFor="audit-from-date">{t('common.fromDate')}</label>
          <input
            id="audit-from-date"
            type="date"
            value={filterForm.from_date}
            onChange={(e) => setFilterForm((f) => ({ ...f, from_date: e.target.value }))}
            className="filter-input"
          />
        </div>
        <div className="filter-field">
          <label htmlFor="audit-to-date">{t('common.toDate')}</label>
          <input
            id="audit-to-date"
            type="date"
            value={filterForm.to_date}
            onChange={(e) => setFilterForm((f) => ({ ...f, to_date: e.target.value }))}
            className="filter-input"
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

      {logsQuery.isLoading && <div className="loading-message" role="status">{t('auditLogs.loadingLogs')}</div>}

      {logsQuery.isError && (
        <div className="error-banner" role="alert">{t('auditLogs.loadFailed')}</div>
      )}

      {logs.length > 0 && (
        <>
          <div className="table-container">
            <table className="data-table audit-table">
              <thead>
                <tr>
                  <th scope="col">{t('auditLogs.timestamp')}</th>
                  <th scope="col">{t('auditLogs.user')}</th>
                  <th scope="col">{t('connections.database')}</th>
                  <th scope="col">{t('auditLogs.sql')}</th>
                  <th scope="col">{t('auditLogs.rows')}</th>
                  <th scope="col">{t('auditLogs.elapsed')}</th>
                  <th scope="col">{t('common.status')}</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => {
                  const isExpanded = expandedRow === log.id;
                  const detailId = `audit-log-detail-${log.id}`;
                  return (
                    <Fragment key={log.id}>
                    <tr
                      className={`audit-row ${isExpanded ? 'audit-row--expanded' : ''}`}
                      role="button"
                      tabIndex={0}
                      aria-expanded={isExpanded}
                      aria-controls={detailId}
                      aria-label={t('auditLogs.toggleDetails', { id: log.id })}
                      onClick={() => toggleRow(log.id)}
                      onKeyDown={(event) => handleRowKeyDown(event, log.id)}
                    >
                      <td className="cell-timestamp">{formatKST(log.timestamp)}</td>
                      <td>{log.user}</td>
                      <td>{log.database_alias}</td>
                      <td className="cell-sql mono">{truncateSql(log.sql)}</td>
                      <td>{log.row_count}</td>
                      <td>{log.elapsed_ms}ms</td>
                      <td>
                        <span
                          className={`badge ${log.status === 'error' ? 'badge-error' : 'badge-ok'}`}
                        >
                          {log.status}
                        </span>
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr key={`${log.id}-detail`} id={detailId} className="audit-detail-row">
                        <td colSpan={7}>
                          <div className="audit-detail">
                            <div className="detail-section">
                              <h4>{t('auditLogs.fullSql')}</h4>
                              <pre className="detail-sql">{log.sql}</pre>
                            </div>
                            {log.params && (
                              <div className="detail-section">
                                <h4>{t('auditLogs.parameters')}</h4>
                                <pre className="detail-sql">
                                  {(() => {
                                    try {
                                      return JSON.stringify(JSON.parse(log.params), null, 2);
                                    } catch {
                                      return log.params;
                                    }
                                  })()}
                                </pre>
                              </div>
                            )}
                            {log.error_message && (
                              <div className="detail-section">
                                <h4>{t('common.error')}</h4>
                                <pre className="detail-error">{log.error_message}</pre>
                              </div>
                            )}
                            <div className="detail-meta">
                              <span>{t('auditLogs.rows')}: {log.row_count}</span>
                              <span>{t('auditLogs.elapsed')}: {log.elapsed_ms}ms</span>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  );
                })}
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

      {!logsQuery.isLoading && logs.length === 0 && !logsQuery.isError && (
        <div className="empty-state">
          <h3>{t('auditLogs.noLogs')}</h3>
          <p>{t('auditLogs.noLogsDesc')}</p>
        </div>
      )}
    </div>
  );
}

export default AuditLogs;
