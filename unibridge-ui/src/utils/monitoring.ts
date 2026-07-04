/**
 * Shared severity tiers for error-rate displays, matching the comparison
 * tables' heatmap thresholds (yellow ≥1%, red ≥5%) so cards and tables agree.
 */
export function errorRateColor(v: number): string {
  if (v >= 5) return 'var(--accent-red)';
  if (v >= 1) return 'var(--accent-yellow)';
  return 'var(--accent-green)';
}
