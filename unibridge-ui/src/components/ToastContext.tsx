import { useState, useCallback, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ToastContext } from './ToastContextValue';
import './Toast.css';

interface Toast {
  id: number;
  type: 'success' | 'error' | 'info';
  title: string;
  message?: string;
}

let nextId = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((toast: Omit<Toast, 'id'>) => {
    const id = nextId++;
    setToasts((prev) => [...prev, { ...toast, id }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 6000);
  }, []);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ addToast }}>
      {children}
      <div className="toast-container">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`toast toast--${toast.type}`}
            role={toast.type === 'error' ? 'alert' : 'status'}
            aria-live={toast.type === 'error' ? 'assertive' : 'polite'}
            aria-atomic="true"
          >
            <div className="toast-content">
              <span className="toast-title">{toast.title}</span>
              {toast.message && <pre className="toast-message">{toast.message}</pre>}
            </div>
            <button
              type="button"
              className="toast-close"
              aria-label={t('common.close')}
              title={t('common.close')}
              onClick={() => dismiss(toast.id)}
            >
              &times;
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
