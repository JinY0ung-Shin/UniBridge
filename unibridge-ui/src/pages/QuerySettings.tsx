import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { getQuerySettings, updateQuerySettings, type QuerySettings } from '../api/client';
import { useCanWrite } from '../components/useCanWrite';
import './QuerySettings.css';

function parseBlockedKeywords(value: string): string[] {
  return value
    .split(',')
    .map((k) => k.trim().toUpperCase())
    .filter((k) => k.length > 0);
}

/** Retention fields are absent on backends that predate them; 0 = keep forever. */
function retentionValue(value: number | undefined): number {
  return value ?? 0;
}

function settingsKey(settings: QuerySettings): string {
  return [
    settings.rate_limit_per_minute,
    settings.max_concurrent_queries,
    settings.default_row_limit,
    settings.query_route_timeout,
    settings.gateway_route_timeout,
    settings.blocked_sql_keywords.join('\0'),
    retentionValue(settings.audit_log_retention_days),
    retentionValue(settings.admin_audit_log_retention_days),
    retentionValue(settings.alert_history_retention_days),
  ].join(':');
}

function QuerySettingsForm({ settings }: { settings: QuerySettings }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const canWrite = useCanWrite('query.settings.write');
  const [rateLimit, setRateLimit] = useState(settings.rate_limit_per_minute);
  const [maxConcurrent, setMaxConcurrent] = useState(settings.max_concurrent_queries);
  const [defaultRowLimit, setDefaultRowLimit] = useState(settings.default_row_limit);
  const [queryRouteTimeout, setQueryRouteTimeout] = useState(settings.query_route_timeout);
  const [gatewayRouteTimeout, setGatewayRouteTimeout] = useState(settings.gateway_route_timeout);
  const [blockedKeywords, setBlockedKeywords] = useState(settings.blocked_sql_keywords.join(', '));
  const [auditRetention, setAuditRetention] = useState(
    retentionValue(settings.audit_log_retention_days),
  );
  const [adminAuditRetention, setAdminAuditRetention] = useState(
    retentionValue(settings.admin_audit_log_retention_days),
  );
  const [alertHistoryRetention, setAlertHistoryRetention] = useState(
    retentionValue(settings.alert_history_retention_days),
  );

  const draftSettings: QuerySettings = {
    rate_limit_per_minute: rateLimit,
    max_concurrent_queries: maxConcurrent,
    default_row_limit: defaultRowLimit,
    query_route_timeout: queryRouteTimeout,
    gateway_route_timeout: gatewayRouteTimeout,
    blocked_sql_keywords: parseBlockedKeywords(blockedKeywords),
    audit_log_retention_days: auditRetention,
    admin_audit_log_retention_days: adminAuditRetention,
    alert_history_retention_days: alertHistoryRetention,
  };
  const hasChanges = settingsKey(draftSettings) !== settingsKey(settings);

  function applySettings(next: QuerySettings) {
    setRateLimit(next.rate_limit_per_minute);
    setMaxConcurrent(next.max_concurrent_queries);
    setDefaultRowLimit(next.default_row_limit);
    setQueryRouteTimeout(next.query_route_timeout);
    setGatewayRouteTimeout(next.gateway_route_timeout);
    setBlockedKeywords(next.blocked_sql_keywords.join(', '));
    setAuditRetention(retentionValue(next.audit_log_retention_days));
    setAdminAuditRetention(retentionValue(next.admin_audit_log_retention_days));
    setAlertHistoryRetention(retentionValue(next.alert_history_retention_days));
  }

  const updateMut = useMutation({
    mutationFn: updateQuerySettings,
    onSuccess: (updated) => {
      queryClient.setQueryData(['query-settings'], updated);
      applySettings(updated);
    },
  });

  function handleSave() {
    if (!hasChanges || !canWrite) return;
    updateMut.mutate(draftSettings);
  }

  function handleDiscard() {
    applySettings(settings);
    updateMut.reset();
  }

  return (
    <div className="settings-form">
      {!canWrite && (
        <div className="settings-readonly-notice" role="status">
          {t('querySettings.readOnlyNotice')}
        </div>
      )}

      <div className="settings-card">
        <h3>{t('querySettings.rateLimiting')}</h3>
        <div className="form-group">
          <label htmlFor="query-rate-limit">{t('querySettings.rateLimit')}</label>
          <input
            id="query-rate-limit"
            type="number"
            min={1}
            max={1000}
            value={rateLimit}
            aria-label={t('querySettings.rateLimit')}
            aria-describedby="query-rate-limit-hint"
            onChange={(e) => setRateLimit(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="query-rate-limit-hint" className="form-hint">{t('querySettings.rateLimitHint')}</span>
        </div>
        <div className="form-group">
          <label htmlFor="query-max-concurrent">{t('querySettings.maxConcurrent')}</label>
          <input
            id="query-max-concurrent"
            type="number"
            min={1}
            max={100}
            value={maxConcurrent}
            aria-label={t('querySettings.maxConcurrent')}
            aria-describedby="query-max-concurrent-hint"
            onChange={(e) => setMaxConcurrent(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="query-max-concurrent-hint" className="form-hint">{t('querySettings.maxConcurrentHint')}</span>
        </div>
        <div className="form-group">
          <label htmlFor="query-default-row-limit">{t('querySettings.defaultRowLimit')}</label>
          <input
            id="query-default-row-limit"
            type="number"
            min={1}
            max={1000000}
            value={defaultRowLimit}
            aria-label={t('querySettings.defaultRowLimit')}
            aria-describedby="query-default-row-limit-hint"
            onChange={(e) => setDefaultRowLimit(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="query-default-row-limit-hint" className="form-hint">{t('querySettings.defaultRowLimitHint')}</span>
        </div>
        <div className="form-group">
          <label htmlFor="query-route-timeout">{t('querySettings.queryRouteTimeout')}</label>
          <input
            id="query-route-timeout"
            type="number"
            min={1}
            max={3600}
            value={queryRouteTimeout}
            aria-label={t('querySettings.queryRouteTimeout')}
            aria-describedby="query-route-timeout-hint"
            onChange={(e) => setQueryRouteTimeout(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="query-route-timeout-hint" className="form-hint">{t('querySettings.queryRouteTimeoutHint')}</span>
        </div>
      </div>

      <div className="settings-card">
        <h3>{t('querySettings.gatewaySection')}</h3>
        <div className="form-group">
          <label htmlFor="gateway-route-timeout">{t('querySettings.gatewayRouteTimeout')}</label>
          <input
            id="gateway-route-timeout"
            type="number"
            min={1}
            max={3600}
            value={gatewayRouteTimeout}
            aria-label={t('querySettings.gatewayRouteTimeout')}
            aria-describedby="gateway-route-timeout-hint"
            onChange={(e) => setGatewayRouteTimeout(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="gateway-route-timeout-hint" className="form-hint">{t('querySettings.gatewayRouteTimeoutHint')}</span>
        </div>
      </div>

      <div className="settings-card">
        <h3>{t('querySettings.sqlBlacklist')}</h3>
        <div className="form-group">
          <label htmlFor="blocked-sql-keywords">{t('querySettings.blockedKeywords')}</label>
          <input
            id="blocked-sql-keywords"
            type="text"
            value={blockedKeywords}
            onChange={(e) => setBlockedKeywords(e.target.value)}
            disabled={!canWrite}
            placeholder="VACUUM, ANALYZE, ..."
            aria-label={t('querySettings.blockedKeywords')}
            aria-describedby="blocked-sql-keywords-hint"
          />
          <span id="blocked-sql-keywords-hint" className="form-hint">{t('querySettings.blockedKeywordsHint')}</span>
        </div>
      </div>

      <div className="settings-card">
        <h3>{t('querySettings.retentionSection')}</h3>
        <div className="form-group">
          <label htmlFor="audit-log-retention">{t('querySettings.auditLogRetention')}</label>
          <input
            id="audit-log-retention"
            type="number"
            min={0}
            max={3650}
            value={auditRetention}
            aria-label={t('querySettings.auditLogRetention')}
            aria-describedby="audit-log-retention-hint"
            onChange={(e) => setAuditRetention(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="audit-log-retention-hint" className="form-hint">{t('querySettings.auditLogRetentionHint')}</span>
        </div>
        <div className="form-group">
          <label htmlFor="admin-audit-log-retention">{t('querySettings.adminAuditLogRetention')}</label>
          <input
            id="admin-audit-log-retention"
            type="number"
            min={0}
            max={3650}
            value={adminAuditRetention}
            aria-label={t('querySettings.adminAuditLogRetention')}
            aria-describedby="admin-audit-log-retention-hint"
            onChange={(e) => setAdminAuditRetention(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="admin-audit-log-retention-hint" className="form-hint">{t('querySettings.adminAuditLogRetentionHint')}</span>
        </div>
        <div className="form-group">
          <label htmlFor="alert-history-retention">{t('querySettings.alertHistoryRetention')}</label>
          <input
            id="alert-history-retention"
            type="number"
            min={0}
            max={3650}
            value={alertHistoryRetention}
            aria-label={t('querySettings.alertHistoryRetention')}
            aria-describedby="alert-history-retention-hint"
            onChange={(e) => setAlertHistoryRetention(Number(e.target.value))}
            disabled={!canWrite}
          />
          <span id="alert-history-retention-hint" className="form-hint">{t('querySettings.alertHistoryRetentionHint')}</span>
        </div>
      </div>

      {canWrite && (
      <div className="settings-save-bar">
        <span className={hasChanges ? 'settings-save-status settings-save-status--changed' : 'settings-save-status'}>
          {hasChanges ? t('querySettings.unsavedChanges') : t('querySettings.noChanges')}
        </span>
        <div className="settings-save-actions">
          <button
            className="btn btn-secondary"
            type="button"
            onClick={handleDiscard}
            disabled={!hasChanges || updateMut.isPending}
          >
            {t('querySettings.discardChanges')}
          </button>
          <button
            className="btn btn-primary"
            type="button"
            onClick={handleSave}
            disabled={!hasChanges || updateMut.isPending}
            aria-busy={updateMut.isPending}
          >
            {updateMut.isPending ? t('common.saving') : t('common.save')}
          </button>
        </div>
      </div>
      )}

      {updateMut.isSuccess && !hasChanges && (
        <span className="save-success" role="status">{t('querySettings.saved')}</span>
      )}
      {updateMut.isError && (
        <span className="save-error" role="alert">{t('querySettings.saveFailed')}</span>
      )}
    </div>
  );
}

function QuerySettingsPage() {
  const { t } = useTranslation();

  const settingsQuery = useQuery({
    queryKey: ['query-settings'],
    queryFn: getQuerySettings,
  });

  return (
    <div className="query-settings">
      <div className="page-header">
        <h1>{t('querySettings.title')}</h1>
        <p className="page-subtitle">{t('querySettings.subtitle')}</p>
      </div>

      {settingsQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}

      {settingsQuery.isError && (
        <div className="error-banner" role="alert">{t('querySettings.loadFailed')}</div>
      )}

      {settingsQuery.data && <QuerySettingsForm settings={settingsQuery.data} />}
    </div>
  );
}

export default QuerySettingsPage;
