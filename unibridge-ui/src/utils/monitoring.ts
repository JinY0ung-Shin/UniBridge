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
