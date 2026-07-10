import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend, Cell, LabelList,
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
import PanelStatus from '../components/PanelStatus';
import SortableHeader from '../components/SortableHeader';
import { type SortState, toggleSortState, sortRows } from '../utils/tableSort';
import './Monitoring.css';
import './LlmMonitoring.css';
import TimeRangeSelector from '../components/TimeRangeSelector';
import BucketSelector from '../components/BucketSelector';
import GrafanaLink from '../components/GrafanaLink';
import { type TimeSelection, type Bucket, selectionKey, selectionSpanSeconds, bucketKey, periodForBucket, bucketTooCoarse, GRAFANA_BUCKET_INTERVAL } from '../utils/timeRange';
import { formatChartTimestamp, formatBucketLabel } from '../utils/time';

type ModelSortColumn = 'model' | 'input_tokens' | 'output_tokens' | 'cached_tokens' | 'tokens' | 'requests' | 'cost';
type KeySortColumn = 'api_key' | 'input_tokens' | 'output_tokens' | 'cached_tokens' | 'tokens' | 'requests';

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
  const [modelSort, setModelSort] = useState<SortState<ModelSortColumn>>({ column: 'tokens', dir: 'desc' });
  const [keySort, setKeySort] = useState<SortState<KeySortColumn>>({ column: 'tokens', dir: 'desc' });
  const volumeLabel = (ts: number) =>
    bucket === 'auto' ? formatChartTimestamp(ts, span) : formatBucketLabel(ts, bucket);
  const chartColors = useChartTheme();

  const toggleModelSort = (column: ModelSortColumn) => setModelSort((prev) => toggleSortState(prev, column));
  const toggleKeySort = (column: KeySortColumn) => setKeySort((prev) => toggleSortState(prev, column));

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

  const sortedModels = useMemo(() => {
    const rows = byModelQuery.data ?? [];
    return sortRows(rows, modelSort, (row, column) => (column === 'model' ? row.model : row[column]));
  }, [byModelQuery.data, modelSort]);

  const sortedKeys = useMemo(() => {
    const rows = topKeysQuery.data ?? [];
    return sortRows(rows, keySort, (row, column) => (column === 'api_key' ? row.api_key : row[column]));
  }, [topKeysQuery.data, keySort]);

  const summary = summaryQuery.data;
  const tokenData = tokensQuery.data;
  const tokenChartData = (tokenData?.prompt ?? []).map((p, i) => ({
    time: volumeLabel(p.timestamp),
    prompt: Math.round(p.value),
    completion: Math.round(tokenData?.completion?.[i]?.value ?? 0),
    cached: Math.round(tokenData?.cached?.[i]?.value ?? 0),
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
          <p className="page-meta">{t('monitoring.headerNote')}</p>
        </div>
        <div className="page-header__filters">
          <GrafanaLink
            dashboard="unibridge-llm"
            time={selection}
            vars={{
              'var-api_key': selectedKey,
              'var-bucket': GRAFANA_BUCKET_INTERVAL[bucket],
            }}
          />
          <a
            href={LITELLM_ADMIN_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="admin-link-btn"
            aria-label={`${t('llmMonitoring.adminDashboard')} ${t('common.opensInNewTab')}`}
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
          <TimeRangeSelector value={selection} onChange={handleSelectionChange} />
          <BucketSelector value={bucket} onChange={handleBucketChange} />
        </div>
      </div>

      {isLoading && <div className="loading-message" role="status">{t('llmMonitoring.loadingMetrics')}</div>}
      {isError && <div className="error-banner" role="alert">{t('llmMonitoring.loadFailed')}</div>}
      {hasPartialError && <div className="error-banner" role="alert">{t('llmMonitoring.partialLoadFailed')}</div>}

      {/* Summary Cards */}
      {summary && (
        <div className="metric-cards">
          <div className="metric-card">
            <div className="metric-card__value">{formatTokens(summary.total_tokens)}</div>
            <div className="metric-card__label">{t('llmMonitoring.totalTokens', { range: rangeLabel })}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{formatTokens(summary.prompt_tokens)}</div>
            <div className="metric-card__label">{t('llmMonitoring.inputTokens')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{formatTokens(summary.completion_tokens)}</div>
            <div className="metric-card__label">{t('llmMonitoring.outputTokens')}</div>
          </div>
          <div className="metric-card">
            <div className="metric-card__value">{formatTokens(summary.cached_tokens)}</div>
            <div className="metric-card__label">{t('llmMonitoring.cachedCard')}</div>
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
          <div className="metric-card">
            <div className="metric-card__value">{`${((summary.cache_hit_rate ?? 0) * 100).toFixed(1)}%`}</div>
            <div className="metric-card__label">{t('llmMonitoring.cacheHitRate')}</div>
          </div>
        </div>
      )}

      {/* Token Usage Trend */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.tokenTrend')}</div>
        <p className="chart-panel__caption">{t('llmMonitoring.tokenChartCaption')}</p>
        {tokenChartData.length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <BarChart data={tokenChartData}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} minTickGap={24} />
                <YAxis stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                  labelStyle={{ color: chartColors.axis }}
                  itemStyle={{ color: chartColors.textSecondary }}
                />
                <Legend wrapperStyle={{ color: chartColors.axis, fontSize: 11 }} />
                <Bar dataKey="prompt" stackId="tokens" fill={chartColors.blue} name={t('llmMonitoring.prompt')} />
                <Bar dataKey="completion" stackId="tokens" fill={chartColors.green} name={t('llmMonitoring.completion')} />
                <Bar dataKey="cached" stackId="cached" fill={chartColors.yellow} name={t('llmMonitoring.cachedOfPrompt')} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <PanelStatus
            loading={tokensQuery.isLoading}
            error={tokensQuery.isError}
            emptyText={t('llmMonitoring.noTokenData')}
          />
        )}
      </div>

      {/* Request Volume */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.requestVolume')}</div>
        {(requestsTotalQuery.data ?? []).length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <BarChart data={(requestsTotalQuery.data ?? []).map((p) => ({ time: volumeLabel(p.timestamp), requests: Math.round(p.value) }))}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} minTickGap={24} />
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
          <PanelStatus
            loading={requestsTotalQuery.isLoading}
            error={requestsTotalQuery.isError}
            emptyText={t('llmMonitoring.noData')}
          />
        )}
      </div>

      {/* Usage by Model */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.byModel', { range: rangeLabel })}</div>
        {(byModelQuery.data ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table comparison-table">
              <thead>
                <tr>
                  <SortableHeader column="model"         label={t('llmMonitoring.model')}           activeColumn={modelSort.column} dir={modelSort.dir} onToggle={toggleModelSort} />
                  <SortableHeader column="input_tokens"  label={t('llmMonitoring.inputTokens')}     align="right" activeColumn={modelSort.column} dir={modelSort.dir} onToggle={toggleModelSort} />
                  <SortableHeader column="output_tokens" label={t('llmMonitoring.outputTokens')}    align="right" activeColumn={modelSort.column} dir={modelSort.dir} onToggle={toggleModelSort} />
                  <SortableHeader column="cached_tokens" label={t('llmMonitoring.cached')}          align="right" activeColumn={modelSort.column} dir={modelSort.dir} onToggle={toggleModelSort} />
                  <SortableHeader column="tokens"        label={t('llmMonitoring.totalTokenShort')} align="right" activeColumn={modelSort.column} dir={modelSort.dir} onToggle={toggleModelSort} />
                  <SortableHeader column="requests"      label={t('llmMonitoring.requests')}        align="right" activeColumn={modelSort.column} dir={modelSort.dir} onToggle={toggleModelSort} />
                  <SortableHeader column="cost"          label={t('llmMonitoring.cost')}            align="right" activeColumn={modelSort.column} dir={modelSort.dir} onToggle={toggleModelSort} />
                </tr>
              </thead>
              <tbody>
                {sortedModels.map((m) => (
                  <tr key={m.model}>
                    <td className="cell-alias">{m.model}</td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(m.input_tokens)}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(m.output_tokens)}
                    </td>
                    <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                      {formatTokens(m.cached_tokens)}
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
          <PanelStatus
            loading={byModelQuery.isLoading}
            error={byModelQuery.isError}
            emptyText={t('llmMonitoring.noModelData')}
          />
        )}
      </div>

      {/* Usage by Model over time */}
      <BucketedBreakdownView
        title={t('breakdown.byModelOverTime')}
        data={byModelSeriesQuery.data}
        loading={byModelSeriesQuery.isLoading}
        error={byModelSeriesQuery.isError}
        bucket={bucket}
        unit="tokens"
        valueFmt={formatTokens}
      />

      {/* Top API Keys — cross-key overview. When scoped to a single key, a
          compact note explains why the comparison is gone instead of the
          panels silently disappearing. */}
      {selectedKey ? (
        <div className="chart-panel chart-panel--note">
          <div className="chart-panel__title">{t('llmMonitoring.topKeys', { range: rangeLabel })}</div>
          <div className="no-data no-data--compact">
            {t('llmMonitoring.filteredNote', { key: selectedKey })}
          </div>
        </div>
      ) : (
        <>
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.topKeys', { range: rangeLabel })}</div>
        {(topKeysQuery.data ?? []).length > 0 ? (
          <div className="table-container" style={{ border: 'none' }}>
            <table className="data-table comparison-table">
              <thead>
                <tr>
                  <SortableHeader column="api_key"       label={t('llmMonitoring.apiKey')}          activeColumn={keySort.column} dir={keySort.dir} onToggle={toggleKeySort} />
                  <SortableHeader column="input_tokens"  label={t('llmMonitoring.inputTokens')}     align="right" activeColumn={keySort.column} dir={keySort.dir} onToggle={toggleKeySort} />
                  <SortableHeader column="output_tokens" label={t('llmMonitoring.outputTokens')}    align="right" activeColumn={keySort.column} dir={keySort.dir} onToggle={toggleKeySort} />
                  <SortableHeader column="cached_tokens" label={t('llmMonitoring.cached')}          align="right" activeColumn={keySort.column} dir={keySort.dir} onToggle={toggleKeySort} />
                  <SortableHeader column="tokens"        label={t('llmMonitoring.totalTokenShort')} align="right" activeColumn={keySort.column} dir={keySort.dir} onToggle={toggleKeySort} />
                  <SortableHeader column="requests"      label={t('llmMonitoring.requests')}        align="right" activeColumn={keySort.column} dir={keySort.dir} onToggle={toggleKeySort} />
                </tr>
              </thead>
              <tbody>
                {sortedKeys.map((k) => (
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
                      {formatTokens(k.cached_tokens)}
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
          <PanelStatus
            loading={topKeysQuery.isLoading}
            error={topKeysQuery.isError}
            emptyText={t('llmMonitoring.noKeyData')}
          />
        )}
      </div>

      {/* Top API Keys over time — same cross-key scope */}
      <BucketedBreakdownView
        title={t('breakdown.byKeyOverTime')}
        data={topKeysSeriesQuery.data}
        loading={topKeysSeriesQuery.isLoading}
        error={topKeysSeriesQuery.isError}
        bucket={bucket}
        unit="tokens"
        valueFmt={formatTokens}
      />
        </>
      )}

      {/* Status Code Distribution */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.statusCodeDist', { range: rangeLabel })}</div>
        <p className="chart-panel__caption">{t('llmMonitoring.statusSourceCaption')}</p>
        {(statusCodesQuery.data ?? []).length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
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
                  <LabelList dataKey="count" position="top" style={{ fontSize: 10, fill: chartColors.axis }} />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <PanelStatus
            loading={statusCodesQuery.isLoading}
            error={statusCodesQuery.isError}
            emptyText={t('llmMonitoring.noStatusData')}
          />
        )}
      </div>

      {/* Success / Error Rate */}
      <div className="chart-panel">
        <div className="chart-panel__title">{t('llmMonitoring.errorRate')}</div>
        {errorChartData.length > 0 ? (
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <BarChart data={errorChartData}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="time" stroke={chartColors.axis} tick={{ fontSize: 11 }} minTickGap={24} />
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
          <PanelStatus
            loading={errorsQuery.isLoading}
            error={errorsQuery.isError}
            emptyText={t('llmMonitoring.noErrorData')}
          />
        )}
      </div>
    </div>
  );
}

export default LlmMonitoring;
