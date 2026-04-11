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
  const [error, setError] = useState('');
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
      .catch((err) => {
        console.error('Keycloak init failed:', err);
        setError(`Authentication service unavailable. Check that Keycloak is reachable at ${keycloak.authServerUrl}`);
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

  if (!initialized) return null;

  if (error || !authenticated) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100vh', background: 'var(--bg-root)', color: 'var(--text-primary)',
        fontFamily: 'var(--font-sans)', padding: '2rem',
      }}>
        <div style={{ textAlign: 'center', maxWidth: 500 }}>
          <h2 style={{ marginBottom: '1rem' }}>Authentication Error</h2>
          <p style={{ color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
            {error || 'Authentication failed. Please try again.'}
          </p>
          <button
            onClick={() => window.location.reload()}
            style={{
              marginTop: '1.5rem', padding: '8px 24px',
              background: 'var(--bg-tertiary)', color: 'var(--text-primary)', border: '1px solid var(--border-hover)',
              borderRadius: 6, cursor: 'pointer', fontSize: 14,
            }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

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
      {children}
    </AuthContext.Provider>
  );
}
