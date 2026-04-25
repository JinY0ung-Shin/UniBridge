import { PermissionContext } from './PermissionContextValue';

export function PermissionProvider({
  permissions,
  loaded,
  children,
}: {
  permissions: string[];
  loaded: boolean;
  children: React.ReactNode;
}) {
  return (
    <PermissionContext.Provider value={{ permissions, loaded }}>
      {children}
    </PermissionContext.Provider>
  );
}
