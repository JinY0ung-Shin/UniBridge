import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import { getServers, getServerMetrics, type ServerMetricSeries } from '../api/client';
import { useChartTheme, type ChartTheme } from '../components/useChartTheme';
import GrafanaLink from '../components/GrafanaLink';
import './Connections.css';
import './Servers.css';

const DURATIONS: Array<{ key: string; step: string }> = [
  { key: '1h', step: '60s' },
  { key: '6h', step: '120s' },
  { key: '24h', step: '300s' },
];

const METRIC_COLORS: Record<string, 'blue' | 'green' | 'yellow'> = {
  cpu: 'blue',
  mem: 'green',
  disk: 'yellow',
};

const METRIC_ORDER: Array<ServerMetricSeries['metric']> = ['cpu', 'mem', 'disk'];

type ChartDatum = {
  timestamp: number;
  time: string;
  [key: string]: number | string | null;
};

interface ChartLine {
  key: string;
  name: string;
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

function formatTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function diskLineColor(theme: ChartTheme, index: number) {
  const colors = [theme.yellow, theme.blue, theme.green, theme.red, theme.textSecondary, theme.textTertiary];
  return colors[index % colors.length];
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
): ChartPanel {
  const dataByTimestamp = new Map<number, ChartDatum>();
  const lines = metricSeries.map((s, index) => {
    const key = `value_${index}`;
    const totalKey = `total_${index}`;
    const usedKey = `used_${index}`;
    const availableKey = `available_${index}`;
    const name = metric === 'disk' ? (s.mountpoint ?? 'disk') : metric;
    let totalBytes: number | null = null;
    let usedBytes: number | null = null;
    let availableBytes: number | null = null;
    for (const point of s.points) {
      let datum = dataByTimestamp.get(point.t);
      if (!datum) {
        datum = { timestamp: point.t, time: formatTime(point.t) };
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
      color: metric === 'disk' ? diskLineColor(theme, index) : theme[METRIC_COLORS[metric]],
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
    data: Array.from(dataByTimestamp.values()).sort((a, b) => a.timestamp - b.timestamp),
  };
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
  const chartPanels = METRIC_ORDER
    .map((metric) => buildChartPanel(metric, series.filter((s) => s.metric === metric), theme))
    .filter((panel) => panel.lines.length > 0);

  return (
    <div className="connections">
      <div className="page-header">
        <div>
          <button type="button" className="link-button" onClick={() => navigate('/servers')}>&larr; {t('servers.title')}</button>
          <h1>{server?.name ?? `#${id}`}</h1>
          <p className="page-subtitle">{server?.address}</p>
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
      {metricsQuery.isError && <div className="error-banner" role="alert">{t('common.errorOccurred')}</div>}

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
                  </div>
                  <div className="server-chart-body">
                    {panel.data.length === 0 ? (
                      <div className="empty-state empty-state--small"><p>{t('servers.noMetricData')}</p></div>
                    ) : (
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={panel.data}>
                          <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
                          <XAxis dataKey="time" stroke={theme.axis} tick={{ fontSize: 11 }} minTickGap={40} />
                          <YAxis stroke={theme.axis} tick={{ fontSize: 11 }} domain={[0, 100]} unit="%" />
                          <Tooltip
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
                              dot={false}
                              connectNulls
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
