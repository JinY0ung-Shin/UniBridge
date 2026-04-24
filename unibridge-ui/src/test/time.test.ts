import { describe, it, expect } from 'vitest';
import { formatKST } from '../utils/time';

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
