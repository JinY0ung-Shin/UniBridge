import { describe, it, expect } from 'vitest';
import { formatKST, kstDateToUtcIso } from '../utils/time';

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
