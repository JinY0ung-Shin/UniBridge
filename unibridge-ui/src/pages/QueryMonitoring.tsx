import { useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import {
  getQueryMetricsSummary,
  getQueryMetricsTotal,
  getQueryMetricsOutcomes,
  getQueryMetricsLatency,
  getQueryDatabasesComparison,
  getQueryConsumersComparison,
  getQueryDatabasesSeries,
  getQueryConsumersSeries,
  getApiKeys,
} from '../api/client';
import { useChartTheme } from '../components/useChartTheme';
import { usePermissions } from '../components/usePermissions';
import BucketedBreakdownView from '../components/BucketedBreakdownView';
import PanelStatus from '../components/PanelStatus';
import SortableHeader from '../components/SortableHeader';
import { type SortState, toggleSortState, sortRows } from '../utils/tableSort';
import './Monitoring.css';
import './GatewayMonitoring.css';
import TimeRangeSelector from '../components/TimeRangeSelector';
import BucketSelector from '../components/BucketSelector';
import { type TimeSelection, type Bucket, selectionKey, selectionSpanSeconds, bucketKey, periodForBucket, bucketTooCoarse, GRAFANA_BUCKET_INTERVAL } from '../utils/timeRange';
import { formatChartTimestamp, formatBucketLabel } from '../utils/time';
import { isolatedPointDot } from '../utils/chartDots';
import { errorRateColor, QUERY_UI_CONSUMER, QUERY_UNTRACKED_CONSUMER } from '../utils/monitoring';
import GrafanaLink from '../components/GrafanaLink';

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

function formatRows(value: number | null): string {
  return value == null ? '—' : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function maxOf<T>(rows: T[], pick: (row: T) => number | null): number {
  return rows.reduce((m, r) => {
    const v = pick(r);
    return v != null && v > m ? v : m;
  }, 0);
}

type DatabaseSortColumn = 'database' | 'db_type' | 'queries' | 'share' | 'error_rate' | 'avg_latency_ms' | 'latency_p95_ms' | 'avg_rows';
type ConsumerSortColumn = 'consumer' | 'queries' | 'share' | 'error_rate' | 'avg_latency_ms' | 'latency_p95_ms' | 'avg_rows';

/**
 * Per-database × per-API-key query stats from the unibridge_query_* metrics
 * (every /query/execute and template run, recorded in-app). The two filters
 * cross-filter: the DB table and its breakdown honor only the API-key filter,
 * the API-key table and its breakdown honor only the DB filter, and the cards
 * and charts honor both — so either table stays a full list with the active
 * row highlighted, and clicking a row toggles that filter.
 */
function QueryMonitoring() {
  const { t } = useTranslation();
  const { permissions, loaded: permissionsLoaded } = usePermissions();
  const [selection, setSelection] = useState<TimeSelection>({ kind: 'preset', value: '1h' });
  const selKey = selectionKey(selection);
  const span = selectionSpanSeconds(selection);
  const refetchInterval = selection.kind === 'custom' ? false : 30_000;
  const rangeLabel = selection.kind === 'preset' ? selection.value : t('queryMonitoring.customRange');
  const [selectedConsumer, setSelectedConsumer] = useState<string>('');
  const [selectedDatabase, setSelectedDatabase] = useState<string>('');
  const [bucket, setBucket] = useState<Bucket>('auto');
  const [dbSort, setDbSort] = useState<SortState<DatabaseSortColumn>>({ column: 'queries', dir: 'desc' });
  const [consumerSort, setConsumerSort] = useState<SortState<ConsumerSortColumn>>({ column: 'queries', dir: 'desc' });
  const pageHeaderRef = useRef<HTMLDivElement | null>(null);
  const chartColors = useChartTheme();
  const volumeLabel = (ts: number) =>
    bucket === 'auto' ? formatChartTimestamp(ts, span) : formatBucketLabel(ts, bucket);

  const toggleDbSort = (column: DatabaseSortColumn) => setDbSort((prev) => toggleSortState(prev, column));
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

  const canReadApiKeys = permissionsLoaded && permissions.includes('apikeys.read');
  // The backend forces self-scoped viewers onto their own key, so the key
  // filter (and key-row filtering) would be a no-op for them.
  const selfScopeOnly = permissionsLoaded
    && !permissions.includes('gateway.monitoring.read')
    && permissions.includes('gateway.monitoring.self');
  // Shown to every other viewer (not only key admins): key rows are clickable
  // for all of them, so the active key filter must always be visible and
  // clearable here, even after its row drops out of the table.
  const showApiKeyFilter = permissionsLoaded && !selfScopeOnly;

  const consumerFilter = selectedConsumer || undefined;
  const databaseFilter = selectedDatabase || undefined;

  const apiKeysQuery = useQuery({
    queryKey: ['api-keys', 'query-monitoring-filter'],
    queryFn: getApiKeys,
    staleTime: 5 * 60 * 1000,
    refetchInterval: false,
    enabled: showApiKeyFilter && canReadApiKeys,
  });

  const summaryQuery = useQuery({
    queryKey: ['query-metrics-summary', selKey, selectedConsumer, selectedDatabase],
    queryFn: () => getQueryMetricsSummary(selection, consumerFilter, databaseFilter),
    refetchInterval,
  });

  const totalQuery = useQuery({
    queryKey: ['query-metrics-total', selKey, selectedConsumer, selectedDatabase, bucketKey(bucket)],
    queryFn: () => getQueryMetricsTotal(selection, consumerFilter, databaseFilter, bucket),
    refetchInterval,
  });

  const outcomesQuery = useQuery({
    queryKey: ['query-metrics-outcomes', selKey, selectedConsumer, selectedDatabase, bucketKey(bucket)],
    queryFn: () => getQueryMetricsOutcomes(selection, consumerFilter, databaseFilter, bucket),
    refetchInterval,
  });

  const latencyQuery = useQuery({
    queryKey: ['query-metrics-latency', selKey, selectedConsumer, selectedDatabase],
    queryFn: () => getQueryMetricsLatency(selection, consumerFilter, databaseFilter),
    refetchInterval,
  });

  // Always fetched: also feeds the DB filter options, so the dropdown lists
  // the databases that actually saw traffic without an admin-only DB listing.
  const databasesQuery = useQuery({
    queryKey: ['query-metrics-databases', selKey, selectedConsumer],
    queryFn: () => getQueryDatabasesComparison(selection, consumerFilter),
    refetchInterval,
  });

  const consumersQuery = useQuery({
    queryKey: ['query-metrics-consumers', selKey, selectedDatabase],
    queryFn: () => getQueryConsumersComparison(selection, databaseFilter),
    refetchInterval,
  });

  const databasesSeriesQuery = useQuery({
    queryKey: ['query-metrics-databases-series', selKey, selectedConsumer, bucketKey(bucket)],
    queryFn: () => getQueryDatabasesSeries(selection, consumerFilter, bucket),
    refetchInterval,
    enabled: bucket !== 'auto',
  });

  const consumersSeriesQuery = useQuery({
    queryKey: ['query-metrics-consumers-series', selKey, selectedDatabase, bucketKey(bucket)],
    queryFn: () => getQueryConsumersSeries(selection, databaseFilter, bucket),
    refetchInterval,
    enabled: bucket !== 'auto',
  });

  const apiKeyDescriptions = useMemo(() => {
    const map: Record<string, string> = {};
    for (const k of apiKeysQuery.data ?? []) {
      if (k.description) map[k.name] = k.description;
    }
    return map;
  }, [apiKeysQuery.data]);

  // Registered keys (key admins only) ∪ keys seen in the comparison table ∪
  // the current selection — so a filter set from a row stays listed (and
  // clearable) even once that key has no traffic in the new window/DB.
  const apiKeyOptions = useMemo(() => {
    const names = (apiKeysQuery.data ?? []).map((k) => k.name);
    for (const c of consumersQuery.data?.consumers ?? []) names.push(c.consumer);
    if (selectedConsumer) names.push(selectedConsumer);
    return [...new Set(names)]
      .filter((name) => name !== QUERY_UI_CONSUMER && name !== QUERY_UNTRACKED_CONSUMER)
      .sort((a, b) => a.localeCompare(b));
  }, [apiKeysQuery.data, consumersQuery.data, selectedConsumer]);

  const databaseOptions = useMemo(() => {
    const names = (databasesQuery.data?.databases ?? []).map((d) => d.database);
    if (selectedDatabase) names.push(selectedDatabase);
    return [...new Set(names)].sort((a, b) => a.localeCompare(b));
  }, [databasesQuery.data, selectedDatabase]);

  const sortedDatabases = useMemo(() => {
    const rows = databasesQuery.data?.databases ?? [];
    return sortRows(rows, dbSort, (row, column) => row[column]);
  }, [databasesQuery.data, dbSort]);

  const sortedConsumers = useMemo(() => {
    const rows = consumersQuery.data?.consumers ?? [];
    return sortRows(rows, consumerSort, (row, column) => row[column]);
  }, [consumersQuery.data, consumerSort]);

  const dbMax = useMemo(() => {
    const rows = databasesQuery.data?.databases ?? [];
    return {
      queries: maxOf(rows, (r) => r.queries),
      avg: maxOf(rows, (r) => r.avg_latency_ms),
      p95: maxOf(rows, (r) => r.latency_p95_ms),
    };
  }, [databasesQuery.data]);

  const consumerMax = useMemo(() => {
    const rows = consumersQuery.data?.consumers ?? [];
    return {
      queries: maxOf(rows, (r) => r.queries),
      avg: maxOf(rows, (r) => r.avg_latency_ms),
      p95: maxOf(rows, (r) => r.latency_p95_ms),
    };
  }, [consumersQuery.data]);

  const consumerLabel = (consumer: string) => {
    if (consumer === QUERY_UI_CONSUMER) return t('breakdown.uiQueries');
    if (consumer === QUERY_UNTRACKED_CONSUMER) return t('breakdown.untracked');
    return consumer;
  };

  // A row click that sets a filter changes the cards and charts at the top of
  // the page, so bring them into view; clearing the filter stays in place.
  function revealFilteredView() {
    pageHeaderRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  }

  function toggleDatabase(database: string) {
    const next = selectedDatabase === database ? '' : database;
    setSelectedDatabase(next);
    if (next) revealFilteredView();
  }

  function toggleConsumer(consumer: string) {
    const next = selectedConsumer === consumer ? '' : consumer;
    setSelectedConsumer(next);
    if (next) revealFilteredView();
  }

  function handleRowKeyDown(event: KeyboardEvent<HTMLTableRowElement>, toggle: () => void) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggle();
    }
  }

  const summary = summaryQuery.data;
  const totalData = (totalQuery.data ?? []).map((p) => ({
    time: volumeLabel(p.timestamp),
    queries: Math.round(p.value),
  }));
  const outcomeData = (outcomesQuery.data ?? []).map((p) => ({
    time: volumeLabel(p.timestamp),
    success: p.success,
    error: p.error,
    timeout: p.timeout,
  }));

  // Merge the percentile series by timestamp. Steps without successful
  // queries arrive as null (and a series may omit a step entirely); both stay
  // null so the lines show gaps instead of misleading dips to zero.
  // The dot renderers read the merged rows, so they are built alongside them.
  // The span is derived from `selection` here: the outer `span` is the result
  // of a plain call, which the React Compiler cannot treat as a stable dep.
  const { latencyChartData, latencyDots } = useMemo(() => {
    const latencySpan = selectionSpanSeconds(selection);
    type Row = { p50: number | null; p95: number | null; p99: number | null };
    const byTimestamp = new Map<number, Row>();
    for (const key of ['p50', 'p95', 'p99'] as const) {
      for (const point of latencyQuery.data?.[key] ?? []) {
        const row = byTimestamp.get(point.timestamp) ?? { p50: null, p95: null, p99: null };
        row[key] = point.value;
        byTimestamp.set(point.timestamp, row);
      }
    }
    const rows = [...byTimestamp.entries()]
      .sort(([a], [b]) => a - b)
      .map(([timestamp, row]) => ({ time: formatChartTimestamp(timestamp, latencySpan), ...row }));
    return {
      latencyChartData: rows,
      latencyDots: {
        p50: isolatedPointDot(rows, 'p50'),
        p95: isolatedPointDot(rows, 'p95'),
        p99: isolatedPointDot(rows, 'p99'),
      },
    };
  }, [latencyQuery.data, selection]);
  const hasLatencyData = latencyChartData.some(
    (row) => row.p50 != null || row.p95 != null || row.p99 != null,
  );

  const isLoading = summaryQuery.isLoading;
  const isError = summaryQuery.isError;
  const hasPartialError = !isError && (
    totalQuery.isError || outcomesQuery.isError || latencyQuery.isError ||
    databasesQuery.isError || consumersQuery.isError ||
    databasesSeriesQuery.isError || consumersSeriesQuery.isError
  );

  const tooltipProps = {
    contentStyle: { background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 },
    labelStyle: { color: chartColors.axis },
    itemStyle: { color: chartColors.textSecondary },
  };

  return (
    <div className="gateway-monitoring">
      <div className="page-header" ref={pageHeaderRef}>
        <div>
          <h1>{t('queryMonitoring.title')}</h1>
          <p className="page-subtitle">{t('queryMonitoring.subtitle')}</p>
          <p className="page-meta">{t('monitoring.headerNote')}</p>
          {selfScopeOnly && <span className="scope-note">{t('queryMonitoring.selfScopeNote')}</span>}
        </div>
        <div className="page-header__filters">
          {/* Grafana reads Prometheus directly (no per-key scoping), so don't
              offer it to viewers the backend restricts to their own key. */}
          {!selfScopeOnly && (
            <GrafanaLink
              dashboard="unibridge-queries"
              time={selection}
              vars={{
                'var-consumer': selectedConsumer,
                'var-database': selectedDatabase,
                'var-bucket': GRAFANA_BUCKET_INTERVAL[bucket],
              }}
            />
          )}
          {showApiKeyFilter && (
            <label className="api-key-filter">
              <span className="api-key-filter__label">{t('queryMonitoring.apiKeyFilter')}</span>
              <select
                className="api-key-filter__select"
                value={selectedConsumer}
                onChange={(e) => setSelectedConsumer(e.target.value)}
              >
                <option value="">{t('queryMonitoring.allApiKeys')}</option>
                <option value={QUERY_UI_CONSUMER}>{t('breakdown.uiQueries')}</option>
                {apiKeyOptions.map((name) => (
                  <option key={name} value={name} title={apiKeyDescriptions[name] || undefined}>{name}</option>
                ))}
              </select>
            </label>
          )}
          <label className="api-key-filter">
            <span className="api-key-filter__label">{t('queryMonitoring.databaseFilter')}</span>
            <select
              className="api-key-filter__select"
              value={selectedDatabase}
              onChange={(e) => setSelectedDatabase(e.target.value)}
            >
              <option value="">{t('queryMonitoring.allDatabases')}</option>
              {databaseOptions.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </label>
          <TimeRangeSelector value={selection} onChange={handleSelectionChange} />
          <BucketSelector value={bucket} onChange={handleBucketChange} />
        </div>
      </div>

      {isLoading && <div className="loading-message" role="status">{t('queryMonitoring.loadingMetrics')}</div>}
      {isError && <div className="error-banner" role="alert">{t('queryMonitoring.loadFailed')}</div>}
      {hasPartialError && <div className="error-banner" role="alert">{t('queryMonitoring.partialLoadFailed')}</div>}

      {/* Summary Cards */}
      {summary && (
        <div className="metric-cards">
          <div className="metric-card">
            <div className="metric-card__value">{summary.total_queries.toLocaleString()}</div>
            <div className="metric-card__label">{t('queryMonitoring.totalQueries', { range: rangeLabel })}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value" style={{ color: errorRateColor(summary.error_rate) }}>
              {summary.error_rate}%
            </div>
            <div className="metric-card__label">{t('queryMonitoring.errorRate')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{summary.timeouts.toLocaleString()}</div>
            <div className="metric-card__label">{t('queryMonitoring.timeouts')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">
              {summary.avg_latency_ms == null ? '—' : `${summary.avg_latency_ms}ms`}
            </div>
            <div className="metric-card__label">{t('queryMonitoring.avgLatency')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">
              {summary.p95_latency_ms == null ? '—' : `${summary.p95_latency_ms}ms`}
            </div>
            <div className="metric-card__label">{t('queryMonitoring.p95Latency')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{formatRows(summary.avg_rows)}</div>
            <div className="metric-card__label">{t('queryMonitoring.avgRows')}</div>
          </div>
        </div>
      )}

      {/* Query Count (per interval) */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('queryMonitoring.queryVolume')}</div>
        {totalData.length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <BarChart data={totalData}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} minTickGap={24} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip {...tooltipProps} />
                <Bar dataKey="queries" fill={chartColors.blue} name={t('queryMonitoring.queries')} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <PanelStatus
            loading={totalQuery.isLoading}
            error={totalQuery.isError}
            emptyText={t('queryMonitoring.noQueryData')}
          />
        )}
      </div>

      {/* Outcomes (per interval) */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('queryMonitoring.outcomes')}</div>
        {outcomeData.length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <BarChart data={outcomeData}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} minTickGap={24} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip {...tooltipProps} />
                <Legend wrapperStyle={{ color: chartColors.axis, fontSize: 11 }} />
                <Bar dataKey="success" stackId="outcome" fill={chartColors.green} name={t('queryMonitoring.success')} />
                <Bar dataKey="error" stackId="outcome" fill={chartColors.red} name={t('queryMonitoring.error')} />
                <Bar dataKey="timeout" stackId="outcome" fill={chartColors.yellow} name={t('queryMonitoring.timeout')} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <PanelStatus
            loading={outcomesQuery.isLoading}
            error={outcomesQuery.isError}
            emptyText={t('queryMonitoring.noQueryData')}
          />
        )}
      </div>

      {/* Execution time */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('queryMonitoring.latency')}</div>
        <div className="chart-panel__caption">{t('queryMonitoring.latencyCaption')}</div>
        {hasLatencyData ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <LineChart data={latencyChartData}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} minTickGap={24} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip {...tooltipProps} />
                <Legend wrapperStyle={{ color: chartColors.axis, fontSize: 11 }} />
                {/* From 6h ranges the rate window spans a whole step, so sparse
                    traffic leaves lone points between gaps; draw those as dots. */}
                <Line type="monotone" dataKey="p50" stroke={chartColors.green} strokeWidth={2} dot={latencyDots.p50} name="P50" />
                <Line type="monotone" dataKey="p95" stroke={chartColors.yellow} strokeWidth={2} dot={latencyDots.p95} name="P95" />
                <Line type="monotone" dataKey="p99" stroke={chartColors.red} strokeWidth={2} dot={latencyDots.p99} name="P99" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <PanelStatus
            loading={latencyQuery.isLoading}
            error={latencyQuery.isError}
            emptyText={t('queryMonitoring.noLatencyData')}
          />
        )}
      </div>

      {/* Database comparison — honors the API-key filter only */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('queryMonitoring.databaseComparison', { range: rangeLabel })}</div>
        <div className="chart-panel__caption">{t('queryMonitoring.comparisonCaption')}</div>
        {sortedDatabases.length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table comparison-table">
              <thead>
                <tr>
                  <SortableHeader column="database"       label={t('queryMonitoring.database')}         activeColumn={dbSort.column} dir={dbSort.dir} onToggle={toggleDbSort} />
                  <SortableHeader column="db_type"        label={t('queryMonitoring.dbType')}           activeColumn={dbSort.column} dir={dbSort.dir} onToggle={toggleDbSort} />
                  <SortableHeader column="queries"        label={t('queryMonitoring.queries')}          align="right" activeColumn={dbSort.column} dir={dbSort.dir} onToggle={toggleDbSort} />
                  <SortableHeader column="share"          label={t('queryMonitoring.share')}            align="right" activeColumn={dbSort.column} dir={dbSort.dir} onToggle={toggleDbSort} />
                  <SortableHeader column="error_rate"     label={t('queryMonitoring.errorRateColumn')}  align="right" activeColumn={dbSort.column} dir={dbSort.dir} onToggle={toggleDbSort} />
                  <SortableHeader column="avg_latency_ms" label={t('queryMonitoring.avgLatencyColumn')} align="right" activeColumn={dbSort.column} dir={dbSort.dir} onToggle={toggleDbSort} />
                  <SortableHeader column="latency_p95_ms" label={t('queryMonitoring.p95LatencyColumn')} align="right" activeColumn={dbSort.column} dir={dbSort.dir} onToggle={toggleDbSort} />
                  <SortableHeader column="avg_rows"       label={t('queryMonitoring.avgRowsColumn')}    align="right" activeColumn={dbSort.column} dir={dbSort.dir} onToggle={toggleDbSort} />
                </tr>
              </thead>
              <tbody>
                {sortedDatabases.map((r) => (
                  <tr
                    key={r.database}
                    className={`route-row ${selectedDatabase === r.database ? 'route-row--selected' : ''}`}
                    onClick={() => toggleDatabase(r.database)}
                    onKeyDown={(event) => handleRowKeyDown(event, () => toggleDatabase(r.database))}
                    tabIndex={0}
                    role="button"
                    aria-pressed={selectedDatabase === r.database}
                    aria-label={t('queryMonitoring.filterDatabase', { database: r.database })}
                  >
                    <td className="cell-alias">{r.database}</td>
                    <td>{r.db_type ?? '—'}</td>
                    <td className="cell-metric"><BarCell value={r.queries} max={dbMax.queries} /></td>
                    <td className="cell-metric"><BarCell value={r.share} max={100} suffix="%" /></td>
                    <td className={`cell-metric ${errorRateClass(r.error_rate)}`}>{r.error_rate.toFixed(2)}%</td>
                    <td className="cell-metric">{r.avg_latency_ms == null ? '—' : <BarCell value={r.avg_latency_ms} max={dbMax.avg} />}</td>
                    <td className="cell-metric">{r.latency_p95_ms == null ? '—' : <BarCell value={r.latency_p95_ms} max={dbMax.p95} />}</td>
                    <td className="cell-metric">{formatRows(r.avg_rows)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <PanelStatus
            loading={databasesQuery.isLoading}
            error={databasesQuery.isError}
            emptyText={t('queryMonitoring.noDatabaseData')}
          />
        )}
      </div>

      {/* Per-database queries over time (bucketed) */}
      <BucketedBreakdownView
        title={t('breakdown.byDatabaseOverTime')}
        data={databasesSeriesQuery.data}
        bucket={bucket}
        loading={databasesSeriesQuery.isLoading}
        error={databasesSeriesQuery.isError}
        unit="queries"
        valueFmt={(n) => Math.round(n).toLocaleString()}
      />

      {/* API key comparison — honors the DB filter only */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('queryMonitoring.apiKeyComparison', { range: rangeLabel })}</div>
        <div className="chart-panel__caption">{t('queryMonitoring.comparisonCaption')}</div>
        {sortedConsumers.length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table comparison-table">
              <thead>
                <tr>
                  <SortableHeader column="consumer"       label={t('queryMonitoring.apiKey')}           activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="queries"        label={t('queryMonitoring.queries')}          align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="share"          label={t('queryMonitoring.share')}            align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="error_rate"     label={t('queryMonitoring.errorRateColumn')}  align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="avg_latency_ms" label={t('queryMonitoring.avgLatencyColumn')} align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="latency_p95_ms" label={t('queryMonitoring.p95LatencyColumn')} align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                  <SortableHeader column="avg_rows"       label={t('queryMonitoring.avgRowsColumn')}    align="right" activeColumn={consumerSort.column} dir={consumerSort.dir} onToggle={toggleConsumerSort} />
                </tr>
              </thead>
              <tbody>
                {sortedConsumers.map((c) => {
                  const label = consumerLabel(c.consumer);
                  const cells = (
                    <>
                      <td
                        className="cell-alias"
                        title={
                          c.consumer === QUERY_UNTRACKED_CONSUMER
                            ? t('queryMonitoring.untrackedHint')
                            : apiKeyDescriptions[c.consumer] || undefined
                        }
                      >
                        {label}
                      </td>
                      <td className="cell-metric"><BarCell value={c.queries} max={consumerMax.queries} /></td>
                      <td className="cell-metric"><BarCell value={c.share} max={100} suffix="%" /></td>
                      <td className={`cell-metric ${errorRateClass(c.error_rate)}`}>{c.error_rate.toFixed(2)}%</td>
                      <td className="cell-metric">{c.avg_latency_ms == null ? '—' : <BarCell value={c.avg_latency_ms} max={consumerMax.avg} />}</td>
                      <td className="cell-metric">{c.latency_p95_ms == null ? '—' : <BarCell value={c.latency_p95_ms} max={consumerMax.p95} />}</td>
                      <td className="cell-metric">{formatRows(c.avg_rows)}</td>
                    </>
                  );
                  // Pre-tracking rows have no key to filter on, and self-scoped
                  // viewers are already pinned to their own key.
                  if (selfScopeOnly || c.consumer === QUERY_UNTRACKED_CONSUMER) {
                    return <tr key={c.consumer}>{cells}</tr>;
                  }
                  return (
                    <tr
                      key={c.consumer}
                      className={`route-row ${selectedConsumer === c.consumer ? 'route-row--selected' : ''}`}
                      onClick={() => toggleConsumer(c.consumer)}
                      onKeyDown={(event) => handleRowKeyDown(event, () => toggleConsumer(c.consumer))}
                      tabIndex={0}
                      role="button"
                      aria-pressed={selectedConsumer === c.consumer}
                      aria-label={t('queryMonitoring.filterApiKey', { key: label })}
                    >
                      {cells}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <PanelStatus
            loading={consumersQuery.isLoading}
            error={consumersQuery.isError}
            emptyText={t('queryMonitoring.noApiKeyData')}
          />
        )}
      </div>

      {/* Per-API-key queries over time (bucketed) */}
      <BucketedBreakdownView
        title={t('breakdown.byQueryKeyOverTime')}
        data={consumersSeriesQuery.data}
        bucket={bucket}
        loading={consumersSeriesQuery.isLoading}
        error={consumersSeriesQuery.isError}
        unit="queries"
        valueFmt={(n) => Math.round(n).toLocaleString()}
        queryConsumerLabels
      />
    </div>
  );
}

export default QueryMonitoring;
