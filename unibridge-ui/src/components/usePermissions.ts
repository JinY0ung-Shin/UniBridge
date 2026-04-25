import { useContext } from 'react';
import { PermissionContext } from './PermissionContextValue';

export function usePermissions() {
  return useContext(PermissionContext);
}
