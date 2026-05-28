import { useState, useMemo } from 'react';
import type { KeyboardEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend, Cell,
} from 'recharts';
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsRequestsTotal,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsRoutesComparison,
  getApiKeys,
  type RouteComparisonRow,
} from '../api/client';
import { useChartTheme, statusCodeColor } from '../components/useChartTheme';
import { usePermissions } from '../components/usePermissions';
import './Monitoring.css';
import './GatewayMonitoring.css';
import TimeRangeSelector from '../components/TimeRangeSelector';
import { type TimeSelection, selectionKey, selectionSpanSeconds } from '../utils/timeRange';
import { formatChartTime, formatChartTimestamp } from '../utils/time';

function BarCell({ value, max, suffix = '' }: { value: number; max: number; suffix?: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <span className="bar-cell">
      <span className="bar-cell__fill" style={{ width: `${pct}%` }} />
      <span className="bar-cell__text">{value.toLocaleString(undefined, { maximumFractionDigits: 2 })}{suffix}</span>
    </span>
  );
}

function errorRateClass(v: number): string {
  if (v >= 5) return 'heatmap-cell heatmap-cell--red';
  if (v >= 1) return 'heatmap-cell heatmap-cell--yellow';
  return 'heatmap-cell';
}

function latencyClass(v: number | null, max: number): string {
  if (v == null || max <= 0) return 'heatmap-cell';
  const ratio = v / max;
  if (ratio >= 0.8) return 'heatmap-cell heatmap-cell--red';
  if (ratio >= 0.5) return 'heatmap-cell heatmap-cell--yellow';
  return 'heatmap-cell';
}

type SortColumn = 'route' | 'requests' | 'share' | 'error_rate' | 'latency_p50_ms' | 'latency_p95_ms';
type SortDir = 'asc' | 'desc';

function SortableHeader({
  column,
  label,
  align = 'left',
  activeColumn,
  dir,
  onToggle,
}: {
  column: SortColumn;
  label: string;
  align?: 'left' | 'right';
  activeColumn: SortColumn;
  dir: SortDir;
  onToggle: (c: SortColumn) => void;
}) {
  const active = activeColumn === column;
  const ariaSort: 'none' | 'ascending' | 'descending' =
    active ? (dir === 'asc' ? 'ascending' : 'descending') : 'none';
  const classes = `sortable-header${align === 'right' ? ' sortable-header--right' : ''}`;
  const handleKey = (e: KeyboardEvent<HTMLTableCellElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onToggle(column);
    }
  };
  return (
    <th
      className={classes}
      onClick={() => onToggle(column)}
      onKeyDown={handleKey}
      tabIndex={0}
      role="button"
      aria-sort={ariaSort}
    >
      {label}
      {active && <span className="sort-indicator">{dir === 'asc' ? '▲' : '▼'}</span>}
    </th>
  );
}

