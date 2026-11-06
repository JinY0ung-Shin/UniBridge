import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import { AxiosError } from 'axios';
import { getServers, getServerMetrics, type ServerMetricSeries } from '../api/client';
import { useChartTheme, type ChartTheme } from '../components/useChartTheme';
import GrafanaLink from '../components/GrafanaLink';
import { isolatedPointDot } from '../utils/chartDots';
import { formatChartPointTime, formatChartTimestamp } from '../utils/time';
import './Connections.css';
import './Servers.css';

const DURATIONS: Array<{ key: string; step: string; seconds: number }> = [
  { key: '1h', step: '60s', seconds: 3600 },
  { key: '6h', step: '120s', seconds: 6 * 3600 },
  { key: '24h', step: '300s', seconds: 24 * 3600 },
];

/** The metrics endpoint's 502 detail when every Prometheus query failed. */
const PROMETHEUS_DOWN_DETAIL = 'Prometheus is unreachable';

interface ChartWindow {
  spanSeconds: number;
  stepSeconds: number;
  /** When the data was fetched (epoch seconds): the window's end. */
  endSeconds: number;
}

const METRIC_COLORS: Record<string, 'blue' | 'green' | 'yellow'> = {
  cpu: 'blue',
  mem: 'green',
  disk: 'yellow',
};

const METRIC_ORDER: Array<ServerMetricSeries['metric']> = ['cpu', 'mem', 'disk', 'gpu_util', 'gpu_mem'];

/** Metrics that return one series per device, so each line needs its own color. */
const PER_DEVICE_METRICS: ReadonlyArray<ServerMetricSeries['metric']> = ['disk', 'gpu_util', 'gpu_mem'];

function isGpuMetric(metric: ServerMetricSeries['metric']) {
  return metric === 'gpu_util' || metric === 'gpu_mem';
}

type ChartDatum = {
  timestamp: number;
  time: string;
  [key: string]: number | string | null;
};

interface ChartLine {
  key: string;
  name: string;
  model: string | null;
  color: string;
  totalKey: string;
  usedKey: string;
  availableKey: string;
  totalBytes: number | null;
  usedBytes: number | null;
  availableBytes: number | null;
}

interface ChartPanel {
  metric: ServerMetricSeries['metric'];
  data: ChartDatum[];
  lines: ChartLine[];
}

/** Distinct hues after the theme accents, so an 8-GPU box gets 8 different lines. */
const EXTRA_DEVICE_COLORS = [
  '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#6366f1', '#84cc16', '#06b6d4', '#a855f7',
];

function deviceLineColor(theme: ChartTheme, index: number) {
  const colors = [theme.yellow, theme.blue, theme.green, theme.red, ...EXTRA_DEVICE_COLORS];
  return colors[index % colors.length];
}

function stepSeconds(step: string): number {
  const n = Number.parseInt(step, 10);
  return Number.isFinite(n) && n > 0 ? n : 60;
}

const BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

