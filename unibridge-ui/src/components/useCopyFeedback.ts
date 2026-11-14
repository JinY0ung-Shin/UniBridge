import { useEffect, useRef, useState } from 'react';

/** How long a copy button says "Copied". */
export const COPY_FEEDBACK_MS = 2000;

/**
 * Clipboard copy with a short "Copied" state for the button that did it.
 *
 * `copiedKey` names the last successful copy (pages with several copy buttons
 * pass a key per button) until COPY_FEEDBACK_MS runs out or `reset()` is
 * called; `copied` is true while any is showing. A failed copy clears it and
 * calls `onError`.
 */
export function useCopyFeedback<K extends string = 'default'>(onError?: () => void) {
  const [copiedKey, setCopiedKey] = useState<K | null>(null);
  const timerRef = useRef<number | null>(null);

  function clearTimer() {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  function reset() {
    clearTimer();
    setCopiedKey(null);
  }

  async function copy(text: string, key = 'default' as K): Promise<boolean> {
    try {
      await navigator.clipboard.writeText(text);
      // Cleared after the await, so an earlier copy that resolved meanwhile
      // cannot leave its timer running and end this one early.
      clearTimer();
      setCopiedKey(key);
      timerRef.current = window.setTimeout(() => {
        setCopiedKey(null);
        timerRef.current = null;
      }, COPY_FEEDBACK_MS);
      return true;
    } catch {
      reset();
      onError?.();
      return false;
    }
  }

  return { copied: copiedKey !== null, copiedKey, copy, reset };
}
