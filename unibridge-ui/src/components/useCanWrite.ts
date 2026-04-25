import { usePermissions } from './usePermissions';

export function useCanWrite(permissionKey: string): boolean {
  const { permissions } = usePermissions();
  return permissions.includes(permissionKey);
}
