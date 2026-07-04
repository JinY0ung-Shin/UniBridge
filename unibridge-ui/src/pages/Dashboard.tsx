import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import { getHealth, getAdminDatabases, getMetricsSummary, getMetricsRequests, getMetricsRequestsTotal, getLlmSummary, getLlmTokens, type DatabaseHealth } from '../api/client';
import { usePermissions } from '../components/usePermissions';
import { useChartTheme } from '../components/useChartTheme';
import UniBridgeLogo from '../components/UniBridgeLogo';
import BucketSelector from '../components/BucketSelector';
import { type Bucket, type TimeSelection, bucketKey } from '../utils/timeRange';
import { formatBucketLabel, formatChartTime } from '../utils/time';
import { errorRateColor } from '../utils/monitoring';
import './Dashboard.css';

const BUCKET_RANGE: Record<Exclude<Bucket, 'auto'>, string> = { hour: '24h', day: '30d', week: '60d' };
const dashSel = (b: Bucket): TimeSelection => ({ kind: 'preset', value: b === 'auto' ? '1h' : BUCKET_RANGE[b] });
/** Range label matching what dashSel actually queries, for the summary cards. */
const dashRange = (b: Bucket): string => (b === 'auto' ? '1h' : BUCKET_RANGE[b]);

interface DashboardDbEntry {
  alias: string;
  status: 'connected' | 'error';
}

function MiniChartState({ children }: { children: React.ReactNode }) {
  return (
    <div className="dashboard-mini-chart__state" role="status" aria-live="polite">
      {children}
    </div>
  );
}

