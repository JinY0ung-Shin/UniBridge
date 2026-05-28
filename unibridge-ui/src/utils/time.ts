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

const KST_TZ = 'Asia/Seoul';

function kstParts(epochSeconds: number): Record<string, string> {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: KST_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  const parts: Record<string, string> = {};
  for (const p of fmt.formatToParts(new Date(epochSeconds * 1000))) {
    if (p.type !== 'literal') parts[p.type] = p.value;
  }
  if (parts.hour === '24') parts.hour = '00'; // some engines emit 24 at midnight
  return parts;
}

/** epoch seconds → "HH:mm" in KST. */
export function formatChartTime(epochSeconds: number): string {
  const p = kstParts(epochSeconds);
  return `${p.hour}:${p.minute}`;
}

/** epoch seconds → span-aware axis label in KST. */
export function formatChartTimestamp(epochSeconds: number, spanSeconds: number): string {
  const p = kstParts(epochSeconds);
  if (spanSeconds > 7 * 86400) return `${Number(p.month)}/${Number(p.day)}`;
  if (spanSeconds > 86400) return `${Number(p.month)}/${Number(p.day)} ${p.hour}h`;
  return `${p.hour}:${p.minute}`;
}

/** Two epochs → "M/D HH:mm~M/D HH:mm" chip text in KST. */
export function formatKstChip(startSeconds: number, endSeconds: number): string {
  const s = kstParts(startSeconds);
  const e = kstParts(endSeconds);
  return (
    `${Number(s.month)}/${Number(s.day)} ${s.hour}:${s.minute}` +
    `~${Number(e.month)}/${Number(e.day)} ${e.hour}:${e.minute}`
  );
}

const DATETIME_LOCAL_MINUTE_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

/** "YYYY-MM-DDTHH:mm" or "...:ss" (datetime-local, interpreted as KST) → epoch seconds. */
export function kstLocalToEpoch(local: string): number {
  const normalized = DATETIME_LOCAL_MINUTE_RE.test(local) ? `${local}:00` : local;
  return Math.floor(Date.parse(`${normalized}+09:00`) / 1000);
}

/** epoch seconds → "YYYY-MM-DDTHH:mm" wall-clock string in KST (for datetime-local value). */
export function epochToKstLocal(epochSeconds: number): string {
  const p = kstParts(epochSeconds);
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}