function GatewayMonitoring() {
  const { t } = useTranslation();
  const { permissions, loaded: permissionsLoaded } = usePermissions();
  const [selection, setSelection] = useState<TimeSelection>({ kind: 'preset', value: '1h' });
  const selKey = selectionKey(selection);
  const span = selectionSpanSeconds(selection);
  const refetchInterval = selection.kind === 'custom' ? false : 30_000;
  const rangeLabel = selection.kind === 'preset' ? selection.value : t('gatewayMonitoring.customRange');
  const [selectedRoute, setSelectedRoute] = useState<string | null>(null);
  const [sort, setSort] = useState<{ column: SortColumn; dir: SortDir }>({ column: 'requests', dir: 'desc' });
  const [selectedConsumer, setSelectedConsumer] = useState<string>('');

  const toggleSort = (column: SortColumn) => {
    setSort((prev) =>
      prev.column === column
        ? { column, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
        : { column, dir: 'desc' }
    );
  };


  const chartColors = useChartTheme();
  const canReadApiKeys = permissionsLoaded && permissions.includes('apikeys.read');

  const apiKeysQuery = useQuery({
    queryKey: ['api-keys', 'gateway-monitoring-filter'],
    queryFn: getApiKeys,
    staleTime: 5 * 60 * 1000,
    refetchInterval: false,
    enabled: canReadApiKeys,
  });
  const apiKeyOptions = useMemo(() => {
    const items = apiKeysQuery.data ?? [];
    return [...items].sort((a, b) => a.name.localeCompare(b.name));
  }, [apiKeysQuery.data]);

  const summaryQuery = useQuery({
    queryKey: ['metrics-summary', selKey, selectedConsumer],
    queryFn: () => getMetricsSummary(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  const requestsQuery = useQuery({
    queryKey: ['metrics-requests', selKey, selectedConsumer],
    queryFn: () => getMetricsRequests(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  const statusQuery = useQuery({
    queryKey: ['metrics-status-codes', selKey, selectedConsumer],
    queryFn: () => getMetricsStatusCodes(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  const latencyQuery = useQuery({
    queryKey: ['metrics-latency', selKey, selectedConsumer],
    queryFn: () => getMetricsLatency(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  const routesComparisonQuery = useQuery({
    queryKey: ['metrics-routes-comparison', selKey, selectedConsumer],
    queryFn: () => getMetricsRoutesComparison(selection, selectedConsumer || undefined),
    refetchInterval,
  });

  const routeLabel = (r: RouteComparisonRow) => r.name || r.route;

  const sortedRoutes = useMemo<RouteComparisonRow[]>(() => {
    const rows = routesComparisonQuery.data?.routes ?? [];
    const multiplier = sort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      if (sort.column === 'route') {
        return routeLabel(a).localeCompare(routeLabel(b)) * multiplier;
      }
      const av = a[sort.column];
      const bv = b[sort.column];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      return ((av as number) - (bv as number)) * multiplier;
    });
  }, [routesComparisonQuery.data, sort]);

  const selectedRouteLabel = useMemo(() => {
    if (!selectedRoute) return null;
    const rows = routesComparisonQuery.data?.routes ?? [];
    const found = rows.find((r) => r.route === selectedRoute);
    return found ? routeLabel(found) : selectedRoute;
  }, [routesComparisonQuery.data, selectedRoute]);

  const maxRequests = useMemo(() => {
    const rows = routesComparisonQuery.data?.routes ?? [];
    return rows.reduce((m, r) => (r.requests > m ? r.requests : m), 0);
  }, [routesComparisonQuery.data]);

  const { maxP50, maxP95 } = useMemo(() => {
    const rows = routesComparisonQuery.data?.routes ?? [];
    return {
      maxP50: rows.reduce((m, r) => (r.latency_p50_ms != null && r.latency_p50_ms > m ? r.latency_p50_ms : m), 0),
      maxP95: rows.reduce((m, r) => (r.latency_p95_ms != null && r.latency_p95_ms > m ? r.latency_p95_ms : m), 0),
    };
  }, [routesComparisonQuery.data]);

  const requestsTotalQuery = useQuery({
    queryKey: ['metrics-requests-total', selKey, selectedConsumer],
    queryFn: () => getMetricsRequestsTotal(selection, undefined, selectedConsumer || undefined),
    refetchInterval,
  });

  // Route drill-down queries
  const routeSummaryQuery = useQuery({
    queryKey: ['metrics-summary', selKey, selectedRoute, selectedConsumer],
    queryFn: () => getMetricsSummary(selection, selectedRoute!, selectedConsumer || undefined),
    refetchInterval,
    enabled: !!selectedRoute,
  });

  const routeRequestsQuery = useQuery({
    queryKey: ['metrics-requests', selKey, selectedRoute, selectedConsumer],
    queryFn: () => getMetricsRequests(selection, selectedRoute!, selectedConsumer || undefined),
    refetchInterval,
    enabled: !!selectedRoute,
  });

  const routeStatusQuery = useQuery({
    queryKey: ['metrics-status-codes', selKey, selectedRoute, selectedConsumer],
    queryFn: () => getMetricsStatusCodes(selection, selectedRoute!, selectedConsumer || undefined),
    refetchInterval,
    enabled: !!selectedRoute,
  });

  const routeVolumQuery = useQuery({
    queryKey: ['metrics-requests-total', selKey, selectedRoute, selectedConsumer],
    queryFn: () => getMetricsRequestsTotal(selection, selectedRoute!, selectedConsumer || undefined),
    refetchInterval,
    enabled: !!selectedRoute,
  });

  const summary = summaryQuery.data;
  const requestsData = (requestsQuery.data ?? []).map((p) => ({
    time: formatChartTime(p.timestamp),
    rps: p.value,
  }));

  const latencyData = latencyQuery.data;
  const latencyChartData = (latencyData?.p50 ?? []).map((p, i) => ({
    time: formatChartTime(p.timestamp),
    p50: p.value,
    p95: latencyData?.p95?.[i]?.value ?? 0,
    p99: latencyData?.p99?.[i]?.value ?? 0,
  }));

  const isLoading = summaryQuery.isLoading;
  const isError = summaryQuery.isError;
  const hasPartialError = !isError && (
    requestsQuery.isError || requestsTotalQuery.isError ||
    statusQuery.isError || latencyQuery.isError || routesComparisonQuery.isError
  );

  return (
    <div className="gateway-monitoring">
      <div className="page-header">
        <div>
          <h1>{t('gatewayMonitoring.title')}</h1>
          <p className="page-subtitle">{t('gatewayMonitoring.subtitle')}</p>
        </div>
        <div className="page-header__filters">
          {canReadApiKeys && (
            <label className="api-key-filter">
              <span className="api-key-filter__label">{t('gatewayMonitoring.apiKeyFilter')}</span>
              <select
                className="api-key-filter__select"
                value={selectedConsumer}
                onChange={(e) => setSelectedConsumer(e.target.value)}
              >
                <option value="">{t('gatewayMonitoring.allApiKeys')}</option>
                {apiKeyOptions.map((k) => (
                  <option key={k.name} value={k.name}>{k.name}</option>
                ))}
              </select>
            </label>
          )}
          <TimeRangeSelector value={selection} onChange={setSelection} />
        </div>
      </div>

      {isLoading && <div className="loading-message">{t('gatewayMonitoring.loadingMetrics')}</div>}
      {isError && <div className="error-banner">{t('gatewayMonitoring.loadFailed')}</div>}
      {hasPartialError && <div className="error-banner">{t('gatewayMonitoring.partialLoadFailed')}</div>}

      {/* Summary Cards */}
      {summary && (
        <div className="metric-cards">
          <div className="metric-card">
            <div className="metric-card__value">{summary.total_requests.toLocaleString()}</div>
            <div className="metric-card__label">{t('gatewayMonitoring.totalRequests', { range: rangeLabel })}</div>
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

      {/* Request Volume (total counts per bucket) */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.requestVolume')}</div>
        {(requestsTotalQuery.data ?? []).length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={(requestsTotalQuery.data ?? []).map((p) => ({ time: formatChartTimestamp(p.timestamp, span), requests: Math.round(p.value) }))}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                  labelStyle={{ color: chartColors.axis }}
                  itemStyle={{ color: chartColors.textSecondary }}
                />
                <Bar dataKey="requests" fill={chartColors.green} name="Requests" />
              </BarChart>
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
                    <Cell key={index} fill={statusCodeColor(entry.code, chartColors)} />
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

      {/* Route Comparison */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.routeComparison')}</div>
        {(routesComparisonQuery.data?.routes ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table comparison-table">
              <thead>
                <tr>
                  <SortableHeader column="route"            label={t('gatewayMonitoring.route')}        activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="requests"         label={t('gatewayMonitoring.requests')}     align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="share"            label={t('gatewayMonitoring.share')}        align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="error_rate"       label={t('gatewayMonitoring.errorRate')}    align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="latency_p50_ms"   label={t('gatewayMonitoring.latencyP50')}   align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="latency_p95_ms"   label={t('gatewayMonitoring.latencyP95')}   align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                </tr>
              </thead>
              <tbody>
                {sortedRoutes.map((r) => (
                  <tr
                    key={r.route}
                    className={`route-row ${selectedRoute === r.route ? 'route-row--selected' : ''}`}
                    onClick={() => setSelectedRoute(selectedRoute === r.route ? null : r.route)}
                  >
                    <td className="cell-alias" title={r.name ? r.route : undefined}>{routeLabel(r)}</td>
                    <td className="cell-metric"><BarCell value={r.requests} max={maxRequests} /></td>
                    <td className="cell-metric"><BarCell value={r.share} max={100} suffix="%" /></td>
                    <td className={`cell-metric ${errorRateClass(r.error_rate)}`}>{r.error_rate.toFixed(2)}%</td>
                    <td className={`cell-metric ${latencyClass(r.latency_p50_ms, maxP50)}`}>{r.latency_p50_ms == null ? '—' : r.latency_p50_ms.toFixed(1)}</td>
                    <td className={`cell-metric ${latencyClass(r.latency_p95_ms, maxP95)}`}>{r.latency_p95_ms == null ? '—' : r.latency_p95_ms.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="no-data">{t('gatewayMonitoring.noRouteData')}</div>
        )}
      </div>

      {/* Route Detail Panel */}
      {selectedRoute && (
        <div className="route-detail-panel">
          <div className="route-detail-header">
            <span className="route-detail-title">{selectedRouteLabel}</span>
            <button className="route-detail-close" onClick={() => setSelectedRoute(null)}>&times;</button>
          </div>

          {routeSummaryQuery.isLoading ? (
            <div className="loading-message">{t('gatewayMonitoring.loadingMetrics')}</div>
          ) : routeSummaryQuery.data ? (
            <>
              <div className="metric-cards">
                <div className="metric-card">
                  <div className="metric-card__value">{routeSummaryQuery.data.total_requests.toLocaleString()}</div>
                  <div className="metric-card__label">{t('gatewayMonitoring.totalRequests', { range: rangeLabel })}</div>
                </div>
                <div className="metric-card">
                  <div className="metric-card__value" style={{ color: routeSummaryQuery.data.error_rate > 5 ? 'var(--accent-red)' : 'var(--accent-green)' }}>
                    {routeSummaryQuery.data.error_rate}%
                  </div>
                  <div className="metric-card__label">{t('gatewayMonitoring.errorRate')}</div>
                </div>
                <div className="metric-card">
                  <div className="metric-card__value">{routeSummaryQuery.data.avg_latency_ms}ms</div>
                  <div className="metric-card__label">{t('gatewayMonitoring.avgLatency')}</div>
                </div>
              </div>

              {/* Route Request Trend */}
              <div className="chart-panel chart-panel--nested">
                <div className="chart-panel__title">{t('gatewayMonitoring.requestTrend')}</div>
                {(routeRequestsQuery.data ?? []).length > 0 ? (
                  <div className="chart-container">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={(routeRequestsQuery.data ?? []).map((p) => ({ time: formatChartTime(p.timestamp), rps: p.value }))}>
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

              {/* Route Request Volume */}
              <div className="chart-panel chart-panel--nested">
                <div className="chart-panel__title">{t('gatewayMonitoring.requestVolume')}</div>
                {(routeVolumQuery.data ?? []).length > 0 ? (
                  <div className="chart-container">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={(routeVolumQuery.data ?? []).map((p) => ({ time: formatChartTimestamp(p.timestamp, span), requests: Math.round(p.value) }))}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                        <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <Tooltip
                          contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                          labelStyle={{ color: chartColors.axis }}
                          itemStyle={{ color: chartColors.textSecondary }}
                        />
                        <Bar dataKey="requests" fill={chartColors.green} name="Requests" />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                ) : (
                  <div className="no-data">{t('gatewayMonitoring.noRequestData')}</div>
                )}
              </div>

              {/* Route Status Code Distribution */}
              <div className="chart-panel chart-panel--nested">
                <div className="chart-panel__title">{t('gatewayMonitoring.statusCodeDist')}</div>
                {(routeStatusQuery.data ?? []).length > 0 ? (
                  <div className="chart-container">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={routeStatusQuery.data}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                        <XAxis dataKey="code" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <Tooltip
                          contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                          labelStyle={{ color: chartColors.axis }}
                          itemStyle={{ color: chartColors.textSecondary }}
                        />
                        <Bar dataKey="count" name="Requests">
                          {(routeStatusQuery.data ?? []).map((entry, index) => (
                            <Cell key={index} fill={statusCodeColor(entry.code, chartColors)} />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                ) : (
                  <div className="no-data">{t('gatewayMonitoring.noStatusData')}</div>
                )}
              </div>
            </>
          ) : null}
        </div>
      )}
    </div>
  );
}

export default GatewayMonitoring;
