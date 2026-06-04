import { useEffect, useState, type ReactNode } from 'react';
import { NavLink, Link, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { getCurrentUser } from '../api/client';
import { useAuth } from './useAuth';
import { PermissionProvider } from './PermissionContext';
import { navItems, hasNavPermission } from './navItems';
import SettingsModal from './SettingsModal';
import './Layout.css';

interface LayoutProps {
  children: ReactNode;
}

function Layout({ children }: LayoutProps) {
  const { t } = useTranslation();
  const { username, logout } = useAuth();
  const location = useLocation();
  const [showSettings, setShowSettings] = useState(false);
  const [navOpenPath, setNavOpenPath] = useState<string | null>(null);

  const permissionsQuery = useQuery({
    queryKey: ['current-user'],
    queryFn: getCurrentUser,
    retry: 1,
    retryDelay: 250,
  });
  const userPermissions = permissionsQuery.data?.permissions ?? [];
  const permissionsLoaded = permissionsQuery.isSuccess;

  const hasPermission = (perm: Parameters<typeof hasNavPermission>[0]) =>
    hasNavPermission(perm, userPermissions);
  const isNavOpen = navOpenPath === location.pathname;

  useEffect(() => {
    if (!isNavOpen) return;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isNavOpen]);

  return (
    <div className={`layout ${isNavOpen ? 'layout--nav-open' : ''}`}>
      <header className="mobile-topbar">
        <button
          type="button"
          className="mobile-nav-toggle"
          aria-label={t('common.openNavigation')}
          aria-controls="app-sidebar"
          aria-expanded={isNavOpen}
          onClick={() => setNavOpenPath(location.pathname)}
        >
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </button>
        <Link to="/" className="mobile-brand" aria-label="UniBridge">
          <span className="sidebar-logo-icon" aria-hidden="true">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M3 5h10M3 8h10M3 11h7" stroke="var(--text-inverse)" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </span>
        </Link>
        {username && <span className="mobile-username">{username}</span>}
      </header>
      <aside className="sidebar" id="app-sidebar">
        <div className="sidebar-header">
          <Link to="/" className="sidebar-logo">
            <div className="sidebar-logo-icon">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M3 5h10M3 8h10M3 11h7" stroke="var(--text-inverse)" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </div>
            <span className="sidebar-title">UniBridge</span>
          </Link>
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
                  onClick={() => setNavOpenPath(null)}
                  className={({ isActive }) =>
                    `nav-link ${isActive ? 'nav-link--active' : ''}`
                  }
                >
                  <span className="nav-icon">
                    {item.icon === 'Dashboard' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <rect x="1" y="1" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="10" y="1" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="1" y="10" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="10" y="10" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
                      </svg>
                    )}
                    {item.icon === 'Connections' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <circle cx="5" cy="5" r="3" stroke="currentColor" strokeWidth="1.5" />
                        <circle cx="13" cy="13" r="3" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M7.5 7.5l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.icon === 'Permissions' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <rect x="3" y="8" width="12" height="8" rx="2" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M6 8V5a3 3 0 016 0v3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.icon === 'Audit Logs' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M5 1h8l4 4v12H1V1h4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" fill="none" />
                        <path d="M5 7h8M5 10h8M5 13h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.icon === 'Query Playground' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M2 4l5 4-5 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M9 14h7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.icon === 'Query Settings' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M6.5 1h5l.4 2.1a6 6 0 011.3.7L15.3 3l2 2.6-1.7 1.4a6 6 0 010 1.4l1.7 1.6-2 2.6-2.1-.8a6 6 0 01-1.3.7L11.5 15h-5l-.4-2.1a6 6 0 01-1.3-.7L2.7 13 .7 10.4l1.7-1.4a6 6 0 010-1.4L.7 5.6l2-2.6 2.1.8a6 6 0 011.3-.7L6.5 1z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
                        <circle cx="9" cy="8" r="2.5" stroke="currentColor" strokeWidth="1.2" />
                      </svg>
                    )}
                    {item.icon === 'Query Templates' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <rect x="2" y="2" width="14" height="14" rx="2" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M5 6h8M5 9h5M5 12h7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.icon === 'S3' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M3 4h12v10H3z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                        <path d="M7 8h4M7 11h2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        <path d="M3 4l3-2h6l3 2" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                      </svg>
                    )}
                    {item.icon === 'Gateway Routes' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M1 9h16M9 1v16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        <circle cx="9" cy="9" r="3" stroke="currentColor" strokeWidth="1.5" />
                      </svg>
                    )}
                    {item.icon === 'Gateway Upstreams' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <rect x="1" y="3" width="16" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="1" y="11" width="16" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
                        <circle cx="4" cy="5" r="1" fill="currentColor" />
                        <circle cx="4" cy="13" r="1" fill="currentColor" />
                      </svg>
                    )}
                    {item.icon === 'Gateway Monitoring' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M1 14l4-6 3 3 4-7 5 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M1 17h16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.icon === 'LLM Monitoring' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M3 14V7l3-2 3 4 3-6 3 5v6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                        <circle cx="14" cy="4" r="2" stroke="currentColor" strokeWidth="1.5" />
                      </svg>
                    )}
                    {item.icon === 'API Keys' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M7 2a5 5 0 014.33 7.5L16 14.17V17h-3v-2h-2v-2l-1.17-1.17A5 5 0 117 2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                        <circle cx="6" cy="7" r="1.5" fill="currentColor" />
                      </svg>
                    )}
                    {item.icon === 'Roles' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M9 2l2 2-2 2-2-2 2-2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                        <path d="M3 9h12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        <rect x="2" y="12" width="5" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
                        <rect x="11" y="12" width="5" height="4" rx="1" stroke="currentColor" strokeWidth="1.5" />
                      </svg>
                    )}
                    {item.icon === 'Users' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <circle cx="9" cy="5" r="3" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M3 16c0-2.8 2.7-5 6-5s6 2.2 6 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.icon === 'Alert Status' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M9 1.5a7.5 7.5 0 107.5 7.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        <circle cx="9" cy="9" r="2" fill="currentColor" />
                        <circle cx="14.5" cy="3.5" r="2" fill="currentColor" />
                      </svg>
                    )}
                    {item.icon === 'Alert Settings' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <path d="M9 2L10.5 6H7.5L9 2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                        <path d="M5 8h8v5a2 2 0 01-2 2H7a2 2 0 01-2-2V8z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
                        <path d="M9 15v2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {item.icon === 'Alert History' && (
                      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                        <circle cx="9" cy="9" r="7" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M9 5v4l3 2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    )}
                  </span>
                  {t(item.labelKey)}
                </NavLink>
              </span>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          {username && (
            <div className="sidebar-user-section">
              <span className="sidebar-username">{username}</span>
              <div className="sidebar-user-actions">
                <button className="sidebar-settings-btn" onClick={() => setShowSettings(true)} title={t('settings.title')}>
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <path d="M6.5 1h3l.4 2.1a5.5 5.5 0 011.3.7L13.3 3l1.5 2.6-1.7 1.4a5.6 5.6 0 010 1.4l1.7 1.6-1.5 2.6-2.1-.8a5.5 5.5 0 01-1.3.7L9.5 15h-3l-.4-2.1a5.5 5.5 0 01-1.3-.7L2.7 13 1.2 10.4l1.7-1.4a5.6 5.6 0 010-1.4L1.2 5.6 2.7 3l2.1.8a5.5 5.5 0 011.3-.7L6.5 1z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
                    <circle cx="8" cy="8" r="2" stroke="currentColor" strokeWidth="1.2" />
                  </svg>
                </button>
                <button className="sidebar-logout-btn" onClick={logout}>{t('common.logout')}</button>
              </div>
            </div>
          )}
          <span className="sidebar-version">Query Service v1.0</span>
        </div>
      </aside>
      <button
        type="button"
        className="nav-scrim"
        aria-label={t('common.closeNavigation')}
        onClick={() => setNavOpenPath(null)}
      />
      <main className="main-content">
        <PermissionProvider permissions={userPermissions} loaded={permissionsLoaded}>
          {permissionsQuery.isError && (
            <div className="permission-error-banner">
              <span>{t('permissions.loadFailed')}</span>
              <button className="btn btn-sm btn-secondary" onClick={() => permissionsQuery.refetch()}>
                {t('common.retry')}
              </button>
            </div>
          )}
          {children}
        </PermissionProvider>
      </main>
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </div>
  );
}

export default Layout;