function Dashboard() {
  const { t } = useTranslation();
  const { permissions } = usePermissions();
  const chartColors = useChartTheme();
  const canViewMonitoring = permissions.includes('gateway.monitoring.read');
  const [gwBucket, setGwBucket] = useState<Bucket>('auto');
  const [llmBucket, setLlmBucket] = useState<Bucket>('auto');

  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 15_000,
  });

  const dbsQuery = useQuery({
    queryKey: ['admin-databases'],
    queryFn: getAdminDatabases,
  });

  // Summary cards follow the chart's bucket-derived span so cards and chart
  // always describe the same window.
  const gwSummaryQuery = useQuery({
    queryKey: ['dashboard-gw-summary', bucketKey(gwBucket)],
    queryFn: () => getMetricsSummary(dashSel(gwBucket)),
    refetchInterval: 30_000,
    enabled: canViewMonitoring,
  });

  const gwRequestsQuery = useQuery({
    queryKey: ['dashboard-gw-requests'],
    queryFn: () => getMetricsRequests(),
    refetchInterval: 30_000,
    enabled: canViewMonitoring && gwBucket === 'auto',
  });

  const gwVolumeQuery = useQuery({
    queryKey: ['dashboard-gw-volume', bucketKey(gwBucket)],
    queryFn: () => getMetricsRequestsTotal(dashSel(gwBucket), undefined, undefined, gwBucket),
    refetchInterval: 30_000,
    enabled: canViewMonitoring && gwBucket !== 'auto',
  });

  const llmSummaryQuery = useQuery({
    queryKey: ['dashboard-llm-summary', bucketKey(llmBucket)],
    queryFn: () => getLlmSummary(dashSel(llmBucket)),
    refetchInterval: 30_000,
    enabled: canViewMonitoring,
  });

  const llmTokensQuery = useQuery({
    queryKey: ['dashboard-llm-tokens', bucketKey(llmBucket)],
    queryFn: () => getLlmTokens(dashSel(llmBucket), llmBucket),
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
  const gwTrendData = (gwRequestsQuery.data ?? []).map((p) => ({
    time: formatChartTime(p.timestamp),
    rps: p.value,
  }));
  const gwVolumeData = gwBucket === 'auto'
    ? []
    : (gwVolumeQuery.data ?? []).map((p) => ({
        time: formatBucketLabel(p.timestamp, gwBucket),
        requests: Math.round(p.value),
      }));
  const gwChartLoading = gwBucket === 'auto' ? gwRequestsQuery.isLoading : gwVolumeQuery.isLoading;
  const gwChartError = gwBucket === 'auto' ? gwRequestsQuery.isError : gwVolumeQuery.isError;
  const gwChartHasData = gwBucket === 'auto' ? gwTrendData.length > 0 : gwVolumeData.length > 0;
  const llmTokenChartData = (llmTokensQuery.data?.prompt ?? []).map((p, i) => ({
    time: llmBucket === 'auto' ? formatChartTime(p.timestamp) : formatBucketLabel(p.timestamp, llmBucket),
    prompt: Math.round(p.value),
    completion: Math.round(llmTokensQuery.data?.completion?.[i]?.value ?? 0),
  }));
  const llmChartHasData = llmTokenChartData.length > 0;

  return (
    <div className="dashboard">
      <div className="page-header">
        <UniBridgeLogo className="dashboard-brand-mark" />
        <div className="dashboard-header-copy">
          <h1>{t('dashboard.title')}</h1>
          <p className="page-subtitle">{t('dashboard.subtitle')}</p>
        </div>
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
      {isLoading && <div className="loading-message" role="status">{t('dashboard.loadingHealth')}</div>}
      {isError && (
        <div className="error-banner" role="alert">
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
            <div className="section-header__actions">
              <BucketSelector value={gwBucket} onChange={setGwBucket} />
              <Link to="/gateway/monitoring" className="section-link">{t('dashboard.viewDetails')}</Link>
            </div>
          </div>
          {gwSummaryQuery.isLoading && (
            <div className="loading-message" role="status">{t('gatewayMonitoring.loadingMetrics')}</div>
          )}
          {gwSummaryQuery.isError && (
            <div className="no-data">{t('dashboard.monitoringNoData')}</div>
          )}
          {gwSummaryQuery.data && (
            <>
              <div className="summary-cards">
                <div className="summary-card">
                  <div className="summary-card__value">{gwSummaryQuery.data.total_requests.toLocaleString()}</div>
                  <div className="summary-card__label">{t('gatewayMonitoring.totalRequests', { range: dashRange(gwBucket) })}</div>
                </div>
                <div className="summary-card">
                  <div className="summary-card__value" style={{ color: errorRateColor(gwSummaryQuery.data.error_rate) }}>
                    {gwSummaryQuery.data.error_rate}%
                  </div>
                  <div className="summary-card__label">{t('gatewayMonitoring.errorRate')}</div>
                </div>
                <div className="summary-card">
                  <div className="summary-card__value">{gwSummaryQuery.data.avg_latency_ms}ms</div>
                  <div className="summary-card__label">{t('gatewayMonitoring.avgLatency')}</div>
                </div>
              </div>
              <div className={`dashboard-mini-chart ${gwChartHasData ? '' : 'dashboard-mini-chart--empty'}`}>
                {gwChartLoading ? (
                  <MiniChartState>{t('gatewayMonitoring.loadingMetrics')}</MiniChartState>
                ) : gwChartError ? (
                  <MiniChartState>{t('dashboard.monitoringNoData')}</MiniChartState>
                ) : gwBucket === 'auto' ? (
                  gwTrendData.length > 0 ? (
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={gwTrendData}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                        <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <Tooltip
                          contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                          labelStyle={{ color: chartColors.axis }}
                          itemStyle={{ color: chartColors.textSecondary }}
                        />
                        <Line type="monotone" dataKey="rps" stroke={chartColors.blue} strokeWidth={2} dot={false} name={t('gatewayMonitoring.rps')} />
                      </LineChart>
                    </ResponsiveContainer>
                  ) : (
                    <MiniChartState>{t('gatewayMonitoring.noRequestData')}</MiniChartState>
                  )
                ) : (
                  gwVolumeData.length > 0 ? (
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={gwVolumeData}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                        <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <Tooltip
                          contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                          labelStyle={{ color: chartColors.axis }}
                          itemStyle={{ color: chartColors.textSecondary }}
                        />
                        <Bar dataKey="requests" fill={chartColors.blue} name={t('gatewayMonitoring.requests')} />
                      </BarChart>
                    </ResponsiveContainer>
                  ) : (
                    <MiniChartState>{t('gatewayMonitoring.noRequestData')}</MiniChartState>
                  )
                )}
              </div>
            </>
          )}
        </>
      )}

      {/* LLM Token Usage */}
      {canViewMonitoring && (
        <>
          <div className="section-header">
            <h2 className="section-title">{t('dashboard.llmMonitoring')}</h2>
            <div className="section-header__actions">
              <BucketSelector value={llmBucket} onChange={setLlmBucket} />
              <Link to="/llm/monitoring" className="section-link">{t('dashboard.viewDetails')}</Link>
            </div>
          </div>
          {llmSummaryQuery.isLoading && (
            <div className="loading-message" role="status">{t('llmMonitoring.loadingMetrics')}</div>
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
                  <div className="summary-card__label">{t('llmMonitoring.totalTokens', { range: dashRange(llmBucket) })}</div>
                </div>
                <div className="summary-card">
                  <div className="summary-card__value">${llmSummaryQuery.data.estimated_cost.toFixed(2)}</div>
                  <div className="summary-card__label">{t('llmMonitoring.estimatedCost')}</div>
                </div>
                <div className="summary-card">
                  <div className="summary-card__value">{llmSummaryQuery.data.total_requests.toLocaleString()}</div>
                  <div className="summary-card__label">{t('llmMonitoring.totalRequests', { range: dashRange(llmBucket) })}</div>
                </div>
              </div>
              <div className={`dashboard-mini-chart ${llmChartHasData ? '' : 'dashboard-mini-chart--empty'}`}>
                {llmTokensQuery.isLoading ? (
                  <MiniChartState>{t('llmMonitoring.loadingMetrics')}</MiniChartState>
                ) : llmTokensQuery.isError ? (
                  <MiniChartState>{t('dashboard.llmNoData')}</MiniChartState>
                ) : llmChartHasData ? (
                  <ResponsiveContainer width="100%" height="100%">
                    {llmBucket === 'auto' ? (
                      <LineChart data={llmTokenChartData}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                        <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <Tooltip
                          contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                          labelStyle={{ color: chartColors.axis }}
                          itemStyle={{ color: chartColors.textSecondary }}
                        />
                        <Legend wrapperStyle={{ color: chartColors.axis, fontSize: 11 }} />
                        <Line type="monotone" dataKey="prompt" stroke={chartColors.blue} strokeWidth={2} dot={false} name={t('llmMonitoring.prompt')} />
                        <Line type="monotone" dataKey="completion" stroke={chartColors.green} strokeWidth={2} dot={false} name={t('llmMonitoring.completion')} />
                      </LineChart>
                    ) : (
                      <BarChart data={llmTokenChartData}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                        <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <Tooltip
                          contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                          labelStyle={{ color: chartColors.axis }}
                          itemStyle={{ color: chartColors.textSecondary }}
                        />
                        <Legend wrapperStyle={{ color: chartColors.axis, fontSize: 11 }} />
                        <Bar dataKey="prompt" stackId="tokens" fill={chartColors.blue} name={t('llmMonitoring.prompt')} />
                        <Bar dataKey="completion" stackId="tokens" fill={chartColors.green} name={t('llmMonitoring.completion')} />
                      </BarChart>
                    )}
                  </ResponsiveContainer>
                ) : (
                  <MiniChartState>{t('llmMonitoring.noTokenData')}</MiniChartState>
                )}
              </div>
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
