import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import {
  getLlmSummary,
  getLlmTokens,
  getLlmByModel,
  getLlmTopKeys,
  getLlmErrors,
  getLlmRequestsTotal,
} from '../api/client';
import { useTheme } from '../components/ThemeContext';
import './LlmMonitoring.css';

const TIME_RANGES = ['15m', '1h', '6h', '24h', '7d', '30d', '60d'];
const LITELLM_ADMIN_URL = import.meta.env.VITE_LITELLM_ADMIN_URL || `http://${window.location.hostname}:${import.meta.env.VITE_LITELLM_PORT || '4000'}/ui`;

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function formatTimestamp(ts: number, range: string): string {
  const d = new Date(ts * 1000);
  if (['30d', '60d'].includes(range)) {
    return `${d.getMonth() + 1}/${d.getDate()}`;
  }
  if (range === '7d') {
    return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}h`;
  }
  return formatTime(ts);
}

function getCssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

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
  const [range, setRange] = useState('1h');
  const { resolved } = useTheme();

  const chartColors = useMemo(() => ({
    grid: getCssVar('--chart-grid'),
    axis: getCssVar('--chart-axis'),
    tooltipBg: getCssVar('--chart-tooltip-bg'),
    tooltipBorder: getCssVar('--chart-tooltip-border'),
    blue: getCssVar('--accent-blue'),
    green: getCssVar('--accent-green'),
    yellow: getCssVar('--accent-yellow'),
    red: getCssVar('--accent-red'),
    textSecondary: getCssVar('--text-secondary'),
  }), [resolved]);

  const summaryQuery = useQuery({
    queryKey: ['llm-summary', range],
    queryFn: () => getLlmSummary(range),
    refetchInterval: 30_000,
  });

  const tokensQuery = useQuery({
    queryKey: ['llm-tokens', range],
    queryFn: () => getLlmTokens(range),
    refetchInterval: 30_000,
  });

  const byModelQuery = useQuery({
    queryKey: ['llm-by-model', range],
    queryFn: () => getLlmByModel(range),
    refetchInterval: 30_000,
  });

  const topKeysQuery = useQuery({
    queryKey: ['llm-top-keys', range],
    queryFn: () => getLlmTopKeys(range),
    refetchInterval: 30_000,
  });

  const errorsQuery = useQuery({
    queryKey: ['llm-errors', range],
    queryFn: () => getLlmErrors(range),
    refetchInterval: 30_000,
  });

  const requestsTotalQuery = useQuery({
    queryKey: ['llm-requests-total', range],
    queryFn: () => getLlmRequestsTotal(range),
    refetchInterval: 30_000,
  });

  const summary = summaryQuery.data;
  const tokenData = tokensQuery.data;
  const tokenChartData = (tokenData?.prompt ?? []).map((p, i) => ({
    time: formatTimestamp(p.timestamp, range),
    prompt: Math.round(p.value),
    completion: Math.round(tokenData?.completion?.[i]?.value ?? 0),
  }));

  const errorChartData = (errorsQuery.data ?? []).map((p) => ({
    time: formatTimestamp(p.timestamp, range),
    success: p.success,
    error: p.error,
  }));

  const isLoading = summaryQuery.isLoading;
  const isError = summaryQuery.isError;
  const hasPartialError = !isError && (
    tokensQuery.isError || byModelQuery.isError ||
    topKeysQuery.isError || errorsQuery.isError || requestsTotalQuery.isError
  );

  return (
    <div className="llm-monitoring">
      <div className="page-header">
        <div>
          <h1>{t('llmMonitoring.title')}</h1>
          <p className="page-subtitle">{t('llmMonitoring.subtitle')}</p>
        </div>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
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
          <div className="time-range-toggle">
            {TIME_RANGES.map((r) => (
              <button
                key={r}
                className={`time-range-btn ${r === range ? 'time-range-btn--active' : ''}`}
                onClick={() => setRange(r)}
              >
                {r}
            </button>
          ))}
          </div>
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
            <div className="metric-card__label">{t('llmMonitoring.totalTokens', { range })}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{formatCost(summary.estimated_cost)}</div>
            <div className="metric-card__label">{t('llmMonitoring.estimatedCost')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{summary.total_requests.toLocaleString()}</div>
            <div className="metric-card__label">{t('llmMonitoring.totalRequests', { range })}</div>
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
              <BarChart data={(requestsTotalQuery.data ?? []).map((p) => ({ time: formatTimestamp(p.timestamp, range), requests: Math.round(p.value) }))}>
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
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.tokens')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.cost')}</th>
                </tr>
              </thead>
              <tbody>
                {(byModelQuery.data ?? []).map((m) => (
                  <tr key={m.model}>
                    <td className="cell-alias">{m.model}</td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(m.tokens)}
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

      {/* Top API Keys */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.topKeys')}</div>
        {(topKeysQuery.data ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('llmMonitoring.apiKey')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.tokens')}</th>
                  <th style={{ textAlign: 'right' }}>{t('llmMonitoring.requests')}</th>
                </tr>
              </thead>
              <tbody>
                {(topKeysQuery.data ?? []).map((k) => (
                  <tr key={k.api_key}>
                    <td className="cell-alias" style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {k.api_key.length > 12 ? `${k.api_key.slice(0, 8)}...${k.api_key.slice(-4)}` : k.api_key}
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
