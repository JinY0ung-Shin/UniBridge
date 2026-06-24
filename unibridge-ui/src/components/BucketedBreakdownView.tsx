import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import { useChartTheme } from './useChartTheme';
import { type Bucket } from '../utils/timeRange';
import { formatBucketLabel } from '../utils/time';
import type { BucketedBreakdown } from '../api/client';

interface BucketedBreakdownViewProps {
  title: string;
  data?: BucketedBreakdown;
  bucket: Bucket;
  loading?: boolean;
  unit: 'tokens' | 'requests';
  valueFmt?: (n: number) => string;
}

/** Default compact value formatter (used when the page passes none). */
function compactNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
}

/** Stable palette for the stacked series; cycles when there are many keys. */
const SERIES_PALETTE = [
  '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899',
  '#14b8a6', '#f97316', '#6366f1', '#84cc16', '#06b6d4', '#a855f7',
  '#64748b',
];

/**
 * Reusable per-dimension "usage over time" view: a stacked recharts bar chart
 * (x = calendar bucket, one stack per dimension value) plus a pivot table
 * (rows = dimension value, columns = buckets, last column = row total).
 *
 * When `bucket === 'auto'` only a muted hint is shown — pages disable the
 * underlying queries in that case, so `data` will be absent anyway.
 */
function BucketedBreakdownView({
  title,
  data,
  bucket,
  loading,
  unit,
  valueFmt,
}: BucketedBreakdownViewProps) {
  const { t } = useTranslation();
  const chartColors = useChartTheme();
  const fmt = valueFmt ?? compactNumber;

  const bucketLabels = useMemo(() => {
    if (bucket === 'auto') return [];
    return (data?.buckets ?? []).map((b) => formatBucketLabel(b, bucket));
  }, [data?.buckets, bucket]);

  // recharts row-per-bucket data: { bucket: <label>, [seriesKey]: value, ... }
  const chartData = useMemo(() => {
    if (!data) return [];
    return data.buckets.map((_, i) => {
      const row: Record<string, string | number> = { bucket: bucketLabels[i] };
      for (const s of data.series) {
        row[s.key] = Math.round(s.points[i] ?? 0);
      }
      return row;
    });
  }, [data, bucketLabels]);

  if (bucket === 'auto') {
    return (
      <div className="chart-panel">
        <div className="chart-panel__title">{title}</div>
        <div className="no-data">{t('breakdown.selectBucketHint')}</div>
      </div>
    );
  }

  const hasData = !!data && data.series.length > 0 && data.buckets.length > 0;

  return (
    <div className="chart-panel">
      <div className="chart-panel__title">{title}</div>
      {loading ? (
        <div className="no-data" role="status">{t('breakdown.loading')}</div>
      ) : hasData ? (
        <>
          <div className="chart-container">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} />
                <XAxis dataKey="bucket" stroke={chartColors.axis} tick={{ fontSize: 11 }} />
                <YAxis
                  stroke={chartColors.axis}
                  tick={{ fontSize: 11 }}
                  label={{ value: unit, angle: -90, position: 'insideLeft', fill: chartColors.axis, fontSize: 11 }}
                />
                <Tooltip
                  contentStyle={{ background: chartColors.tooltipBg, border: `1px solid ${chartColors.tooltipBorder}`, borderRadius: 6 }}
                  labelStyle={{ color: chartColors.axis }}
                  itemStyle={{ color: chartColors.textSecondary }}
                />
                <Legend wrapperStyle={{ color: chartColors.axis, fontSize: 11 }} />
                {data!.series.map((s, i) => (
                  <Bar
                    key={s.key}
                    dataKey={s.key}
                    stackId="a"
                    fill={SERIES_PALETTE[i % SERIES_PALETTE.length]}
                    name={s.key}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>

          <div className="table-container table-container--plain">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col" aria-hidden="true" />
                  {bucketLabels.map((label, i) => (
                    <th key={i} scope="col" className="breakdown-cell--right">{label}</th>
                  ))}
                  <th scope="col" className="breakdown-cell--right">{t('breakdown.total')}</th>
                </tr>
              </thead>
              <tbody>
                {data!.series.map((s, si) => (
                  <tr key={s.key}>
                    <th scope="row" className="cell-alias">
                      <span
                        className={`breakdown-swatch breakdown-swatch--${si % SERIES_PALETTE.length}`}
                      />
                      {s.key}
                    </th>
                    {data!.buckets.map((_, bi) => (
                      <td
                        key={bi}
                        className="breakdown-cell--right breakdown-cell--mono"
                      >
                        {fmt(s.points[bi] ?? 0)}
                      </td>
                    ))}
                    <td
                      className="breakdown-cell--right breakdown-cell--mono breakdown-cell--total"
                    >
                      {fmt(s.total)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <div className="no-data">{t('breakdown.noData')}</div>
      )}
    </div>
  );
}

export default BucketedBreakdownView;
