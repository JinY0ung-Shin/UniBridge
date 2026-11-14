import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatBytes } from '../utils/bytes';
import { REVOKE_DELAY_MS, saveBlob, saveJson } from '../utils/download';

describe('formatBytes', () => {
  it('counts in 1024s and drops a trailing .0 by default', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(5 * 1024 ** 3)).toBe('5 GB');
    expect(formatBytes(2 * 1024 ** 5)).toBe('2 PB');
  });

  it('shows an unknown size as a dash', () => {
    expect(formatBytes(null)).toBe('—');
  });

  it('keeps one decimal and IEC labels when asked', () => {
    expect(formatBytes(2048, { fixedDecimal: true })).toBe('2.0 KB');
    expect(formatBytes(5 * 1024 * 1024, { fixedDecimal: true })).toBe('5.0 MB');
    expect(formatBytes(512, { fixedDecimal: true })).toBe('512 B');
    expect(formatBytes(1536 * 1024 ** 3, { iec: true, fixedDecimal: true })).toBe('1.5 TiB');
    expect(formatBytes(12.6, { iec: true, fixedDecimal: true })).toBe('13 B');
  });
});

describe('saveBlob / saveJson', () => {
  const originalCreate = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    for (const [name, descriptor] of [['createObjectURL', originalCreate], ['revokeObjectURL', originalRevoke]] as const) {
      if (descriptor) Object.defineProperty(URL, name, descriptor);
      else delete (URL as unknown as Record<string, unknown>)[name];
    }
  });

  it('clicks a download link and revokes its URL a moment later', () => {
    vi.useFakeTimers();
    const createObjectURL = vi.fn(() => 'blob:x');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);

    saveBlob(new Blob(['hi']), 'hi.txt');

    expect(click).toHaveBeenCalledOnce();
    const anchor = click.mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.download).toBe('hi.txt');
    expect(anchor.href).toBe('blob:x');
    expect(anchor.isConnected).toBe(false);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(REVOKE_DELAY_MS);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:x');
  });

  it('saves a value as pretty JSON', async () => {
    // fake timers, so the delayed revoke runs inside this test, not after its cleanup
    vi.useFakeTimers();
    let saved: Blob | undefined;
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn((blob: Blob) => { saved = blob; return 'blob:json'; }),
    });
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);

    saveJson({ a: 1 }, 'a.json');
    vi.advanceTimersByTime(REVOKE_DELAY_MS);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:json');

    expect(saved?.type).toBe('application/json');
    expect(await saved?.text()).toBe('{\n  "a": 1\n}');
  });
});
