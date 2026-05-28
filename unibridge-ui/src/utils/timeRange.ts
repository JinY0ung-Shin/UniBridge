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
