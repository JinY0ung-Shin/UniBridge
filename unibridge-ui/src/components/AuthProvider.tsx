import { useState, useEffect, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import keycloak from '../keycloak';
import { setApiAuthReady } from '../api/client';
import { AuthContext } from './AuthContext';
import { resolveAppRole, type TokenClaims } from './appRole';

const getAppRole = (): string | null => resolveAppRole(keycloak.tokenParsed as TokenClaims);

const centeredBox: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  height: '100vh', background: 'var(--bg-root)', color: 'var(--text-primary)',
  fontFamily: 'var(--font-sans)', padding: '2rem',
};

const actionBtn: React.CSSProperties = {
  marginTop: '1.5rem', padding: '8px 24px',
  background: 'var(--bg-tertiary)', color: 'var(--text-primary)', border: '1px solid var(--border-hover)',
  borderRadius: 6, cursor: 'pointer', fontSize: 14,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [authenticated, setAuthenticated] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const [error, setError] = useState('');
  // Role kept in state (not read inline at render) so the gate reacts to token
  // refreshes — an admin granting/revoking a role is reflected on the next refresh.
  const [appRole, setAppRole] = useState<string | null>(null);
  const initCalled = useRef(false);

  useEffect(() => {
    if (initCalled.current) return;
    initCalled.current = true;

    keycloak
      .init({ onLoad: 'login-required', checkLoginIframe: false, pkceMethod: 'S256' })
      .then((auth) => {
        setAuthenticated(auth);
        setApiAuthReady(auth);
        if (auth) setAppRole(getAppRole());
        setInitialized(true);
      })
      .catch((err) => {
        console.error('Keycloak init failed:', err);
        setApiAuthReady(false);
        setError(`Authentication service unavailable. Check that Keycloak is reachable at ${keycloak.authServerUrl}`);
        setInitialized(true);
      });

    // Re-evaluate the role whenever the token is (re)issued so approval/revocation
    // applied by an admin takes effect without a manual page reload.
    keycloak.onAuthSuccess = () => setAppRole(getAppRole());
    keycloak.onAuthRefreshSuccess = () => setAppRole(getAppRole());

    keycloak.onTokenExpired = () => {
      keycloak.updateToken(30).catch(() => {
        setApiAuthReady(false);
        keycloak.logout();
      });
    };
  }, []);

  const logout = () => {
    setApiAuthReady(false);
    keycloak.logout({ redirectUri: window.location.origin });
  };

  // Force a token refresh to pick up a role just granted by an admin. The refreshed
  // token re-evaluates the gate via state (no full-page reload needed).
  const recheckApproval = () => {
    keycloak.updateToken(-1)
      .then(() => setAppRole(getAppRole()))
      .catch(() => { /* transient: stay on the pending screen */ });
  };

  if (!initialized) return null;

  if (error || !authenticated) {
    return (
      <div style={centeredBox}>
        <div style={{ textAlign: 'center', maxWidth: 500 }}>
          <h2 style={{ marginBottom: '1rem' }}>Authentication Error</h2>
          <p role="alert" style={{ color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
            {error || 'Authentication failed. Please try again.'}
          </p>
          <button type="button" onClick={() => window.location.reload()} style={actionBtn}>
            {t('common.retry')}
          </button>
        </div>
      </div>
    );
  }

  // Authenticated but no application role yet → awaiting admin approval.
  if (!appRole) {
    const username = (keycloak.tokenParsed?.preferred_username as string) || '';
    return (
      <div style={centeredBox}>
        <div style={{ textAlign: 'center', maxWidth: 520 }}>
          <h2 style={{ marginBottom: '1rem' }}>{t('pending.title')}</h2>
          <p role="status" aria-live="polite" style={{ color: 'var(--text-tertiary)', lineHeight: 1.6 }}>
            {t('pending.message')}
          </p>
          {username && (
            <p style={{ color: 'var(--text-tertiary)', marginTop: '0.5rem', fontSize: 13 }}>
              {t('pending.account', { username })}
            </p>
          )}
          <div style={{ display: 'flex', gap: 12, justifyContent: 'center' }}>
            <button type="button" onClick={recheckApproval} style={actionBtn}>{t('pending.recheck')}</button>
            <button type="button" onClick={logout} style={actionBtn}>{t('common.logout')}</button>
          </div>
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
        appRole,
        initialized,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}
