import { Fragment, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getAdminAuditLogs, type AdminAuditLogParams } from '../api/client';
import { formatKST, kstDateToUtcIso } from '../utils/time';
import './AuditLogs.css';
import './AdminAuditLogs.css';

const PAGE_SIZE = 20;

const RESOURCE_TYPES = [
  'route',
  'upstream',
  'api_key',
  'db_connection',
  'permission',
  'query_template',
  'system_settings',
  's3_connection',
  'nas_connection',
  'alert_settings',
  'alert_channel',
  'resource_owner',
  'role',
  'user',
  'user_role',
] as const;
const ACTIONS = ['create', 'update', 'delete'] as const;

function prettyJson(value: string): string {
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function AdminAuditLogs() {
  const { t } = useTranslation();
  const [page, setPage] = useState(0);
  const [expandedRow, setExpandedRow] = useState<number | null>(null);

  // form state (separate from applied filters so we don't refetch on every keystroke)
  const [filterForm, setFilterForm] = useState({
    actor: '',
    resource_type: '',
    action: '',
    from_date: '',
    to_date: '',
  });

  const [appliedFilterForm, setAppliedFilterForm] = useState({
    actor: '',
    resource_type: '',
    action: '',
    from_date: '',
    to_date: '',
  });

  const filters: AdminAuditLogParams = {
    actor: appliedFilterForm.actor || undefined,
    resource_type: appliedFilterForm.resource_type || undefined,
    action: appliedFilterForm.action || undefined,
    from_date: kstDateToUtcIso(appliedFilterForm.from_date, 'start'),
    to_date: kstDateToUtcIso(appliedFilterForm.to_date, 'end'),
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  };

  const logsQuery = useQuery({
    queryKey: ['admin-audit-logs', filters],
    queryFn: () => getAdminAuditLogs(filters),
  });

  const logs = logsQuery.data ?? [];
  const hasMore = logs.length === PAGE_SIZE;

  function applyFilters() {
    setExpandedRow(null);
    setPage(0);
    setAppliedFilterForm({ ...filterForm });
  }

  function goToPage(newPage: number) {
    setExpandedRow(null);
    setPage(newPage);
  }

  function toggleRow(id: number) {
    setExpandedRow((prev) => (prev === id ? null : id));
  }

  return (
    <div className="audit-logs">
      <div className="page-header">
        <h1>{t('adminAuditLogs.title')}</h1>
        <p className="page-subtitle">{t('adminAuditLogs.subtitle')}</p>
      </div>

      {/* Filter bar */}
      <div className="filter-bar">
        <input
          type="text"
          placeholder={t('adminAuditLogs.actor')}
          value={filterForm.actor}
          onChange={(e) => setFilterForm((f) => ({ ...f, actor: e.target.value }))}
          className="filter-input"
        />
        <select
          value={filterForm.resource_type}
          onChange={(e) => setFilterForm((f) => ({ ...f, resource_type: e.target.value }))}
          className="filter-select"
        >
          <option value="">{t('adminAuditLogs.allResourceTypes')}</option>
          {RESOURCE_TYPES.map((rt) => (
            <option key={rt} value={rt}>
              {rt}
            </option>
          ))}
        </select>
        <select
          value={filterForm.action}
          onChange={(e) => setFilterForm((f) => ({ ...f, action: e.target.value }))}
          className="filter-select"
        >
          <option value="">{t('adminAuditLogs.allActions')}</option>
          {ACTIONS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <input
          type="date"
          value={filterForm.from_date}
          onChange={(e) => setFilterForm((f) => ({ ...f, from_date: e.target.value }))}
          className="filter-input"
        />
        <input
          type="date"
          value={filterForm.to_date}
          onChange={(e) => setFilterForm((f) => ({ ...f, to_date: e.target.value }))}
          className="filter-input"
        />
        <button className="btn btn-primary" onClick={applyFilters}>
          {t('common.search')}
        </button>
      </div>

      {logsQuery.isLoading && (
        <div className="loading-message">{t('adminAuditLogs.loadingLogs')}</div>
      )}

      {logsQuery.isError && (
        <div className="error-banner">{t('adminAuditLogs.loadFailed')}</div>
      )}

      {logs.length > 0 && (
        <>
          <div className="table-container">
            <table className="data-table audit-table">
              <thead>
                <tr>
                  <th>{t('adminAuditLogs.timestamp')}</th>
                  <th>{t('adminAuditLogs.actor')}</th>
                  <th>{t('adminAuditLogs.action')}</th>
                  <th>{t('adminAuditLogs.resourceType')}</th>
                  <th>{t('adminAuditLogs.resourceId')}</th>
                  <th>{t('adminAuditLogs.summary')}</th>
                  <th>{t('common.status')}</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => (
                  <Fragment key={log.id}>
                    <tr
                      className={`audit-row ${expandedRow === log.id ? 'audit-row--expanded' : ''}`}
                      onClick={() => toggleRow(log.id)}
                    >
                      <td className="cell-timestamp">{formatKST(log.timestamp)}</td>
                      <td>{log.actor}</td>
                      <td>
                        <span className={`badge badge-action-${log.action}`}>{log.action}</span>
                      </td>
                      <td>{log.resource_type}</td>
                      <td className="mono">{log.resource_id}</td>
                      <td className="cell-summary">{log.summary ?? '—'}</td>
                      <td>
                        <span
                          className={`badge ${log.status === 'error' ? 'badge-error' : 'badge-ok'}`}
                        >
                          {log.status}
                        </span>
                      </td>
                    </tr>
                    {expandedRow === log.id && (
                      <tr key={`${log.id}-detail`} className="audit-detail-row">
                        <td colSpan={7}>
                          <div className="audit-detail">
                            <div className="audit-diff">
                              <div className="detail-section">
                                <h4>{t('adminAuditLogs.before')}</h4>
                                <pre className="detail-sql">
                                  {log.before != null ? prettyJson(log.before) : '—'}
                                </pre>
                              </div>
                              <div className="detail-section">
                                <h4>{t('adminAuditLogs.after')}</h4>
                                <pre className="detail-sql">
                                  {log.after != null ? prettyJson(log.after) : '—'}
                                </pre>
                              </div>
                            </div>
                            {log.error_message && (
                              <div className="detail-section">
                                <h4>{t('adminAuditLogs.errorMessage')}</h4>
                                <pre className="detail-error">{log.error_message}</pre>
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
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
            <span className="page-info">{t('common.page', { page: page + 1 })}</span>
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

      {!logsQuery.isLoading && logs.length === 0 && !logsQuery.isError && (
        <div className="empty-state">
          <h3>{t('adminAuditLogs.noLogs')}</h3>
          <p>{t('adminAuditLogs.noLogsDesc')}</p>
        </div>
      )}
    </div>
  );
}

export default AdminAuditLogs;
