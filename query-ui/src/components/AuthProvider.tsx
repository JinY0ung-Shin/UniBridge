import { useState, useEffect, useRef, createContext, useContext, type ReactNode } from 'react';
import keycloak from '../keycloak';

interface AuthContextType {
  authenticated: boolean;
  token: string | null;
  username: string | null;
  initialized: boolean;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType>({
  authenticated: false,
  token: null,
  username: null,
  initialized: false,
  logout: () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authenticated, setAuthenticated] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const initCalled = useRef(false);

  useEffect(() => {
    if (initCalled.current) return;
    initCalled.current = true;

    keycloak
      .init({ onLoad: 'login-required', checkLoginIframe: false, pkceMethod: 'S256' })
      .then((auth) => {
        setAuthenticated(auth);
        setInitialized(true);
      })
      .catch(() => {
        setInitialized(true);
      });

    keycloak.onTokenExpired = () => {
      keycloak.updateToken(30).catch(() => {
        keycloak.logout();
      });
    };
  }, []);

  const logout = () => {
    keycloak.logout({ redirectUri: window.location.origin });
  };

  return (
    <AuthContext.Provider
      value={{
        authenticated,
        token: keycloak.token || null,
        username: keycloak.tokenParsed?.preferred_username as string || null,
        initialized,
        logout,
      }}
    >
      {initialized ? children : null}
    </AuthContext.Provider>
  );
}
