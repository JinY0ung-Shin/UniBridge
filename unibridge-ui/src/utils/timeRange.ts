export const TIME_RANGES = ['15m', '1h', '6h', '24h', '7d', '30d', '60d'] as const;

export const PRESET_SECONDS: Record<string, number> = {
  '15m': 900,
  '1h': 3600,
  '6h': 21600,
  '24h': 86400,
  '7d': 604800,
  '30d': 2592000,
  '60d': 5184000,
};

export type TimeSelection =
  | { kind: 'preset'; value: string }
  | { kind: 'custom'; start: number; end: number }; // epoch seconds

export const DEFAULT_SELECTION: TimeSelection = { kind: 'preset', value: '1h' };

/** Query params for the metrics API: preset → {range}, custom → {start,end}. */
export function timeParams(sel: TimeSelection): Record<string, string | number> {
  return sel.kind === 'preset'
    ? { range: sel.value }
    : { start: sel.start, end: sel.end };
}

/** Stable react-query key fragment. */
export function selectionKey(sel: TimeSelection): string {
  return sel.kind === 'preset' ? `preset:${sel.value}` : `custom:${sel.start}-${sel.end}`;
}

/** Span in seconds (for chart-axis label granularity). */
export function selectionSpanSeconds(sel: TimeSelection): number {
  return sel.kind === 'preset' ? PRESET_SECONDS[sel.value] ?? 3600 : sel.end - sel.start;
}

/**
 * Calendar bucket granularity for volume/bar charts. `auto` keeps the legacy
 * range-derived stepping; hour/day/week snap bars to KST calendar boundaries.
 */
export const BUCKETS = ['auto', 'hour', 'day', 'week'] as const;
export type Bucket = (typeof BUCKETS)[number];

/** Query param for bucketed endpoints; omitted (no override) when auto. */
export function bucketParam(bucket: Bucket): Record<string, string> {
  return bucket === 'auto' ? {} : { bucket };
}

/** Stable react-query key fragment for a bucket. */
export function bucketKey(bucket: Bucket): string {
  return `bucket:${bucket}`;
}

/**
 * One-shot bucket→period convenience mapping. Picking a calendar bucket nudges
 * the time range to a sensible default span; `auto`/`hour` leave it untouched.
 */
export const BUCKET_PERIOD: Record<Bucket, string | null> = {
  auto: null,
  hour: null,
  day: '7d',
  week: '30d',
};

/**
 * Suggested time selection for a freshly picked bucket, or `null` when the
 * bucket carries no opinion (auto/hour). Pages apply this once on bucket
 * change — but only for preset selections; an explicit custom range is never
 * overridden. The period selector remains independently adjustable afterward.
 */
export function periodForBucket(bucket: Bucket): TimeSelection | null {
  const value = BUCKET_PERIOD[bucket];
  return value ? { kind: 'preset', value } : null;
}

/** Approximate seconds covered by one calendar bucket. */
export const BUCKET_SECONDS: Record<Exclude<Bucket, 'auto'>, number> = {
  hour: 3600,
  day: 86400,
  week: 604800,
};

/**
 * True when the selection is too narrow for the bucket to draw a useful chart
 * (fewer than ~2 bars). Pages reset the bucket to 'auto' in that case when the
 * user shrinks the time range.
 */
export function bucketTooCoarse(sel: TimeSelection, bucket: Bucket): boolean {
  if (bucket === 'auto') return false;
  return selectionSpanSeconds(sel) < BUCKET_SECONDS[bucket] * 2;
}
