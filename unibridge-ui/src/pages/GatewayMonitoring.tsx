import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend, Cell,
} from 'recharts';
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsTopRoutes,
} from '../api/client';
import { useTheme } from '../components/ThemeContext';
import './GatewayMonitoring.css';

const TIME_RANGES = ['15m', '1h', '6h', '24h'];

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function getCssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function getStatusColor(code: string): string {
  const green = getCssVar('--accent-green');
  const blue = getCssVar('--accent-blue');
  const yellow = getCssVar('--accent-yellow');
  const red = getCssVar('--accent-red');
  const muted = getCssVar('--text-tertiary');
  if (code.startsWith('2')) return green;
  if (code.startsWith('3')) return blue;
  if (code.startsWith('4')) return yellow;
  if (code.startsWith('5')) return red;
  return muted;
}

function GatewayMonitoring() {
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
    queryKey: ['metrics-summary', range],
    queryFn: () => getMetricsSummary(range),
    refetchInterval: 30_000,
  });

  const requestsQuery = useQuery({
    queryKey: ['metrics-requests', range],
    queryFn: () => getMetricsRequests(range),
    refetchInterval: 30_000,
  });

  const statusQuery = useQuery({
    queryKey: ['metrics-status-codes', range],
    queryFn: () => getMetricsStatusCodes(range),
    refetchInterval: 30_000,
  });

  const latencyQuery = useQuery({
    queryKey: ['metrics-latency', range],
    queryFn: () => getMetricsLatency(range),
    refetchInterval: 30_000,
  });

  const topRoutesQuery = useQuery({
    queryKey: ['metrics-top-routes', range],
    queryFn: () => getMetricsTopRoutes(range),
    refetchInterval: 30_000,
  });

  const summary = summaryQuery.data;
  const requestsData = (requestsQuery.data ?? []).map((p) => ({
    time: formatTime(p.timestamp),
    rps: p.value,
  }));

  const latencyData = latencyQuery.data;
  const latencyChartData = (latencyData?.p50 ?? []).map((p, i) => ({
    time: formatTime(p.timestamp),
    p50: p.value,
    p95: latencyData?.p95?.[i]?.value ?? 0,
    p99: latencyData?.p99?.[i]?.value ?? 0,
  }));

  const isLoading = summaryQuery.isLoading;
  const isError = summaryQuery.isError;

  return (
    <div className="gateway-monitoring">
      <div className="page-header">
        <div>
          <h1>{t('gatewayMonitoring.title')}</h1>
          <p className="page-subtitle">{t('gatewayMonitoring.subtitle')}</p>
        </div>
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

      {isLoading && <div className="loading-message">{t('gatewayMonitoring.loadingMetrics')}</div>}
      {isError && <div className="error-banner">{t('gatewayMonitoring.loadFailed')}</div>}

      {/* Summary Cards */}
      {summary && (
        <div className="metric-cards">
          <div className="metric-card">
            <div className="metric-card__value">{summary.total_requests.toLocaleString()}</div>
            <div className="metric-card__label">{t('gatewayMonitoring.totalRequests', { range })}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value" style={{ color: summary.error_rate > 5 ? 'var(--accent-red)' : 'var(--accent-green)' }}>
              {summary.error_rate}%
            </div>
            <div className="metric-card__label">{t('gatewayMonitoring.errorRate')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{summary.avg_latency_ms}ms</div>
            <div className="metric-card__label">{t('gatewayMonitoring.avgLatency')}</div>
          </div>
        </div>
      )}

      {/* Request Trend */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.requestTrend')}</div>
        {requestsData.length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={requestsData}>
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
        ) : (
          <div className="no-data">{t('gatewayMonitoring.noRequestData')}</div>
        )}
      </div>

      {/* Status Code Distribution */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.statusCodeDist')}</div>
        {(statusQuery.data ?? []).length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={statusQuery.data}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="code" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                  labelStyle={{ color: chartColors.axis }}
                  itemStyle={{ color: chartColors.textSecondary }}
                />
                <Bar dataKey="count" name="Requests">
                  {(statusQuery.data ?? []).map((entry, index) => (
                    <Cell key={index} fill={getStatusColor(entry.code)} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="no-data">{t('gatewayMonitoring.noStatusData')}</div>
        )}
      </div>

      {/* Latency */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.latency')}</div>
        {latencyChartData.length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={latencyChartData}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                  labelStyle={{ color: chartColors.axis }}
                  itemStyle={{ color: chartColors.textSecondary }}
                />
                <Legend wrapperStyle={{ color: chartColors.axis, fontSize: 11 }} />
                <Line type="monotone" dataKey="p50" stroke={chartColors.green} strokeWidth={2} dot={false} name="P50" />
                <Line type="monotone" dataKey="p95" stroke={chartColors.yellow} strokeWidth={2} dot={false} name="P95" />
                <Line type="monotone" dataKey="p99" stroke={chartColors.red} strokeWidth={2} dot={false} name="P99" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="no-data">{t('gatewayMonitoring.noLatencyData')}</div>
        )}
      </div>

      {/* Top Routes */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.topRoutes')}</div>
        {(topRoutesQuery.data ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t('gatewayMonitoring.route')}</th>
                  <th style={{ textAlign: 'right' }}>{t('gatewayMonitoring.requests')}</th>
                </tr>
              </thead>
              <tbody>
                {(topRoutesQuery.data ?? []).map((r) => (
                  <tr key={r.route}>
                    <td className="cell-alias">{r.route}</td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {r.requests.toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="no-data">{t('gatewayMonitoring.noRouteData')}</div>
        )}
      </div>
    </div>
  );
}

export default GatewayMonitoring;
