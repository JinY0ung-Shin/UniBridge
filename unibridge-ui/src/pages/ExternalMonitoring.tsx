import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend, Cell, LabelList,
} from 'recharts';
import {
  getExternalSummary,
  getExternalRequests,
  getExternalRequestsTotal,
  getExternalStatusCodes,
  getExternalLatency,
  getExternalServicesComparison,
  getExternalServicesComparisonSeries,
} from '../api/client';
import { useChartTheme, statusCodeColor } from '../components/useChartTheme';
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

type ServiceSortColumn = 'service' | 'requests' | 'share' | 'error_rate' | 'latency_p50_ms' | 'latency_p95_ms';

/**
 * Traffic stats for API services monitored WITHOUT gateway onboarding —
 * services registered on the Servers page that expose RED metrics per
 * docs/api-metrics-convention.md. Mirrors the gateway monitoring page; there
 * is no per-API-key axis here because auth is not handled by UniBridge.
 */
function ExternalMonitoring() {
  const { t } = useTranslation();
  const [selection, setSelection] = useState<TimeSelection>({ kind: 'preset', value: '1h' });
  const selKey = selectionKey(selection);
  const span = selectionSpanSeconds(selection);
  const refetchInterval = selection.kind === 'custom' ? false : 30_000;
  const rangeLabel = selection.kind === 'preset' ? selection.value : t('externalMonitoring.customRange');
  const [selectedService, setSelectedService] = useState<string>('');
  const [bucket, setBucket] = useState<Bucket>('auto');
  const [sort, setSort] = useState<SortState<ServiceSortColumn>>({ column: 'requests', dir: 'desc' });
  const chartColors = useChartTheme();
  const volumeLabel = (ts: number) =>
    bucket === 'auto' ? formatChartTimestamp(ts, span) : formatBucketLabel(ts, bucket);

  const toggleSort = (column: ServiceSortColumn) => setSort((prev) => toggleSortState(prev, column));

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

  const serviceFilter = selectedService || undefined;

  const summaryQuery = useQuery({
    queryKey: ['ext-summary', selKey, selectedService],
    queryFn: () => getExternalSummary(selection, serviceFilter),
    refetchInterval,
  });

  const requestsQuery = useQuery({
    queryKey: ['ext-requests', selKey, selectedService],
    queryFn: () => getExternalRequests(selection, serviceFilter),
    refetchInterval,
  });

  const requestsTotalQuery = useQuery({
    queryKey: ['ext-requests-total', selKey, selectedService, bucketKey(bucket)],
    queryFn: () => getExternalRequestsTotal(selection, serviceFilter, bucket),
    refetchInterval,
  });

  const statusQuery = useQuery({
    queryKey: ['ext-status-codes', selKey, selectedService],
    queryFn: () => getExternalStatusCodes(selection, serviceFilter),
    refetchInterval,
  });

  const latencyQuery = useQuery({
    queryKey: ['ext-latency', selKey, selectedService],
    queryFn: () => getExternalLatency(selection, serviceFilter),
    refetchInterval,
  });

  // Always fetched (cheap, top-10): also feeds the service filter options, so
  // the dropdown keeps working while the comparison panel itself is hidden.
  const comparisonQuery = useQuery({
    queryKey: ['ext-services-comparison', selKey],
    queryFn: () => getExternalServicesComparison(selection),
    refetchInterval,
  });

  const comparisonSeriesQuery = useQuery({
    queryKey: ['ext-services-comparison-series', selKey, bucketKey(bucket)],
    queryFn: () => getExternalServicesComparisonSeries(selection, bucket),
    refetchInterval,
    enabled: bucket !== 'auto' && !selectedService,
  });

  const serviceOptions = useMemo(() => {
    const names = (comparisonQuery.data?.services ?? []).map((s) => s.service);
    if (selectedService && !names.includes(selectedService)) names.push(selectedService);
    return [...new Set(names)].sort((a, b) => a.localeCompare(b));
  }, [comparisonQuery.data, selectedService]);

  const sortedServices = useMemo(() => {
    const rows = comparisonQuery.data?.services ?? [];
    return sortRows(rows, sort, (row, column) => (column === 'service' ? row.service : row[column]));
  }, [comparisonQuery.data, sort]);

  const maxRequests = useMemo(() => {
    const rows = comparisonQuery.data?.services ?? [];
    return rows.reduce((m, r) => (r.requests > m ? r.requests : m), 0);
  }, [comparisonQuery.data]);

  const { maxP50, maxP95 } = useMemo(() => {
    const rows = comparisonQuery.data?.services ?? [];
    return {
      maxP50: rows.reduce((m, r) => (r.latency_p50_ms != null && r.latency_p50_ms > m ? r.latency_p50_ms : m), 0),
      maxP95: rows.reduce((m, r) => (r.latency_p95_ms != null && r.latency_p95_ms > m ? r.latency_p95_ms : m), 0),
    };
  }, [comparisonQuery.data]);

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
    statusQuery.isError || latencyQuery.isError ||
    comparisonQuery.isError || comparisonSeriesQuery.isError
  );

  return (
    <div className="gateway-monitoring">
      <div className="page-header">
        <div>
          <h1>{t('externalMonitoring.title')}</h1>
          <p className="page-subtitle">{t('externalMonitoring.subtitle')}</p>
          <p className="page-meta">{t('monitoring.headerNote')}</p>
        </div>
        <div className="page-header__filters">
          <label className="api-key-filter">
            <span className="api-key-filter__label">{t('externalMonitoring.serviceFilter')}</span>
            <select
              className="api-key-filter__select"
              value={selectedService}
              onChange={(e) => setSelectedService(e.target.value)}
            >
              <option value="">{t('externalMonitoring.allServices')}</option>
              {serviceOptions.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </label>
          <TimeRangeSelector value={selection} onChange={handleSelectionChange} />
          <BucketSelector value={bucket} onChange={handleBucketChange} />
        </div>
      </div>

      {isLoading && <div className="loading-message" role="status">{t('externalMonitoring.loadingMetrics')}</div>}
      {isError && <div className="error-banner" role="alert">{t('externalMonitoring.loadFailed')}</div>}
      {hasPartialError && <div className="error-banner" role="alert">{t('externalMonitoring.partialLoadFailed')}</div>}

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

      {/* Request Rate */}
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

      {/* Request Count (per interval) */}
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

      {/* Service Comparison — hidden (with a note) while scoped to one service */}
      {selectedService ? (
        <div className="chart-panel chart-panel--note">
          <div className="chart-panel__title">{t('externalMonitoring.serviceComparison', { range: rangeLabel })}</div>
          <div className="no-data no-data--compact">
            {t('externalMonitoring.filteredNote', { key: selectedService })}
          </div>
        </div>
      ) : (
        <>
      <div className="chart-panel">
        <div className="chart-panel__title">{t('externalMonitoring.serviceComparison', { range: rangeLabel })}</div>
        {(comparisonQuery.data?.services ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table comparison-table">
              <thead>
                <tr>
                  <SortableHeader column="service"         label={t('externalMonitoring.service')}     activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="requests"        label={t('gatewayMonitoring.requests')}     align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="share"           label={t('gatewayMonitoring.share')}        align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="error_rate"      label={t('gatewayMonitoring.errorRate')}    align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="latency_p50_ms"  label={t('gatewayMonitoring.latencyP50')}   align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                  <SortableHeader column="latency_p95_ms"  label={t('gatewayMonitoring.latencyP95')}   align="right" activeColumn={sort.column} dir={sort.dir} onToggle={toggleSort} />
                </tr>
              </thead>
              <tbody>
                {sortedServices.map((r) => (
                  <tr key={r.service}>
                    <td className="cell-alias">{r.service}</td>
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
            loading={comparisonQuery.isLoading}
            error={comparisonQuery.isError}
            emptyText={t('externalMonitoring.noServiceData')}
          />
        )}
      </div>

      {/* Per-service requests over time (bucketed) */}
      <BucketedBreakdownView
        title={t('breakdown.byServiceOverTime')}
        data={comparisonSeriesQuery.data}
        bucket={bucket}
        loading={comparisonSeriesQuery.isLoading}
        error={comparisonSeriesQuery.isError}
        unit="requests"
        valueFmt={(n) => Math.round(n).toLocaleString()}
      />
        </>
      )}
    </div>
  );
}

export default ExternalMonitoring;
