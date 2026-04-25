import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer,
} from 'recharts';
import { getHealth, getAdminDatabases, getMetricsSummary, getMetricsRequests, getLlmSummary, getLlmTokens, type DatabaseHealth } from '../api/client';
import { usePermissions } from '../components/usePermissions';
import { useTheme } from '../components/useTheme';
import './Dashboard.css';

interface DashboardDbEntry {
  alias: string;
  status: 'connected' | 'error';
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function getCssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function Dashboard() {
  const { t } = useTranslation();
  const { permissions } = usePermissions();
  // Subscribe so chart CSS variables are reread after theme changes.
  useTheme();
  const canViewMonitoring = permissions.includes('gateway.monitoring.read');

  const chartColors = {
    grid: getCssVar('--chart-grid'),
    axis: getCssVar('--chart-axis'),
    tooltipBg: getCssVar('--chart-tooltip-bg'),
    tooltipBorder: getCssVar('--chart-tooltip-border'),
    blue: getCssVar('--accent-blue'),
    textSecondary: getCssVar('--text-secondary'),
  };

  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 15_000,
  });

  const dbsQuery = useQuery({
    queryKey: ['admin-databases'],
    queryFn: getAdminDatabases,
  });

  const gwSummaryQuery = useQuery({
    queryKey: ['dashboard-gw-summary'],
    queryFn: () => getMetricsSummary('1h'),
    refetchInterval: 30_000,
    enabled: canViewMonitoring,
  });

  const gwRequestsQuery = useQuery({
    queryKey: ['dashboard-gw-requests'],
    queryFn: () => getMetricsRequests('1h'),
    refetchInterval: 30_000,
    enabled: canViewMonitoring,
  });

  const llmSummaryQuery = useQuery({
    queryKey: ['dashboard-llm-summary'],
    queryFn: () => getLlmSummary('1h'),
    refetchInterval: 30_000,
    enabled: canViewMonitoring,
  });

  const llmTokensQuery = useQuery({
    queryKey: ['dashboard-llm-tokens'],
    queryFn: () => getLlmTokens('1h'),
    refetchInterval: 30_000,
    enabled: canViewMonitoring,
  });

  const healthData = healthQuery.data;
  const databases = dbsQuery.data ?? [];

  const dbHealthMap: Record<string, DatabaseHealth> = healthData?.databases ?? {};
  const healthEntries: DashboardDbEntry[] = Object.entries(dbHealthMap).map(([alias, h]) => ({
    alias,
    status: h.status === 'ok' ? 'connected' : 'error',
  }));
  const totalDbs = databases.length || healthEntries.length;
  const connectedCount = healthEntries.filter((h) => h.status === 'connected').length;
  const errorCount = healthEntries.filter((h) => h.status === 'error').length;

  const isLoading = healthQuery.isLoading || dbsQuery.isLoading;
  const isError = healthQuery.isError || dbsQuery.isError;

  return (
    <div className="dashboard">
      <div className="page-header">
        <h1>{t('dashboard.title')}</h1>
        <p className="page-subtitle">{t('dashboard.subtitle')}</p>
      </div>

      {/* Summary cards */}
      <div className="summary-cards">
        <div className="summary-card">
          <div className="summary-card__value">{totalDbs}</div>
          <div className="summary-card__label">{t('dashboard.totalDatabases')}</div>
        </div>
        <div className="summary-card">
          <div className="summary-card__value" style={{ color: 'var(--accent-green)' }}>{connectedCount}</div>
          <div className="summary-card__label">{t('dashboard.connected')}</div>
        </div>
        <div className="summary-card">
          <div className="summary-card__value" style={{ color: 'var(--accent-red)' }}>{errorCount}</div>
          <div className="summary-card__label">{t('dashboard.errors')}</div>
        </div>
      </div>

      {/* Status */}
      {isLoading && <div className="loading-message">{t('dashboard.loadingHealth')}</div>}
      {isError && (
        <div className="error-banner">
          {t('dashboard.loadFailed')}
        </div>
      )}

      {/* DB health grid */}
      {healthEntries.length > 0 && (
        <>
          <h2 className="section-title">{t('dashboard.databaseStatus')}</h2>
          <div className="db-grid">
            {healthEntries.map((db) => (
              <div key={db.alias} className={`db-card ${db.status === 'error' ? 'db-card--error' : ''}`}>
                <div className="db-card__header">
                  <span className={`status-dot ${db.status === 'connected' ? 'status-dot--green' : 'status-dot--red'}`} />
                  <span className="db-card__alias">{db.alias}</span>
                </div>
                <div className="db-card__body">
                  {db.status === 'connected' ? (
                    <div className="db-card__connected">{t('dashboard.connectionSuccess')}</div>
                  ) : (
                    <div className="db-card__error">{t('dashboard.connectionFailed')}</div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {/* Gateway Monitoring */}
      {canViewMonitoring && (
        <>
          <div className="section-header">
            <h2 className="section-title">{t('dashboard.gatewayMonitoring')}</h2>
            <Link to="/gateway/monitoring" className="section-link">{t('dashboard.viewDetails')}</Link>
          </div>
          {gwSummaryQuery.isLoading && (
            <div className="loading-message">{t('gatewayMonitoring.loadingMetrics')}</div>
          )}
          {gwSummaryQuery.isError && (
            <div className="no-data">{t('dashboard.monitoringNoData')}</div>
          )}
          {gwSummaryQuery.data && (
            <>
              <div className="summary-cards">
                <div className="summary-card">
                  <div className="summary-card__value">{gwSummaryQuery.data.total_requests.toLocaleString()}</div>
                  <div className="summary-card__label">{t('gatewayMonitoring.totalRequests', { range: '1h' })}</div>
                </div>
                <div className="summary-card">
                  <div className="summary-card__value" style={{ color: gwSummaryQuery.data.error_rate > 5 ? 'var(--accent-red)' : 'var(--accent-green)' }}>
                    {gwSummaryQuery.data.error_rate}%
                  </div>
                  <div className="summary-card__label">{t('gatewayMonitoring.errorRate')}</div>
                </div>
                <div className="summary-card">
                  <div className="summary-card__value">{gwSummaryQuery.data.avg_latency_ms}ms</div>
                  <div className="summary-card__label">{t('gatewayMonitoring.avgLatency')}</div>
                </div>
              </div>
              {(gwRequestsQuery.data ?? []).length > 0 && (
                <div className="dashboard-mini-chart">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={(gwRequestsQuery.data ?? []).map((p) => ({ time: formatTime(p.timestamp), rps: p.value }))}>
                      <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                      <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                      <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                      <Tooltip
                        contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                        labelStyle={{ color: chartColors.axis }}
                        itemStyle={{ color: chartColors.textSecondary }}
                      />
                      <Line type="monotone" dataKey="rps" stroke={chartColors.blue} strokeWidth={2} dot={false} name="req/s" />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}
            </>
          )}
        </>
      )}

      {/* LLM Token Usage */}
      {canViewMonitoring && (
        <>
          <div className="section-header">
            <h2 className="section-title">{t('dashboard.llmMonitoring')}</h2>
            <Link to="/llm/monitoring" className="section-link">{t('dashboard.viewDetails')}</Link>
          </div>
          {llmSummaryQuery.isLoading && (
            <div className="loading-message">{t('llmMonitoring.loadingMetrics')}</div>
          )}
          {llmSummaryQuery.isError && (
            <div className="no-data">{t('dashboard.llmNoData')}</div>
          )}
          {llmSummaryQuery.data && (
            <>
              <div className="summary-cards">
                <div className="summary-card">
                  <div className="summary-card__value">
                    {llmSummaryQuery.data.total_tokens >= 1000
                      ? `${(llmSummaryQuery.data.total_tokens / 1000).toFixed(1)}K`
                      : llmSummaryQuery.data.total_tokens.toLocaleString()}
                  </div>
                  <div className="summary-card__label">{t('llmMonitoring.totalTokens', { range: '1h' })}</div>
                </div>
                <div className="summary-card">
                  <div className="summary-card__value">${llmSummaryQuery.data.estimated_cost.toFixed(2)}</div>
                  <div className="summary-card__label">{t('llmMonitoring.estimatedCost')}</div>
                </div>
                <div className="summary-card">
                  <div className="summary-card__value">{llmSummaryQuery.data.total_requests.toLocaleString()}</div>
                  <div className="summary-card__label">{t('llmMonitoring.totalRequests', { range: '1h' })}</div>
                </div>
              </div>
              {(llmTokensQuery.data?.prompt ?? []).length > 0 && (
                <div className="dashboard-mini-chart">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={(llmTokensQuery.data?.prompt ?? []).map((p, i) => ({
                      time: formatTime(p.timestamp),
                      prompt: Math.round(p.value),
                      completion: Math.round(llmTokensQuery.data?.completion?.[i]?.value ?? 0),
                    }))}>
                      <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                      <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                      <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                      <Tooltip
                        contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                        labelStyle={{ color: chartColors.axis }}
                        itemStyle={{ color: chartColors.textSecondary }}
                      />
                      <Line type="monotone" dataKey="prompt" stroke={chartColors.blue} strokeWidth={2} dot={false} name="Prompt" />
                      <Line type="monotone" dataKey="completion" stroke={getCssVar('--accent-green')} strokeWidth={2} dot={false} name="Completion" />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}
            </>
          )}
        </>
      )}

      {/* Empty state */}
      {!isLoading && healthEntries.length === 0 && !isError && (
        <div className="empty-state">
          <h3>{t('dashboard.noDatabases')}</h3>
          <p>{t('dashboard.noDatabasesDesc')}</p>
        </div>
      )}
    </div>
  );
}

export default Dashboard;
