import { useState, useEffect, type ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import { getToken, getAuthRoles, getCurrentUser } from '../api/client';
import './Layout.css';

const navItems = [
  { to: '/', label: 'Dashboard', section: 'data', permission: null },
  { to: '/connections', label: 'Connections', section: 'data', permission: 'query.databases.read' },
  { to: '/permissions', label: 'Permissions', section: 'data', permission: 'query.permissions.read' },
  { to: '/audit-logs', label: 'Audit Logs', section: 'data', permission: 'query.audit.read' },
  { to: '/query', label: 'Query Playground', section: 'data', permission: 'query.execute' },
  { to: '/gateway/routes', label: 'Gateway Routes', section: 'gateway', permission: 'gateway.routes.read' },
  { to: '/gateway/upstreams', label: 'Gateway Upstreams', section: 'gateway', permission: 'gateway.upstreams.read' },
  { to: '/gateway/consumers', label: 'Gateway Consumers', section: 'gateway', permission: 'gateway.consumers.read' },
  { to: '/gateway/monitoring', label: 'Gateway Monitoring', section: 'gateway', permission: 'gateway.monitoring.read' },
  { to: '/roles', label: 'Roles', section: 'admin', permission: 'admin.roles.read' },
];

interface LayoutProps {
  children: ReactNode;
}

function Layout({ children }: LayoutProps) {
  const [hasToken, setHasToken] = useState(() => !!localStorage.getItem('auth_token'));
  const [loginUsername, setLoginUsername] = useState('');
  const [loginRole, setLoginRole] = useState('');
  const [loginError, setLoginError] = useState('');
  const [loginLoading, setLoginLoading] = useState(false);
  const [availableRoles, setAvailableRoles] = useState<string[]>([]);
  const [userPermissions, setUserPermissions] = useState<string[]>([]);

  // Load available roles for login form
  useEffect(() => {
    if (!hasToken) {
      getAuthRoles().then((roles) => {
        setAvailableRoles(roles);
        if (roles.length > 0 && !loginRole) setLoginRole(roles[0]);
      }).catch(() => setAvailableRoles(['admin']));
    }
  }, [hasToken]);

  // Load current user permissions after login
  useEffect(() => {
    if (hasToken) {
      getCurrentUser().then((user) => {
        setUserPermissions(user.permissions);
      }).catch(() => setUserPermissions([]));
    }
  }, [hasToken]);

  function hasPermission(perm: string | null): boolean {
    if (perm === null) return true;
    return userPermissions.includes(perm);
  }

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    if (!loginUsername.trim()) return;
    setLoginLoading(true);
    setLoginError('');
    try {
      const { access_token } = await getToken(loginUsername.trim(), loginRole);
      localStorage.setItem('auth_token', access_token);
      setHasToken(true);
    } catch {
      setLoginError('Failed to obtain token. Please try again.');
    } finally {
      setLoginLoading(false);
    }
  }

  if (!hasToken) {
    return (
      <div className="login-overlay">
        <form className="login-form" onSubmit={handleLogin}>
          <h2>API Hub Login</h2>
          <div className="login-field">
            <label htmlFor="login-username">Username</label>
            <input
              id="login-username"
              type="text"
              value={loginUsername}
              onChange={(e) => setLoginUsername(e.target.value)}
              placeholder="Enter username"
              autoFocus
              required
            />
          </div>
          <div className="login-field">
            <label htmlFor="login-role">Role</label>
            <select
              id="login-role"
              value={loginRole}
              onChange={(e) => setLoginRole(e.target.value)}
            >
              {availableRoles.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
          {loginError && <div className="login-error">{loginError}</div>}
          <button type="submit" className="login-btn" disabled={loginLoading}>
            {loginLoading ? 'Logging in...' : 'Login'}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="sidebar-logo">
            <div className="sidebar-logo-icon">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M3 5h10M3 8h10M3 11h7" stroke="#000" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </div>
            <span className="sidebar-title">API Hub</span>
          </div>
        </div>
        <nav className="sidebar-nav">
          {navItems.filter((item) => hasPermission(item.permission)).map((item, index, filtered) => {
            const prevItem = filtered[index - 1];
            const showDivider = prevItem && prevItem.section !== item.section;
            return (
              <span key={item.to}>
                {showDivider && <div className="nav-divider" />}
                <NavLink
                  to={item.to}
                  end={item.to === '/'}
                  className={({ isActive }) =>
                    `nav-link ${isActive ? 'nav-link--active' : ''}`
                  }
                >
                  <span className="nav-icon">
                    {item.label === 'Dashboard' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <rect x="1" y="1" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="10" y="1" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="1" y="10" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="10" y="10" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
                      </svg>
                    )}
                    {item.label === 'Connections' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <circle cx="5" cy="5" r="3" stroke="currentColor" strokeWidth="1.5" />
                        <circle cx="13" cy="13" r="3" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M7.5 7.5l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.label === 'Permissions' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <rect x="3" y="8" width="12" height="8" rx="2" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M6 8V5a3 3 0 016 0v3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.label === 'Audit Logs' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M5 1h8l4 4v12H1V1h4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" fill="none" />
                        <path d="M5 7h8M5 10h8M5 13h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.label === 'Query Playground' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M2 4l5 4-5 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M9 14h7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.label === 'Gateway Routes' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M1 9h16M9 1v16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        <circle cx="9" cy="9" r="3" stroke="currentColor" strokeWidth="1.5" />
                      </svg>
                    )}
                    {item.label === 'Gateway Upstreams' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <rect x="1" y="3" width="16" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="1" y="11" width="16" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
                        <circle cx="4" cy="5" r="1" fill="currentColor" />
                        <circle cx="4" cy="13" r="1" fill="currentColor" />
                      </svg>
                    )}
                    {item.label === 'Gateway Consumers' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <circle cx="9" cy="6" r="3.5" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M2 16c0-3.3 3.1-6 7-6s7 2.7 7 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.label === 'Gateway Monitoring' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M1 14l4-6 3 3 4-7 5 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M1 17h16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.label === 'Roles' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M9 2l2 2-2 2-2-2 2-2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                        <path d="M3 9h12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        <rect x="2" y="12" width="5" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="11" y="12" width="5" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
                      </svg>
                    )}
                  </span>
                  {item.label}
                </NavLink>
              </span>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          <span className="sidebar-version">Query Service v1.0</span>
        </div>
      </aside>
      <main className="main-content">{children}</main>
    </div>
  );
}

export default Layout;
