import { createContext } from 'react';

export interface AuthContextType {
  authenticated: boolean;
  token: string | null;
  username: string | null;
  /** Application role from token claims ('admin' | 'user'); null when unresolved. */
  appRole: string | null;
  initialized: boolean;
  logout: () => void;
}

export const AuthContext = createContext<AuthContextType>({
  authenticated: false,
  token: null,
  username: null,
  appRole: null,
  initialized: false,
  logout: () => {},
});
