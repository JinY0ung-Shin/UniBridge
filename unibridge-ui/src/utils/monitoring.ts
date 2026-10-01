import type { LatencyData } from '../api/client';
import { isolatedPointDot } from './chartDots';
import { formatChartPointTime } from './time';

/**
 * XAxis + Tooltip props for a chart whose rows carry their epoch as `ts`.
 *
 * recharts resolves the hovered row of an axis tooltip by axis value (first
 * row whose dataKey equals the active label), not by index — so keying the
 * axis on a display label that repeats ("14:00" on 15s steps, "9/24" on 6h
 * steps) shows the first matching row's values. Keying on the unique epoch
 * and formatting only for display avoids that: ticks render `tickLabel(ts)`,
 * the tooltip names the point with `tooltipLabel(ts)`.
 */
export function timeKeyedAxis(
  tickLabel: (ts: number) => string,
  tooltipLabel: (ts: number) => string = formatChartPointTime,
) {
  return {
    xAxis: { dataKey: 'ts', tickFormatter: (value: unknown) => tickLabel(Number(value)) },
    tooltip: { labelFormatter: (label: unknown) => tooltipLabel(Number(label)) },
  };
}

export interface LatencyChartRow {
  /** Point epoch (seconds) — the chart's axis key (see timeKeyedAxis). */
  ts: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
}

/**
 * p50/p95/p99 series → recharts rows keyed by epoch. Quantiles over windows
 * with no traffic arrive as null (and missing points become null), so the
 * lines show gaps instead of misleading dips to zero.
 */
export function toLatencyChartRows(data: LatencyData | undefined): LatencyChartRow[] {
  return (data?.p50 ?? []).map((p, i) => ({
    ts: p.timestamp,
    p50: p.value,
    p95: data?.p95?.[i]?.value ?? null,
    p99: data?.p99?.[i]?.value ?? null,
  }));
}

/** True when any row carries a latency value (an all-gap series is "no data"). */
export function hasLatencyValues(rows: LatencyChartRow[]): boolean {
  return rows.some((row) => row.p50 != null || row.p95 != null || row.p99 != null);
}

/**
 * Per-line `dot` renderers for gap-preserving latency charts (no connectNulls):
 * sparse traffic leaves lone values between gaps, which recharts would draw as
 * invisible zero-length paths — see isolatedPointDot.
 */
export function latencyLineDots(rows: LatencyChartRow[]) {
  return {
    p50: isolatedPointDot(rows, 'p50'),
    p95: isolatedPointDot(rows, 'p95'),
    p99: isolatedPointDot(rows, 'p99'),
  };
}

/**
 * Shared severity tiers for error-rate displays, matching the comparison
 * tables' heatmap thresholds (yellow ≥1%, red ≥5%) so cards and tables agree.
 */
export function errorRateColor(v: number): string {
  if (v >= 5) return 'var(--accent-red)';
  if (v >= 1) return 'var(--accent-yellow)';
  return 'var(--accent-green)';
}

/**
 * Query-monitoring `consumer` value for queries run without an API key (UI
 * Query Playground / direct JWT calls). Real key names can never be wrapped in
 * double underscores, so this never collides with a key.
 */
export const QUERY_UI_CONSUMER = '__ui__';

/**
 * Query-monitoring row for queries recorded before per-key tracking existed
 * (series without a consumer label). Not a filterable key.
 */
export const QUERY_UNTRACKED_CONSUMER = '(untracked)';
