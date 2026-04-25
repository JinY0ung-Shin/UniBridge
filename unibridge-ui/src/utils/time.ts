const KST_OPTS: Intl.DateTimeFormatOptions = {
  timeZone: 'Asia/Seoul',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
};

/**
 * Format a UTC timestamp (ISO string or Date) as Korea Standard Time.
 * Returns '—' for null/undefined/empty input; falls back to the original
 * string when parsing fails.
 */
export function formatKST(value: string | Date | null | undefined): string {
  if (!value) return '—';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) {
    return typeof value === 'string' ? value : '';
  }
  return d.toLocaleString('ko-KR', KST_OPTS);
}

/**
 * Convert a "YYYY-MM-DD" string (interpreted as a KST calendar day) to an
 * ISO 8601 UTC timestamp marking the day's start or end.
 *
 * Examples (KST = UTC+9):
 *   kstDateToUtcIso("2026-04-22", "start") → "2026-04-21T15:00:00.000Z"
 *   kstDateToUtcIso("2026-04-22", "end")   → "2026-04-22T14:59:59.999Z"
 *
 * Returns undefined for empty or malformed input, preserving "no filter"
 * semantics when the field is left blank.
 */
export function kstDateToUtcIso(
  dateStr: string,
  boundary: 'start' | 'end',
): string | undefined {
  if (!dateStr) return undefined;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
  if (!match) return undefined;
  const [, y, m, d] = match;
  // KST (+09:00) wall-clock anchor for the day boundary
  const iso =
    boundary === 'start'
      ? `${y}-${m}-${d}T00:00:00+09:00`
      : `${y}-${m}-${d}T23:59:59.999+09:00`;
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
}