function asFiniteNumber(value: number | string | null | undefined): number | null {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === 'string' && value.trim()) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function formatBytes(value: number | string | null | undefined) {
  const bytes = asFiniteNumber(value);
  if (bytes == null || bytes < 0) return null;
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < BYTE_UNITS.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${BYTE_UNITS[unitIndex]}`;
}

function formatDiskCapacityValue(
  usedBytes: number | string | null | undefined,
  totalBytes: number | string | null | undefined,
  t: (key: string, options?: Record<string, string>) => string,
) {
  const total = formatBytes(totalBytes);
  if (!total) return null;
  const used = formatBytes(usedBytes);
  if (!used) return t('servers.diskTotal', { total });
  return t('servers.diskCapacityValue', { used, total });
}

function buildChartPanel(
  metric: ServerMetricSeries['metric'],
  metricSeries: ServerMetricSeries[],
  theme: ChartTheme,
  win: ChartWindow,
): ChartPanel {
  const dataByTimestamp = new Map<number, ChartDatum>();
  const lines = metricSeries.map((s, index) => {
    const key = `value_${index}`;
    const totalKey = `total_${index}`;
    const usedKey = `used_${index}`;
    const availableKey = `available_${index}`;
    const name = metric === 'disk'
      ? (s.mountpoint ?? 'disk')
      : isGpuMetric(metric)
        ? `GPU ${s.gpu ?? index}`
        : metric;
    let totalBytes: number | null = null;
    let usedBytes: number | null = null;
    let availableBytes: number | null = null;
    for (const point of s.points) {
      let datum = dataByTimestamp.get(point.t);
      if (!datum) {
        datum = { timestamp: point.t, time: formatChartTimestamp(point.t, win.spanSeconds) };
        dataByTimestamp.set(point.t, datum);
      }
      datum[key] = point.v;
      datum[totalKey] = point.total_bytes ?? null;
      datum[usedKey] = point.used_bytes ?? null;
      datum[availableKey] = point.available_bytes ?? null;
      totalBytes = asFiniteNumber(point.total_bytes) ?? totalBytes;
      usedBytes = asFiniteNumber(point.used_bytes) ?? usedBytes;
      availableBytes = asFiniteNumber(point.available_bytes) ?? availableBytes;
    }
    return {
      key,
      name,
      model: isGpuMetric(metric) ? (s.gpu_model ?? null) : null,
      color: PER_DEVICE_METRICS.includes(metric) ? deviceLineColor(theme, index) : theme[METRIC_COLORS[metric]],
      totalKey,
      usedKey,
      availableKey,
      totalBytes,
      usedBytes,
      availableBytes,
    };
  });
  return {
    metric,
    lines,
    data: onStepGrid(
      Array.from(dataByTimestamp.values()).sort((a, b) => a.timestamp - b.timestamp),
      lines.map((line) => line.key),
      win,
    ),
  };
}

/**
 * Lay the samples on the full step grid of the window, from its start to its
 * end, with a null row for every step that has no sample. Prometheus omits
 * the steps where a series had no data (host down, exporter not scraped), and
 * the X axis is categorical, so without the grid an outage would be joined up
 * (or, at the start or end of the window, simply missing) and a 6h hole in a
 * 24h chart would take one slot. The chart does not connect nulls, so every
 * missing step shows as a break of its real width.
 *
 * The grid is anchored on a sample, so rows line up with Prometheus' own
 * evaluation times. If the browser clock is far off the server's, the window
 * edges are dropped and only the holes between samples are filled.
 */
function onStepGrid(rows: ChartDatum[], keys: string[], win: ChartWindow): ChartDatum[] {
  if (rows.length === 0) return rows;
  const step = win.stepSeconds;
  const anchor = rows[0].timestamp;
  const slotOf = (ts: number) => Math.round((ts - anchor) / step);
  const bySlot = new Map<number, ChartDatum>();
  for (const row of rows) bySlot.set(slotOf(row.timestamp), row);
  const lastSampleSlot = slotOf(rows[rows.length - 1].timestamp);
  let first = Math.min(0, Math.ceil((win.endSeconds - win.spanSeconds - anchor) / step));
  let last = Math.max(lastSampleSlot, Math.floor((win.endSeconds - anchor) / step));
  if (last - first + 1 > 2 * (win.spanSeconds / step + 1)) {
    first = 0;
    last = lastSampleSlot;
  }
  const out: ChartDatum[] = [];
  for (let slot = first; slot <= last; slot += 1) {
    let row = bySlot.get(slot);
    if (!row) {
      const timestamp = anchor + slot * step;
      row = { timestamp, time: formatChartTimestamp(timestamp, win.spanSeconds) };
    }
    for (const key of keys) {
      if (!(key in row)) row[key] = null;
    }
    out.push(row);
  }
  return out;
}

function ServerDetail() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const params = useParams();
  const id = Number(params.id);
  const theme = useChartTheme();
  const [duration, setDuration] = useState(DURATIONS[0]);

  const serversQuery = useQuery({ queryKey: ['servers'], queryFn: getServers });
  const server = (serversQuery.data ?? []).find((s) => s.id === id);

  const metricsQuery = useQuery({
    queryKey: ['server-metrics', id, duration.key],
    queryFn: () => getServerMetrics(id, { duration: duration.key, step: duration.step }),
    enabled: Number.isFinite(id),
    refetchInterval: 30_000,
  });

  const series = metricsQuery.data ?? [];
  const chartWindow: ChartWindow = {
    spanSeconds: duration.seconds,
    stepSeconds: stepSeconds(duration.step),
    // When the data arrived (0 only while there is none, so no grid is built).
    endSeconds: metricsQuery.dataUpdatedAt / 1000,
  };
  const chartPanels = METRIC_ORDER
    .map((metric) => buildChartPanel(metric, series.filter((s) => s.metric === metric), theme, chartWindow))
    .filter((panel) => panel.lines.length > 0);
  const prometheusDown = metricsQuery.error instanceof AxiosError
    && metricsQuery.error.response?.status === 502
    && (metricsQuery.error.response.data as { detail?: unknown } | undefined)?.detail === PROMETHEUS_DOWN_DETAIL;

  return (
    <div className="connections">
      <div className="page-header">
        <div>
          <button type="button" className="link-button" onClick={() => navigate('/servers')}>&larr; {t('servers.title')}</button>
          <h1>{server?.name ?? `#${id}`}</h1>
          <p className="page-subtitle">{server?.address}</p>
          <p className="hint">{t('servers.chartTimeHint')}</p>
        </div>
        <div className="server-detail-controls">
          <GrafanaLink
            dashboard="unibridge-servers"
            time={{ kind: 'preset', value: duration.key }}
            vars={server?.name ? { 'var-host': server.name } : undefined}
          />
          {DURATIONS.map((d) => (
            <button
              key={d.key}
              type="button"
              className={`btn btn-sm${d.key === duration.key ? ' btn-primary' : ''}`}
              onClick={() => setDuration(d)}
            >
              {d.key}
            </button>
          ))}
        </div>
      </div>

      {metricsQuery.isLoading && <div className="loading-message" role="status">{t('common.loading')}</div>}
      {metricsQuery.isError && (
        <div className="error-banner" role="alert">
          {prometheusDown ? t('servers.metricsUnavailable') : t('common.errorOccurred')}
        </div>
      )}

      {!metricsQuery.isLoading && !metricsQuery.isError && (
        chartPanels.length === 0 ? (
          <div className="empty-state server-detail-empty">
            <p>{t('servers.noMetricData')}</p>
          </div>
        ) : (
          <div className="server-charts">
            {chartPanels.map((panel) => {
              const capacityItems = panel.metric === 'disk'
                ? panel.lines.flatMap((line) => {
                    const value = formatDiskCapacityValue(line.usedBytes, line.totalBytes, t);
                    return value ? [{ line, value }] : [];
                  })
                : [];
              // Identical cards are the norm on a GPU host, so list each model once.
              const gpuModels = Array.from(
                new Set(panel.lines.flatMap((line) => (line.model ? [line.model] : []))),
              );
              return (
                <div className="server-chart-card" key={panel.metric}>
                  <div className="server-chart-card__heading">
                    <h3>{t(`servers.metric_${panel.metric}`)}</h3>
                    {capacityItems.length > 0 && (
                      <div className="server-disk-capacity" aria-label={t('servers.diskCapacity')}>
                        {capacityItems.map(({ line, value }) => (
                          <span className="server-disk-capacity__item" key={line.key}>
                            <span
                              className="server-disk-capacity__swatch"
                              style={{ backgroundColor: line.color }}
                              aria-hidden="true"
                            />
                            <span>{line.name}: {value}</span>
                          </span>
                        ))}
                      </div>
                    )}
                    {gpuModels.length > 0 && (
                      <p className="server-gpu-models" aria-label={t('servers.gpuModels')}>
                        {gpuModels.join(', ')}
                      </p>
                    )}
                  </div>
                  <div className="server-chart-body">
                    {panel.data.length === 0 ? (
                      <div className="empty-state empty-state--small"><p>{t('servers.noMetricData')}</p></div>
                    ) : (
                      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
                        <LineChart data={panel.data}>
                          <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
                          <XAxis dataKey="time" stroke={theme.axis} tick={{ fontSize: 11 }} minTickGap={40} />
                          <YAxis stroke={theme.axis} tick={{ fontSize: 11 }} domain={[0, 100]} unit="%" />
                          <Tooltip
                            labelFormatter={(label, payload) => {
                              const ts = payload?.[0]?.payload?.timestamp;
                              return typeof ts === 'number' ? formatChartPointTime(ts) : String(label);
                            }}
                            contentStyle={{ background: theme.tooltipBg, border: `1px solid ${theme.tooltipBorder}` }}
                            formatter={(value, _name, item) => {
                              const n = Number(value);
                              if (!Number.isFinite(n)) return '—';
                              const percent = `${n.toFixed(1)}%`;
                              if (panel.metric !== 'disk' || !item || typeof item !== 'object') {
                                return percent;
                              }
                              const dataKey = 'dataKey' in item ? String(item.dataKey) : null;
                              const line = panel.lines.find((candidate) => candidate.key === dataKey);
                              const payload = 'payload' in item ? item.payload as ChartDatum | undefined : undefined;
                              const capacity = line && payload
                                ? formatDiskCapacityValue(payload[line.usedKey], payload[line.totalKey], t)
                                : null;
                              return capacity ? `${percent} · ${capacity}` : percent;
                            }}
                          />
                          {panel.lines.length > 1 && (
                            <Legend
                              iconType="line"
                              verticalAlign="top"
                              height={24}
                              wrapperStyle={{ color: theme.textSecondary, fontSize: 12 }}
                            />
                          )}
                          {panel.lines.map((line) => (
                            <Line
                              key={line.key}
                              type="monotone"
                              dataKey={line.key}
                              name={line.name}
                              stroke={line.color}
                              strokeWidth={2}
                              dot={isolatedPointDot(panel.data as Array<Record<string, number | null>>, line.key)}
                            />
                          ))}
                        </LineChart>
                      </ResponsiveContainer>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )
      )}
    </div>
  );
}

export default ServerDetail;
