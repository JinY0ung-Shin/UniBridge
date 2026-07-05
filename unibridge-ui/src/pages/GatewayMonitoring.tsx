import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend, Cell, LabelList,
} from 'recharts';
import {
  getMetricsSummary,
  getMetricsRequests,
  getMetricsRequestsTotal,
  getMetricsStatusCodes,
  getMetricsLatency,
  getMetricsRoutesComparison,
  getMetricsConsumersComparison,
  getRoutesComparisonSeries,
  getConsumersComparisonSeries,
  getApiKeys,
  type RouteComparisonRow,
} from '../api/client';
import { useChartTheme, statusCodeColor } from '../components/useChartTheme';
import { usePermissions } from '../components/usePermissions';
import BucketedBreakdownView from '../components/BucketedBreakdownView';
import PanelStatus from '../components/PanelStatus';
import SortableHeader from '../components/SortableHeader';
import { type SortState, toggleSortState, sortRows } from '../utils/tableSort';
import './Monitoring.css';
import './GatewayMonitoring.css';
import TimeRangeSelector from '../components/TimeRangeSelector';
import BucketSelector from '../components/BucketSelector';
import { type TimeSelection, type Bucket, selectionKey, selectionSpanSeconds, bucketKey, periodForBucket, bucketTooCoarse } from '../utils/timeRange';
import { formatChartTime, formatChartTimestamp, formatBucketLabel } from '../utils/time';
import { errorRateColor } from '../utils/monitoring';

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

type RouteSortColumn = 'route' | 'requests' | 'share' | 'error_rate' | 'latency_p50_ms' | 'latency_p95_ms';
type ConsumerSortColumn = 'consumer' | 'requests' | 'share' | 'error_rate' | 'latency_p50_ms' | 'latency_p95_ms';

