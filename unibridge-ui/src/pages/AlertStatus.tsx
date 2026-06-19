import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getAlertStatus, type AlertStatus as AlertStatusEntry } from '../api/client';
import { formatKST } from '../utils/time';
import './AlertStatus.css';

type RuleType = 'db_health' | 'nas_health' | 'upstream_health' | 'error_rate' | 'route_error_rate';

function typeLabel(t: (k: string) => string, type: string): string {
  const map: Record<string, string> = {
    db_health: t('alerts.typeDbHealth'),
    nas_health: t('alerts.typeNasHealth'),
    upstream_health: t('alerts.typeUpstreamHealth'),
    error_rate: t('alerts.typeErrorRate'),
    route_error_rate: t('alerts.typeRouteErrorRate'),
    server_down: t('alerts.typeServerDown'),
    server_disk: t('alerts.typeServerDisk'),
    server_disk_forecast: t('alerts.typeServerDiskForecast'),
    server_cpu: t('alerts.typeServerCpu'),
    server_mem: t('alerts.typeServerMem'),
  };
  return map[type] ?? type;
}

function severityLabel(t: (k: string) => string, severity: string | null): string {
  if (severity === 'critical') return t('alerts.severityCritical');
  if (severity === 'warning') return t('alerts.severityWarning');
  return '';
}

function formatDuration(ts: string | null): string {
  if (!ts) return '';
  const start = new Date(ts).getTime();
  if (Number.isNaN(start)) return '';
  const diffMs = Date.now() - start;
  if (diffMs < 0) return '';
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ${min % 60}m`;
  const day = Math.floor(hr / 24);
  return `${day}d ${hr % 24}h`;
}

function AlertStatus() {
  const { t } = useTranslation();
  const statusQuery = useQuery({
    queryKey: ['alert-status'],
    queryFn: getAlertStatus,
    refetchInterval: 15_000,
  });

  const entries: AlertStatusEntry[] = statusQuery.data ?? [];
  const alerting = entries.filter((e) => e.status === 'alert');
  const healthy = entries.filter((e) => e.status === 'ok');

  return (
    <div className="alert-status">
      <div className="page-header">
        <div>
          <h1>{t('alerts.statusTitle')}</h1>
          <p className="page-subtitle">{t('alerts.statusSubtitle')}</p>
        </div>
        <button
          className="btn btn-secondary"
          onClick={() => statusQuery.refetch()}
          disabled={statusQuery.isFetching}
        >
          {statusQuery.isFetching ? t('common.loading') : t('common.refresh')}
        </button>
      </div>

      {statusQuery.isLoading && (
        <div className="loading-message">{t('common.loading')}</div>
      )}

      {statusQuery.isError && (
        <div className="error-banner">{t('common.errorOccurred')}</div>
      )}

      {!statusQuery.isLoading && !statusQuery.isError && (
        <>
          <div className="status-summary">
            <div className="status-summary-card status-summary-card--alert">
              <div className="status-summary-count">{alerting.length}</div>
              <div className="status-summary-label">{t('alerts.statusAlerting')}</div>
            </div>
            <div className="status-summary-card status-summary-card--ok">
              <div className="status-summary-count">{healthy.length}</div>
              <div className="status-summary-label">{t('alerts.statusHealthy')}</div>
            </div>
          </div>

          {/* Alerting */}
          <section className="status-section">
            <h2 className="status-section-title">
              <span className="status-dot status-dot--alert" />
              {t('alerts.statusAlerting')} ({alerting.length})
            </h2>
            {alerting.length === 0 ? (
              <div className="empty-state empty-state--small">
                <p>{t('alerts.statusNoneAlerting')}</p>
              </div>
            ) : (
              <div className="table-container">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t('alerts.ruleType')}</th>
                      <th>{t('alerts.target')}</th>
                      <th>{t('alerts.statusSince')}</th>
                      <th>{t('alerts.statusDuration')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {alerting.map((e, i) => (
                      <tr key={`${e.type}:${e.target}:${i}`}>
                        <td>
                          <span
                            className={`rule-type-badge rule-type-badge--${e.type as RuleType}`}
                          >
                            {typeLabel(t, e.type)}
                          </span>
                          {e.severity && (
                            <span
                              style={{
                                marginLeft: 6,
                                padding: '1px 8px',
                                borderRadius: 999,
                                fontSize: '0.72rem',
                                fontWeight: 600,
                                color: '#fff',
                                background: e.severity === 'critical' ? 'var(--accent-red, #d23b3b)' : 'var(--accent-yellow, #d29a3b)',
                              }}
                            >
                              {severityLabel(t, e.severity)}
                            </span>
                          )}
                        </td>
                        <td className="cell-target">{e.target || '*'}</td>
                        <td className="cell-timestamp">{formatKST(e.since)}</td>
                        <td className="cell-duration">{formatDuration(e.since)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Healthy */}
          <section className="status-section">
            <h2 className="status-section-title">
              <span className="status-dot status-dot--ok" />
              {t('alerts.statusHealthy')} ({healthy.length})
            </h2>
            {healthy.length === 0 ? (
              <div className="empty-state empty-state--small">
                <p>{t('alerts.statusNoneHealthy')}</p>
              </div>
            ) : (
              <div className="table-container">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{t('alerts.ruleType')}</th>
                      <th>{t('alerts.target')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {healthy.map((e, i) => (
                      <tr key={`${e.type}:${e.target}:${i}`}>
                        <td>
                          <span
                            className={`rule-type-badge rule-type-badge--${e.type as RuleType}`}
                          >
                            {typeLabel(t, e.type)}
                          </span>
                        </td>
                        <td className="cell-target">{e.target || '*'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

export default AlertStatus;
