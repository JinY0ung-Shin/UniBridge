import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import { getServers, getServerMetrics, type ServerMetricSeries } from '../api/client';
import { useChartTheme } from '../components/useChartTheme';
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

function toChartData(series: ServerMetricSeries) {
  return series.points.map((p) => ({
    time: new Date(p.t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    value: p.v,
  }));
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

  return (
    <div className="connections">
      <div className="page-header">
        <div>
          <button className="link-button" onClick={() => navigate('/servers')}>&larr; {t('servers.title')}</button>
          <h1>{server?.name ?? `#${id}`}</h1>
          <p className="page-subtitle">{server?.address}</p>
        </div>
        <div className="server-detail-controls">
          {DURATIONS.map((d) => (
            <button
              key={d.key}
              className={`btn btn-sm${d.key === duration.key ? ' btn-primary' : ''}`}
              onClick={() => setDuration(d)}
            >
              {d.key}
            </button>
          ))}
        </div>
      </div>

      {metricsQuery.isLoading && <div className="loading-message">{t('common.loading')}</div>}
      {metricsQuery.isError && <div className="error-banner">{t('common.errorOccurred')}</div>}

      {!metricsQuery.isLoading && !metricsQuery.isError && (
        <div className="server-charts">
          {series.map((s) => {
            const color = theme[METRIC_COLORS[s.metric] ?? 'blue'];
            const data = toChartData(s);
            return (
              <div className="server-chart-card" key={s.metric}>
                <h3>{t(`servers.metric_${s.metric}`)}</h3>
                <div className="server-chart-body">
                  {data.length === 0 ? (
                    <div className="empty-state empty-state--small"><p>{t('servers.noMetricData')}</p></div>
                  ) : (
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={data}>
                        <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
                        <XAxis dataKey="time" stroke={theme.axis} tick={{ fontSize: 11 }} minTickGap={40} />
                        <YAxis stroke={theme.axis} tick={{ fontSize: 11 }} domain={[0, 100]} unit="%" />
                        <Tooltip
                          contentStyle={{ background: theme.tooltipBg, border: `1px solid ${theme.tooltipBorder}` }}
                          formatter={(value) => {
                            const n = Number(value);
                            return Number.isFinite(n) ? `${n.toFixed(1)}%` : '—';
                          }}
                        />
                        <Line type="monotone" dataKey="value" stroke={color} strokeWidth={2} dot={false} connectNulls />
                      </LineChart>
                    </ResponsiveContainer>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default ServerDetail;
