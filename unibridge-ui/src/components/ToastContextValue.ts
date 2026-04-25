import { createContext } from 'react';

interface ToastContextValue {
  addToast: (toast: { type: 'success' | 'error' | 'info'; title: string; message?: string }) => void;
}

export const ToastContext = createContext<ToastContextValue>({ addToast: () => {} });
