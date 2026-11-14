import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { COPY_FEEDBACK_MS, useCopyFeedback } from '../components/useCopyFeedback';

const writeText = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  writeText.mockReset();
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useCopyFeedback', () => {
  it('shows the copied key until the feedback time runs out', async () => {
    writeText.mockResolvedValue(undefined);
    const { result } = renderHook(() => useCopyFeedback<'a' | 'b'>());

    await act(async () => {
      expect(await result.current.copy('text', 'a')).toBe(true);
    });
    expect(writeText).toHaveBeenCalledWith('text');
    expect(result.current.copied).toBe(true);
    expect(result.current.copiedKey).toBe('a');

    act(() => vi.advanceTimersByTime(COPY_FEEDBACK_MS));
    expect(result.current.copied).toBe(false);
    expect(result.current.copiedKey).toBeNull();
  });

  it('restarts the timer on a second copy and clears on reset', async () => {
    writeText.mockResolvedValue(undefined);
    const { result } = renderHook(() => useCopyFeedback<'a' | 'b'>());

    await act(async () => { await result.current.copy('one', 'a'); });
    act(() => vi.advanceTimersByTime(COPY_FEEDBACK_MS - 100));
    await act(async () => { await result.current.copy('two', 'b'); });
    act(() => vi.advanceTimersByTime(200));
    // the first copy's timer did not end the second one early
    expect(result.current.copiedKey).toBe('b');

    act(() => result.current.reset());
    expect(result.current.copied).toBe(false);
  });

  it('clears the state and reports a failed copy', async () => {
    writeText.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error('denied'));
    const onError = vi.fn();
    const { result } = renderHook(() => useCopyFeedback(onError));

    await act(async () => { await result.current.copy('ok'); });
    expect(result.current.copied).toBe(true);
    await act(async () => {
      expect(await result.current.copy('nope')).toBe(false);
    });
    expect(result.current.copied).toBe(false);
    expect(onError).toHaveBeenCalledTimes(1);
  });
});
