import { useCallback, useEffect, useId, useRef, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

interface ResourceModalProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
  closeLabel: string;
  closeOnOverlayClick?: boolean;
  closeOnEscape?: boolean;
  className?: string;
  style?: CSSProperties;
}

function ResourceModal({
  title,
  onClose,
  children,
  closeLabel,
  closeOnOverlayClick = true,
  closeOnEscape = true,
  className = '',
  style,
}: ResourceModalProps) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previouslyFocusedElement = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  const isVisuallyHidden = useCallback((element: HTMLElement): boolean => {
    let current: HTMLElement | null = element;
    while (current && current !== dialogRef.current) {
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden') return true;
      current = current.parentElement;
    }
    return false;
  }, []);

  const getFocusableElements = useCallback((): HTMLElement[] => {
    if (!dialogRef.current) return [];
    return Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter((element) => {
        if (element.hasAttribute('disabled')) return false;
        if (element.hasAttribute('hidden')) return false;
        if (element.getAttribute('type') === 'hidden') return false;
        if (element.getAttribute('aria-hidden') === 'true') return false;
        if (isVisuallyHidden(element)) return false;
        return true;
      });
  }, [isVisuallyHidden]);

  useEffect(() => {
    const restoreFocusTo = previouslyFocusedElement.current;
    const active = document.activeElement;
    if (!(active instanceof HTMLElement) || !dialogRef.current?.contains(active)) {
      const initial = getFocusableElements().find((element) => element !== closeButtonRef.current);
      (initial ?? closeButtonRef.current)?.focus();
    }

    return () => {
      if (restoreFocusTo && document.contains(restoreFocusTo)) {
        restoreFocusTo.focus();
      }
    };
  }, [getFocusableElements]);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      if (!closeOnEscape) return;
      event.preventDefault();
      onClose();
      return;
    }

    if (event.key !== 'Tab') return;

    const focusable = getFocusableElements();
    if (focusable.length === 0) return;

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;

    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="modal-overlay" onClick={closeOnOverlayClick ? onClose : undefined}>
      <div
        ref={dialogRef}
        className={`modal${className ? ` ${className}` : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={handleKeyDown}
        style={style}
      >
        <div className="modal-header">
          <h2 id={titleId}>{title}</h2>
          <button
            ref={closeButtonRef}
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label={closeLabel}
          >
            &times;
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export default ResourceModal;
