import { createContext, useContext } from 'react';

interface PermissionState {
  permissions: string[];
  loaded: boolean;
}

const PermissionContext = createContext<PermissionState>({ permissions: [], loaded: false });

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

export function usePermissions() {
  return useContext(PermissionContext);
}
