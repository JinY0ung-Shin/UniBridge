import { createContext } from 'react';

export interface AuthContextType {
  authenticated: boolean;
  token: string | null;
  username: string | null;
  initialized: boolean;
  logout: () => void;
}

export const AuthContext = createContext<AuthContextType>({
  authenticated: false,
  token: null,
  username: null,
  initialized: false,
  logout: () => {},
});
