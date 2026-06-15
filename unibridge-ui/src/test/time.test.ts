import { describe, it, expect } from 'vitest';
import {
  formatKST,
  kstDateToUtcIso,
  kstLocalToEpoch,
  epochToKstLocal,
  formatChartTime,
  formatChartTimestamp,
  formatBucketLabel,
  formatKstChip,
} from '../utils/time';

describe('formatKST', () => {
  it('converts UTC ISO to KST string', () => {
    const out = formatKST('2026-04-22T00:00:00+00:00');
    // 00:00 UTC → 09:00 KST
    expect(out).toMatch(/09:00:00/);
  });

  it('converts Z-suffixed UTC ISO to KST string', () => {
    const out = formatKST('2026-04-22T12:00:00Z');
    // 12:00 UTC → 21:00 KST
    expect(out).toMatch(/21:00:00/);
  });

  it('accepts Date instance', () => {
    const d = new Date('2026-04-22T00:00:00Z');
    const out = formatKST(d);
    expect(out).toMatch(/09:00:00/);
  });

  it('returns em-dash for null', () => {
    expect(formatKST(null)).toBe('—');
  });

  it('returns em-dash for undefined', () => {
    expect(formatKST(undefined)).toBe('—');
  });

  it('returns em-dash for empty string', () => {
    expect(formatKST('')).toBe('—');
  });

  it('falls back to original string for invalid input', () => {
    expect(formatKST('not-a-date')).toBe('not-a-date');
  });
});

describe('kstDateToUtcIso', () => {
  it('returns start-of-day UTC for KST date start boundary', () => {
    // 2026-04-22 00:00 KST = 2026-04-21 15:00 UTC
    expect(kstDateToUtcIso('2026-04-22', 'start')).toBe('2026-04-21T15:00:00.000Z');
  });

  it('returns end-of-day UTC for KST date end boundary', () => {
    // 2026-04-22 23:59:59.999 KST = 2026-04-22 14:59:59.999 UTC
    expect(kstDateToUtcIso('2026-04-22', 'end')).toBe('2026-04-22T14:59:59.999Z');
  });

  it('returns undefined for empty string', () => {
    expect(kstDateToUtcIso('', 'start')).toBeUndefined();
  });

  it('returns undefined for malformed input', () => {
    expect(kstDateToUtcIso('not-a-date', 'start')).toBeUndefined();
    expect(kstDateToUtcIso('2026/04/22', 'start')).toBeUndefined();
  });
});

describe('KST monitoring helpers', () => {
  // 2026-05-20 09:00 KST == 2026-05-20 00:00 UTC
  const epoch = Date.UTC(2026, 4, 20, 0, 0, 0) / 1000;

  it('kstLocalToEpoch interprets input as KST (+09:00)', () => {
    expect(kstLocalToEpoch('2026-05-20T09:00')).toBe(epoch);
  });

  it('kstLocalToEpoch accepts datetime-local values with seconds', () => {
    expect(kstLocalToEpoch('2026-05-20T09:00:30')).toBe(epoch + 30);
  });

  it('epochToKstLocal round-trips', () => {
    expect(epochToKstLocal(epoch)).toBe('2026-05-20T09:00');
  });

  it('formatChartTime renders KST HH:mm regardless of host TZ', () => {
    expect(formatChartTime(epoch)).toBe('09:00');
  });

  it('formatChartTimestamp picks granularity by span', () => {
    expect(formatChartTimestamp(epoch, 3600)).toBe('09:00');            // <=24h
    expect(formatChartTimestamp(epoch, 2 * 86400)).toBe('5/20 09h');    // >24h, <=7d
    expect(formatChartTimestamp(epoch, 30 * 86400)).toBe('5/20');       // >7d
  });

  it('formatKstChip renders start~end', () => {
    const end = epoch + 2 * 86400 + 9 * 3600; // 5/22 18:00 KST
    expect(formatKstChip(epoch, end)).toBe('5/20 09:00~5/22 18:00');
  });

  it('formatBucketLabel renders per-granularity KST labels', () => {
    expect(formatBucketLabel(epoch, 'hour')).toBe('5/20 09h');
    expect(formatBucketLabel(epoch, 'day')).toBe('5/20');
    expect(formatBucketLabel(epoch, 'week')).toBe('5/20~');
  });
});
