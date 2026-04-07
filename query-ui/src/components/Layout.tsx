import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import './Layout.css';

const navItems = [
  { to: '/', label: 'Dashboard', icon: '/' },
  { to: '/connections', label: 'Connections', icon: '/' },
  { to: '/permissions', label: 'Permissions', icon: '/' },
  { to: '/audit-logs', label: 'Audit Logs', icon: '/' },
  { to: '/query', label: 'Query Playground', icon: '/' },
];

interface LayoutProps {
  children: ReactNode;
}

function Layout({ children }: LayoutProps) {
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="sidebar-logo">
            <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
              <rect width="28" height="28" rx="6" fill="#4361ee" />
              <path
                d="M7 10h14M7 14h14M7 18h10"
                stroke="#fff"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
            <span className="sidebar-title">API Hub Admin</span>
          </div>
        </div>
        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
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
              </span>
              {item.label}
            </NavLink>
          ))}
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