function GatewayMonitoring() {
  const { t } = useTranslation();
  const { permissions, loaded: permissionsLoaded } = usePermissions();
  const [selection, setSelection] = useState<TimeSelection>({ kind: 'preset', value: '1h' });
  const selKey = selectionKey(selection);
  const span = selectionSpanSeconds(selection);
  const refetchInterval = selection.kind === 'custom' ? false : 30_000;
  const rangeLabel = selection.kind === 'preset' ? selection.value : t('gatewayMonitoring.customRange');
  const [selectedRoute, setSelectedRoute] = useState<string | null>(null);
  const [sort, setSort] = useState<SortState<RouteSortColumn>>({ column: 'requests', dir: 'desc' });
  const [consumerSort, setConsumerSort] = useState<SortState<ConsumerSortColumn>>({ column: 'requests', dir: 'desc' });
  const [selectedConsumer, setSelectedConsumer] = useState<string>('');
  const [bucket, setBucket] = useState<Bucket>('auto');
  const routeDetailRef = useRef<HTMLDivElement | null>(null);
  const volumeLabel = (ts: number) =>
    bucket === 'auto' ? formatChartTimestamp(ts, span) : formatBucketLabel(ts, bucket);

  const toggleSort = (column: RouteSortColumn) => setSort((prev) => toggleSortState(prev, column));
  const toggleConsumerSort = (column: ConsumerSortColumn) =>
    setConsumerSort((prev) => toggleSortState(prev, column));

  // Shrinking the range under the current calendar bucket would leave a
  // one-bar chart; fall back to auto stepping instead.
  const handleSelectionChange = (next: TimeSelection) => {
    setSelection(next);
    if (bucketTooCoarse(next, bucket)) setBucket('auto');
  };

  // Picking day/week nudges the preset period to a matching span, but an
  // explicitly chosen custom range is never overridden.
  const handleBucketChange = (b: Bucket) => {
    setBucket(b);
    if (selection.kind !== 'custom') {
      const p = periodForBucket(b);
      if (p) setSelection(p);
    }
  };

  useEffect(() => {
    if (selectedRoute) {
      routeDetailRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
    }
  }, [selectedRoute]);

  const chartColors = useChartTheme();
  const canReadApiKeys = permissionsLoaded && permissions.includes('apikeys.read');
  const selfScopeOnly = permissionsLoaded
    && !permissions.includes('gateway.monitoring.read')
    && permissions.includes('gateway.monitoring.self');

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

  const consumersComparisonQuery = useQuery({
    queryKey: ['metrics-consumers-comparison', selKey],
    queryFn: () => getMetricsConsumersComparison(selection),
    refetchInterval,
    // Cross-key overview; the panel is hidden when scoped to one key, so skip the fetch.
    enabled: !selectedConsumer,
  });

  const routesSeriesQuery = useQuery({
    queryKey: ['metrics-routes-comparison-series', selKey, selectedConsumer, bucketKey(bucket)],
    queryFn: () => getRoutesComparisonSeries(selection, selectedConsumer || undefined, bucket),
    refetchInterval,
    enabled: bucket !== 'auto',
  });

  const consumersSeriesQuery = useQuery({
    queryKey: ['metrics-consumers-comparison-series', selKey, bucketKey(bucket)],
    queryFn: () => getConsumersComparisonSeries(selection, bucket),
    refetchInterval,
    enabled: bucket !== 'auto' && !selectedConsumer,
  });

  const apiKeyDescriptions = useMemo(() => {
    const map: Record<string, string> = {};
    for (const k of apiKeysQuery.data ?? []) {
      if (k.description) map[k.name] = k.description;
    }
    return map;
  }, [apiKeysQuery.data]);

  const maxConsumerRequests = useMemo(() => {
    const rows = consumersComparisonQuery.data?.consumers ?? [];
    return rows.reduce((m, c) => (c.requests > m ? c.requests : m), 0);
  }, [consumersComparisonQuery.data]);

  const { maxConsumerP50, maxConsumerP95 } = useMemo(() => {
    const rows = consumersComparisonQuery.data?.consumers ?? [];
    return {
      maxConsumerP50: rows.reduce((m, c) => (c.latency_p50_ms != null && c.latency_p50_ms > m ? c.latency_p50_ms : m), 0),
      maxConsumerP95: rows.reduce((m, c) => (c.latency_p95_ms != null && c.latency_p95_ms > m ? c.latency_p95_ms : m), 0),
    };
  }, [consumersComparisonQuery.data]);

  const routeLabel = (r: RouteComparisonRow) => r.name || r.route;

  const sortedRoutes = useMemo<RouteComparisonRow[]>(() => {
    const rows = routesComparisonQuery.data?.routes ?? [];
    return sortRows(rows, sort, (row, column) =>
      column === 'route' ? routeLabel(row) : row[column],
    );
  }, [routesComparisonQuery.data, sort]);

  const sortedConsumers = useMemo(() => {
    const rows = consumersComparisonQuery.data?.consumers ?? [];
    return sortRows(rows, consumerSort, (row, column) =>
      column === 'consumer' ? row.consumer : row[column],
    );
  }, [consumersComparisonQuery.data, consumerSort]);

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
    queryKey: ['metrics-requests-total', selKey, selectedConsumer, bucketKey(bucket)],
    queryFn: () => getMetricsRequestsTotal(selection, undefined, selectedConsumer || undefined, bucket),
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
    queryKey: ['metrics-requests-total', selKey, selectedRoute, selectedConsumer, bucketKey(bucket)],
    queryFn: () => getMetricsRequestsTotal(selection, selectedRoute!, selectedConsumer || undefined, bucket),
    refetchInterval,
    enabled: !!selectedRoute,
  });

  const summary = summaryQuery.data;
  const requestsData = (requestsQuery.data ?? []).map((p) => ({
    time: formatChartTime(p.timestamp),
    rps: p.value,
  }));

  const latencyData = latencyQuery.data;
  // Missing percentile points stay null so the lines show gaps instead of
  // misleading dips to zero.
  const latencyChartData = (latencyData?.p50 ?? []).map((p, i) => ({
    time: formatChartTime(p.timestamp),
    p50: p.value,
    p95: latencyData?.p95?.[i]?.value ?? null,
    p99: latencyData?.p99?.[i]?.value ?? null,
  }));

  const isLoading = summaryQuery.isLoading;
  const isError = summaryQuery.isError;
  const hasPartialError = !isError && (
    requestsQuery.isError || requestsTotalQuery.isError ||
    statusQuery.isError || latencyQuery.isError || routesComparisonQuery.isError ||
    consumersComparisonQuery.isError || routesSeriesQuery.isError || consumersSeriesQuery.isError
  );
  const routeDetailHasError = routeSummaryQuery.isError
    || routeRequestsQuery.isError
    || routeStatusQuery.isError
    || routeVolumQuery.isError;

  function toggleSelectedRoute(route: string) {
    setSelectedRoute((current) => (current === route ? null : route));
  }

  function handleRouteRowKeyDown(event: KeyboardEvent<HTMLTableRowElement>, route: string) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggleSelectedRoute(route);
    }
  }

  return (
    <div className="gateway-monitoring">
      <div className="page-header">
        <div>
          <h1>{t('gatewayMonitoring.title')}</h1>
          <p className="page-subtitle">{t('gatewayMonitoring.subtitle')}</p>
          <p className="page-meta">{t('monitoring.headerNote')}</p>
          {selfScopeOnly && <span className="scope-note">{t('gatewayMonitoring.selfScopeNote')}</span>}
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
                  <option key={k.name} value={k.name} title={k.description || undefined}>{k.name}</option>
                ))}
              </select>
            </label>
          )}
          <TimeRangeSelector value={selection} onChange={handleSelectionChange} />
          <BucketSelector value={bucket} onChange={handleBucketChange} />
        </div>
      </div>

      {isLoading && <div className="loading-message" role="status">{t('gatewayMonitoring.loadingMetrics')}</div>}
      {isError && <div className="error-banner" role="alert">{t('gatewayMonitoring.loadFailed')}</div>}
      {hasPartialError && <div className="error-banner" role="alert">{t('gatewayMonitoring.partialLoadFailed')}</div>}

      {/* Summary Cards */}
      {summary && (
        <div className="metric-cards">
          <div className="metric-card">
            <div className="metric-card__value">{summary.total_requests.toLocaleString()}</div>
            <div className="metric-card__label">{t('gatewayMonitoring.totalRequests', { range: rangeLabel })}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value" style={{ color: errorRateColor(summary.error_rate) }}>
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
                <Line type="monotone" dataKey="rps" stroke={chartColors.blue} strokeWidth={2} dot={false} name={t('gatewayMonitoring.rps')} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <PanelStatus
            loading={requestsQuery.isLoading}
            error={requestsQuery.isError}
            emptyText={t('gatewayMonitoring.noRequestData')}
          />
        )}
      </div>

      {/* Request Volume (total counts per bucket) */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.requestVolume')}</div>
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
                <Bar dataKey="requests" fill={chartColors.green} name={t('gatewayMonitoring.requests')} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <PanelStatus
            loading={requestsTotalQuery.isLoading}
            error={requestsTotalQuery.isError}
            emptyText={t('gatewayMonitoring.noRequestData')}
          />
        )}
      </div>

      {/* Status Code Distribution */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.statusCodeDist', { range: rangeLabel })}</div>
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
                <Bar dataKey="count" name={t('gatewayMonitoring.requests')}>
                  {(statusQuery.data ?? []).map((entry, index) => (
                    <Cell key={index} fill={statusCodeColor(entry.code, chartColors)} />
                  ))}
                  <LabelList dataKey="count" position="top" style={{ fontSize: 10, fill: chartColors.axis }} />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <PanelStatus
            loading={statusQuery.isLoading}
            error={statusQuery.isError}
            emptyText={t('gatewayMonitoring.noStatusData')}
          />
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
          <PanelStatus
            loading={latencyQuery.isLoading}
            error={latencyQuery.isError}
            emptyText={t('gatewayMonitoring.noLatencyData')}
          />
        )}
      </div>

      {/* Route Comparison */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.routeComparison', { range: rangeLabel })}</div>
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
                    onClick={() => toggleSelectedRoute(r.route)}
                    onKeyDown={(event) => handleRouteRowKeyDown(event, r.route)}
                    tabIndex={0}
                    role="button"
                    aria-pressed={selectedRoute === r.route}
                    aria-label={t('gatewayMonitoring.openRouteDetail', { route: routeLabel(r) })}
                  >
                    <td className="cell-alias" title={r.name ? r.route : undefined}>{routeLabel(r)}</td>
                    <td className="cell-metric"><BarCell value={r.requests} max={maxRequests} /></td>
                    <td className="cell-metric"><BarCell value={r.share} max={100} suffix="%" /></td>
                    <td className={`cell-metric ${errorRateClass(r.error_rate)}`}>{r.error_rate.toFixed(2)}%</td>
                    <td className="cell-metric">{r.latency_p50_ms == null ? '—' : <BarCell value={r.latency_p50_ms} max={maxP50} />}</td>
                    <td className="cell-metric">{r.latency_p95_ms == null ? '—' : <BarCell value={r.latency_p95_ms} max={maxP95} />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <PanelStatus
            loading={routesComparisonQuery.isLoading}
            error={routesComparisonQuery.isError}
            emptyText={t('gatewayMonitoring.noRouteData')}
          />
        )}
      </div>

      {/* Route Detail Panel — rendered directly under the table it drills
          into, and scrolled into view on open. */}
      {selectedRoute && (
        <div className="route-detail-panel" ref={routeDetailRef}>
          <div className="route-detail-header">
            <span className="route-detail-title">{selectedRouteLabel}</span>
            <button
              type="button"
              className="route-detail-close"
              onClick={() => setSelectedRoute(null)}
              aria-label={t('gatewayMonitoring.closeRouteDetail')}
              title={t('gatewayMonitoring.closeRouteDetail')}
            >
              &times;
            </button>
          </div>

          {routeDetailHasError && (
            <div className="error-banner" role="alert">
              {routeSummaryQuery.isError
                ? t('gatewayMonitoring.loadFailed')
                : t('gatewayMonitoring.partialLoadFailed')}
            </div>
          )}

          {routeSummaryQuery.isLoading ? (
            <div className="loading-message" role="status">{t('gatewayMonitoring.loadingMetrics')}</div>
          ) : routeSummaryQuery.data ? (
            <>
              <div className="metric-cards">
                <div className="metric-card">
                  <div className="metric-card__value">{routeSummaryQuery.data.total_requests.toLocaleString()}</div>
                  <div className="metric-card__label">{t('gatewayMonitoring.totalRequests', { range: rangeLabel })}</div>
                </div>
                <div className="metric-card">
                  <div className="metric-card__value" style={{ color: errorRateColor(routeSummaryQuery.data.error_rate) }}>
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
                        <Line type="monotone" dataKey="rps" stroke={chartColors.blue} strokeWidth={2} dot={false} name={t('gatewayMonitoring.rps')} />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                ) : (
                  <PanelStatus
                    loading={routeRequestsQuery.isLoading}
                    error={routeRequestsQuery.isError}
                    emptyText={t('gatewayMonitoring.noRequestData')}
                  />
                )}
              </div>

              {/* Route Request Volume */}
              <div className="chart-panel chart-panel--nested">
                <div className="chart-panel__title">{t('gatewayMonitoring.requestVolume')}</div>
                {(routeVolumQuery.data ?? []).length > 0 ? (
                  <div className="chart-container">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={(routeVolumQuery.data ?? []).map((p) => ({ time: volumeLabel(p.timestamp), requests: Math.round(p.value) }))}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                        <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                        <Tooltip
                          contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                          labelStyle={{ color: chartColors.axis }}
                          itemStyle={{ color: chartColors.textSecondary }}
                        />
                        <Bar dataKey="requests" fill={chartColors.green} name={t('gatewayMonitoring.requests')} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                ) : (
                  <PanelStatus
                    loading={routeVolumQuery.isLoading}
                    error={routeVolumQuery.isError}
                    emptyText={t('gatewayMonitoring.noRequestData')}
                  />
                )}
              </div>

              {/* Route Status Code Distribution */}
              <div className="chart-panel chart-panel--nested">
                <div className="chart-panel__title">{t('gatewayMonitoring.statusCodeDist', { range: rangeLabel })}</div>
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
                        <Bar dataKey="count" name={t('gatewayMonitoring.requests')}>
                          {(routeStatusQuery.data ?? []).map((entry, index) => (
                            <Cell key={index} fill={statusCodeColor(entry.code, chartColors)} />
                          ))}
                          <LabelList dataKey="count" position="top" style={{ fontSize: 10, fill: chartColors.axis }} />
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                ) : (
                  <PanelStatus
                    loading={routeStatusQuery.isLoading}
                    error={routeStatusQuery.isError}
                    emptyText={t('gatewayMonitoring.noStatusData')}
                  />
                )}
              </div>
            </>
          ) : routeSummaryQuery.isError ? null : (
            <div className="no-data">{t('gatewayMonitoring.noRouteDetailData')}</div>
          )}
        </div>
      )}

      {/* Per-route requests over time (bucketed) */}
      <BucketedBreakdownView
        title={t('breakdown.byRouteOverTime')}
        data={routesSeriesQuery.data}
        bucket={bucket}
        loading={routesSeriesQuery.isLoading}
        error={routesSeriesQuery.isError}
        unit="requests"
        valueFmt={(n) => Math.round(n).toLocaleString()}
      />

      {/* API Key Comparison — cross-key overview. When the page is scoped to
          a single key, a compact note explains why the comparison is gone
          instead of the panels silently disappearing. */}
      {selectedConsumer ? (
        <div className="chart-panel chart-panel--note">
          <div className="chart-panel__title">{t('gatewayMonitoring.apiKeyComparison', { range: rangeLabel })}</div>
          <div className="no-data no-data--compact">
            {t('gatewayMonitoring.filteredNote', { key: selectedConsumer })}
          </div>
        </div>
      ) : (
        <>
      <div className="chart-panel">
        <div className="chart-panel__title">{t('gatewayMonitoring.apiKeyComparison', { range: rangeLabel })}</div>
        {(consumersComparisonQuery.data?.consumers ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table comparison-table">
              <thead>
                <tr>
                  <SortableHeader column="consumer"        label={t('gatewayMonitoring.apiKey')}     activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="requests"        label={t('gatewayMonitoring.requests')}   align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="share"           label={t('gatewayMonitoring.share')}      align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="error_rate"      label={t('gatewayMonitoring.errorRate')}  align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="latency_p50_ms"  label={t('gatewayMonitoring.latencyP50')} align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="latency_p95_ms"  label={t('gatewayMonitoring.latencyP95')} align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                </tr>
              </thead>
              <tbody>
                {sortedConsumers.map((c) => (
                  <tr key={c.consumer}>
                    <td className="cell-alias" title={apiKeyDescriptions[c.consumer] || undefined}>{c.consumer}</td>
                    <td className="cell-metric"><BarCell value={c.requests} max={maxConsumerRequests} /></td>
                    <td className="cell-metric"><BarCell value={c.share} max={100} suffix="%" /></td>
                    <td className={`cell-metric ${errorRateClass(c.error_rate)}`}>{c.error_rate.toFixed(2)}%</td>
                    <td className="cell-metric">{c.latency_p50_ms == null ? '—' : <BarCell value={c.latency_p50_ms} max={maxConsumerP50} />}</td>
                    <td className="cell-metric">{c.latency_p95_ms == null ? '—' : <BarCell value={c.latency_p95_ms} max={maxConsumerP95} />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <PanelStatus
            loading={consumersComparisonQuery.isLoading}
            error={consumersComparisonQuery.isError}
            emptyText={t('gatewayMonitoring.noApiKeyData')}
          />
        )}
      </div>

      {/* Per-API-key requests over time (bucketed) — same cross-key scope */}
      <BucketedBreakdownView
        title={t('breakdown.byConsumerOverTime')}
        data={consumersSeriesQuery.data}
        bucket={bucket}
        loading={consumersSeriesQuery.isLoading}
        error={consumersSeriesQuery.isError}
        unit="requests"
        valueFmt={(n) => Math.round(n).toLocaleString()}
      />
        </>
      )}

    </div>
  );
}

export default GatewayMonitoring;
