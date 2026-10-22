import { describe, it, expect } from 'vitest';
import { formatLatencyMs, formatSeconds, hasLatencyValues, secondsTooltipFormatter, timeKeyedAxis, toLatencyChartRows } from '../utils/monitoring';
import { formatBucketLabel, formatChartTimestamp } from '../utils/time';

// 2026-09-24 14:00 KST
const ts = Date.UTC(2026, 8, 24, 5, 0, 0) / 1000;

describe('formatSeconds', () => {
  it('shows latency in seconds, three decimals below one second', () => {
    expect(formatLatencyMs(45.5)).toBe('0.046s');
    expect(formatLatencyMs(1234)).toBe('1.23s');
    expect(formatLatencyMs(60000)).toBe('60s');
    expect(formatLatencyMs(300000)).toBe('300s');
    expect(formatSeconds(0)).toBe('0s');
  });

  it('formats numeric tooltip values and passes others through', () => {
    expect(secondsTooltipFormatter(0.25)).toBe('0.25s');
    expect(secondsTooltipFormatter(null)).toBe('');
  });
});

describe('toLatencyChartRows', () => {
  it('keys rows by epoch and keeps missing percentiles as null gaps', () => {
    const rows = toLatencyChartRows({
      p50: [{ timestamp: ts, value: 10 }, { timestamp: ts + 3600, value: 12 }],
      p95: [{ timestamp: ts, value: 40 }],
      p99: [],
    });
    expect(rows).toEqual([
      { ts, p50: 0.01, p95: 0.04, p99: null },
      { ts: ts + 3600, p50: 0.012, p95: null, p99: null },
    ]);
  });

  it('passes null quantiles through, including p50, so recharts draws gaps', () => {
    const rows = toLatencyChartRows({
      p50: [{ timestamp: ts, value: null }, { timestamp: ts + 60, value: 8 }],
      p95: [{ timestamp: ts, value: null }, { timestamp: ts + 60, value: 20 }],
      p99: [{ timestamp: ts, value: 95 }, { timestamp: ts + 60, value: null }],
    });
    expect(rows).toEqual([
      { ts, p50: null, p95: null, p99: 0.095 },
      { ts: ts + 60, p50: 0.008, p95: 0.02, p99: null },
    ]);
  });

  it('reports whether any row carries a latency value', () => {
    expect(hasLatencyValues([{ ts, p50: null, p95: null, p99: null }])).toBe(false);
    expect(hasLatencyValues([{ ts, p50: null, p95: null, p99: 3 }])).toBe(true);
    expect(hasLatencyValues([])).toBe(false);
  });

  it('returns no rows without data', () => {
    expect(toLatencyChartRows(undefined)).toEqual([]);
  });
});

describe('timeKeyedAxis', () => {
  it('keys the axis on the epoch and formats ticks with the label fn', () => {
    const { xAxis } = timeKeyedAxis((t) => formatChartTimestamp(t, 30 * 86400));
    expect(xAxis.dataKey).toBe('ts');
    expect(xAxis.tickFormatter(ts)).toBe('9/24');
  });

  it('names the point time in the tooltip by default', () => {
    const { tooltip } = timeKeyedAxis((t) => formatChartTimestamp(t, 30 * 86400));
    expect(tooltip.labelFormatter(ts)).toBe('9/24 14:00');
    expect(tooltip.labelFormatter(ts + 6 * 3600)).toBe('9/24 20:00');
  });

  it('accepts a period label for calendar-bucket bars', () => {
    const dayLabel = (t: number) => formatBucketLabel(t, 'day');
    const { xAxis, tooltip } = timeKeyedAxis(dayLabel, dayLabel);
    expect(xAxis.tickFormatter(ts)).toBe('9/24');
    expect(tooltip.labelFormatter(ts)).toBe('9/24');
  });
});
