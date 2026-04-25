import { useContext } from 'react';
import { ToastContext } from './ToastContextValue';

export function useToast() {
  return useContext(ToastContext);
}
