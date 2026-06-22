import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend, Cell,
} from 'recharts';
import {
  getLlmSummary,
  getLlmTokens,
  getLlmByModel,
  getLlmTopKeys,
  getLlmByModelSeries,
  getLlmTopKeysSeries,
  getLlmErrors,
  getLlmStatusCodes,
  getLlmRequestsTotal,
  getApiKeys,
} from '../api/client';
import { useChartTheme, statusCodeColor } from '../components/useChartTheme';
import { usePermissions } from '../components/usePermissions';
import BucketedBreakdownView from '../components/BucketedBreakdownView';
import './Monitoring.css';
import './LlmMonitoring.css';
import TimeRangeSelector from '../components/TimeRangeSelector';
import BucketSelector from '../components/BucketSelector';
import { type TimeSelection, type Bucket, selectionKey, selectionSpanSeconds, bucketKey, periodForBucket } from '../utils/timeRange';
import { formatChartTimestamp, formatBucketLabel } from '../utils/time';

const LITELLM_ADMIN_URL = window.__RUNTIME_CONFIG__?.LITELLM_ADMIN_URL || import.meta.env.VITE_LITELLM_ADMIN_URL || 'https://localhost:4000/ui';

function formatCost(value: number): string {
  return `$${value.toFixed(2)}`;
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function LlmMonitoring() {
  const { t } = useTranslation();
  const { permissions, loaded: permissionsLoaded } = usePermissions();
  const [selection, setSelection] = useState<TimeSelection>({ kind: 'preset', value: '1h' });
  const selKey = selectionKey(selection);
  const span = selectionSpanSeconds(selection);
  const refetchInterval = selection.kind === 'custom' ? false : 30_000;
  const rangeLabel = selection.kind === 'preset' ? selection.value : t('llmMonitoring.customRange');
  const [selectedKey, setSelectedKey] = useState<string>('');
  const [bucket, setBucket] = useState<Bucket>('auto');
  const volumeLabel = (ts: number) =>
    bucket === 'auto' ? formatChartTimestamp(ts, span) : formatBucketLabel(ts, bucket);
  const chartColors = useChartTheme();

  const keyFilter = selectedKey || undefined;

  const summaryQuery = useQuery({
    queryKey: ['llm-summary', selKey, selectedKey],
    queryFn: () => getLlmSummary(selection, keyFilter),
    refetchInterval,
  });

  const tokensQuery = useQuery({
    queryKey: ['llm-tokens', selKey, selectedKey, bucketKey(bucket)],
    queryFn: () => getLlmTokens(selection, bucket, keyFilter),
    refetchInterval,
  });

  const byModelQuery = useQuery({
    queryKey: ['llm-by-model', selKey, selectedKey],
    queryFn: () => getLlmByModel(selection, keyFilter),
    refetchInterval,
  });

  // Cross-key overview; the panel is hidden when scoped to one key, so skip the fetch.
  const topKeysQuery = useQuery({
    queryKey: ['llm-top-keys', selKey],
    queryFn: () => getLlmTopKeys(selection),
    refetchInterval,
    enabled: !selectedKey,
  });

  const byModelSeriesQuery = useQuery({
    queryKey: ['llm-by-model-series', selKey, selectedKey, bucketKey(bucket)],
    queryFn: () => getLlmByModelSeries(selection, bucket, keyFilter),
    enabled: bucket !== 'auto',
    refetchInterval,
  });

  const topKeysSeriesQuery = useQuery({
    queryKey: ['llm-top-keys-series', selKey, bucketKey(bucket)],
    queryFn: () => getLlmTopKeysSeries(selection, bucket),
    enabled: bucket !== 'auto' && !selectedKey,
    refetchInterval,
  });

  const errorsQuery = useQuery({
    queryKey: ['llm-errors', selKey, selectedKey, bucketKey(bucket)],
    queryFn: () => getLlmErrors(selection, bucket, keyFilter),
    refetchInterval,
  });

  const statusCodesQuery = useQuery({
    queryKey: ['llm-status-codes', selKey, selectedKey],
    queryFn: () => getLlmStatusCodes(selection, keyFilter),
    refetchInterval,
  });

  const requestsTotalQuery = useQuery({
    queryKey: ['llm-requests-total', selKey, selectedKey, bucketKey(bucket)],
    queryFn: () => getLlmRequestsTotal(selection, bucket, keyFilter),
    refetchInterval,
  });

  // Map API key name → its description, to surface the key's purpose on hover
  // in the Top API Keys table. Requires apikeys.read; degrades to no tooltip.
  const canReadApiKeys = permissionsLoaded && permissions.includes('apikeys.read');
  const apiKeysQuery = useQuery({
    queryKey: ['api-keys', 'llm-monitoring-descriptions'],
    queryFn: getApiKeys,
    staleTime: 5 * 60 * 1000,
    refetchInterval: false,
    enabled: canReadApiKeys,
  });
  const apiKeyDescriptions = useMemo(() => {
    const map: Record<string, string> = {};
    for (const k of apiKeysQuery.data ?? []) {
      if (k.description) map[k.name] = k.description;
    }
    return map;
  }, [apiKeysQuery.data]);
  const apiKeyOptions = useMemo(() => {
    const items = apiKeysQuery.data ?? [];
    return [...items].sort((a, b) => a.name.localeCompare(b.name));
  }, [apiKeysQuery.data]);

  const summary = summaryQuery.data;
  const tokenData = tokensQuery.data;
  const tokenChartData = (tokenData?.prompt ?? []).map((p, i) => ({
    time: volumeLabel(p.timestamp),
    prompt: Math.round(p.value),
    completion: Math.round(tokenData?.completion?.[i]?.value ?? 0),
  }));

  const errorChartData = (errorsQuery.data ?? []).map((p) => ({
    time: volumeLabel(p.timestamp),
    success: p.success,
    error: p.error,
  }));

  const isLoading = summaryQuery.isLoading;
  const isError = summaryQuery.isError;
  const hasPartialError = !isError && (
    tokensQuery.isError || byModelQuery.isError ||
    topKeysQuery.isError || errorsQuery.isError ||
    statusCodesQuery.isError || requestsTotalQuery.isError ||
    byModelSeriesQuery.isError || topKeysSeriesQuery.isError
  );

  return (
    <div className="llm-monitoring">
      <div className="page-header">
        <div>
          <h1>{t('llmMonitoring.title')}</h1>
          <p className="page-subtitle">{t('llmMonitoring.subtitle')}</p>
        </div>
        <div className="page-header__filters">
          <a
            href={LITELLM_ADMIN_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="admin-link-btn"
          >
            {t('llmMonitoring.adminDashboard')}
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" style={{ marginLeft: 4 }}>
              <path d="M3.5 1.5H10.5V8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M10.5 1.5L1.5 10.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </a>
          {canReadApiKeys && (
            <label className="api-key-filter">
              <span className="api-key-filter__label">{t('llmMonitoring.apiKeyFilter')}</span>
              <select
                className="api-key-filter__select"
                value={selectedKey}
                onChange={(e) => setSelectedKey(e.target.value)}
              >
                <option value="">{t('llmMonitoring.allApiKeys')}</option>
                {apiKeyOptions.map((k) => (
                  <option key={k.name} value={k.name} title={k.description || undefined}>{k.name}</option>
                ))}
              </select>
            </label>
          )}
          <TimeRangeSelector value={selection} onChange={setSelection} />
          <BucketSelector
            value={bucket}
            onChange={(b) => {
              setBucket(b);
              const p = periodForBucket(b);
              if (p) setSelection(p);
            }}
          />
        </div>
      </div>

      {isLoading && <div className="loading-message">{t('llmMonitoring.loadingMetrics')}</div>}
      {isError && <div className="error-banner">{t('llmMonitoring.loadFailed')}</div>}
      {hasPartialError && <div className="error-banner">{t('llmMonitoring.partialLoadFailed')}</div>}

      {/* Summary Cards */}
      {summary && (
        <div className="metric-cards">
          <div className="metric-card">
            <div className="metric-card__value">{formatTokens(summary.total_tokens)}</div>
            <div className="metric-card__label">{t('llmMonitoring.totalTokens', { range: rangeLabel })}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{formatCost(summary.estimated_cost)}</div>
            <div className="metric-card__label">{t('llmMonitoring.estimatedCost')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{summary.total_requests.toLocaleString()}</div>
            <div className="metric-card__label">{t('llmMonitoring.totalRequests', { range: rangeLabel })}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{summary.avg_latency_ms}ms</div>
            <div className="metric-card__label">{t('llmMonitoring.avgLatency')}</div>
          </div>
        </div>
      )}

      {/* Token Usage Trend */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.tokenTrend')}</div>
        {tokenChartData.length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={tokenChartData}>
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
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="no-data">{t('llmMonitoring.noTokenData')}</div>
        )}
      </div>

      {/* Request Volume */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.requestVolume')}</div>
        {(requestsTotalQuery.data ?? []).length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={(requestsTotalQuery.data ?? []).map((p) => ({ time: volumeLabel(p.timestamp), requests: Math.round(p.value) }))}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                  labelStyle={{ color: chartColors.axis }}
                  itemStyle={{ color: chartColors.textSecondary }}
                />
                <Bar dataKey="requests" fill={chartColors.blue} name={t('llmMonitoring.requests')} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="no-data">{t('llmMonitoring.noData')}</div>
        )}
      </div>

      {/* Usage by Model */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.byModel')}</div>
        {(byModelQuery.data ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('llmMonitoring.model')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.inputTokens')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.outputTokens')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.totalTokenShort')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.requests')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.cost')}</th>
                </tr>
              </thead>
              <tbody>
                {(byModelQuery.data ?? []).map((m) => (
                  <tr key={m.model}>
                    <td className="cell-alias">{m.model}</td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(m.input_tokens)}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(m.output_tokens)}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(m.tokens)}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {m.requests.toLocaleString()}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatCost(m.cost)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="no-data">{t('llmMonitoring.noModelData')}</div>
        )}
      </div>

      {/* Usage by Model over time */}
      <BucketedBreakdownView
        title={t('breakdown.byModelOverTime')}
        data={byModelSeriesQuery.data}
        loading={byModelSeriesQuery.isLoading}
        bucket={bucket}
        unit="tokens"
        valueFmt={formatTokens}
      />

      {/* Top API Keys — cross-key overview; hidden when scoped to a single key. */}
      {!selectedKey && (
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.topKeys')}</div>
        {(topKeysQuery.data ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('llmMonitoring.apiKey')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.inputTokens')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.outputTokens')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.totalTokenShort')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.requests')}</th>
                </tr>
              </thead>
              <tbody>
                {(topKeysQuery.data ?? []).map((k) => (
                  <tr key={k.api_key}>
                    <td className="cell-alias" title={apiKeyDescriptions[k.api_key] || undefined}>
                      {k.api_key}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(k.input_tokens)}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(k.output_tokens)}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(k.tokens)}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {k.requests.toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="no-data">{t('llmMonitoring.noKeyData')}</div>
        )}
      </div>
      )}

      {/* Top API Keys over time — same cross-key scope */}
      {!selectedKey && (
        <BucketedBreakdownView
          title={t('breakdown.byKeyOverTime')}
          data={topKeysSeriesQuery.data}
          loading={topKeysSeriesQuery.isLoading}
          bucket={bucket}
          unit="tokens"
          valueFmt={formatTokens}
        />
      )}

      {/* Status Code Distribution */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.statusCodeDist')}</div>
        {(statusCodesQuery.data ?? []).length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={statusCodesQuery.data}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="code" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                  labelStyle={{ color: chartColors.axis }}
                  itemStyle={{ color: chartColors.textSecondary }}
                />
                <Bar dataKey="count" name={t('llmMonitoring.requests')}>
                  {(statusCodesQuery.data ?? []).map((entry, index) => (
                    <Cell key={index} fill={statusCodeColor(entry.code, chartColors)} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="no-data">{t('llmMonitoring.noStatusData')}</div>
        )}
      </div>

      {/* Success / Error Rate */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.errorRate')}</div>
        {errorChartData.length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={errorChartData}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                  labelStyle={{ color: chartColors.axis }}
                  itemStyle={{ color: chartColors.textSecondary }}
                />
                <Legend wrapperStyle={{ color: chartColors.axis, fontSize: 11 }} />
                <Bar dataKey="success" stackId="status" fill={chartColors.green} name={t('llmMonitoring.success')} />
                <Bar dataKey="error" stackId="status" fill={chartColors.red} name={t('llmMonitoring.error')} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="no-data">{t('llmMonitoring.noErrorData')}</div>
        )}
      </div>
    </div>
  );
}

export default LlmMonitoring;
