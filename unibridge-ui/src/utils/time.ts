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
