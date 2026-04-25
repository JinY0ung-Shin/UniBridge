import { createContext } from 'react';

export interface PermissionState {
  permissions: string[];
  loaded: boolean;
}

export const PermissionContext = createContext<PermissionState>({
  permissions: [],
  loaded: false,
});
