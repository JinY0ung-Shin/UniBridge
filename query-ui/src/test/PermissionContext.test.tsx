import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import {
  PermissionProvider,
  usePermissions,
} from '../components/PermissionContext';
import { ProtectedRoute } from '../App';

/* ── Helper: render a component that displays the current permissions ── */

function PermissionsDisplay() {
  const { permissions: perms } = usePermissions();
  return (
    <div>
      <span data-testid="count">{perms.length}</span>
      {perms.map((p) => (
        <span key={p} data-testid="perm">
          {p}
        </span>
      ))}
    </div>
  );
}

/* ── Tests: usePermissions ── */

describe('usePermissions', () => {
  it('returns an empty array and loaded=false when used outside a provider', () => {
    function LoadedDisplay() {
      const { permissions: perms, loaded } = usePermissions();
      return <div><span data-testid="count">{perms.length}</span><span data-testid="loaded">{String(loaded)}</span></div>;
    }
    render(<LoadedDisplay />);
    expect(screen.getByTestId('count')).toHaveTextContent('0');
    expect(screen.getByTestId('loaded')).toHaveTextContent('false');
  });
});

/* ── Tests: PermissionProvider ── */

describe('PermissionProvider', () => {
  it('provides permissions to children', () => {
    const perms = ['query.execute', 'query.databases.read'];
    render(
      <PermissionProvider permissions={perms} loaded={true}>
        <PermissionsDisplay />
      </PermissionProvider>,
    );

    expect(screen.getByTestId('count')).toHaveTextContent('2');
    const items = screen.getAllByTestId('perm');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('query.execute');
    expect(items[1]).toHaveTextContent('query.databases.read');
  });
});

/* ── Tests: ProtectedRoute ── */

describe('ProtectedRoute', () => {
  it('renders children when the required permission is present', () => {
    render(
      <MemoryRouter initialEntries={['/protected']}>
        <PermissionProvider permissions={['query.execute', 'admin.roles.read']} loaded={true}>
          <Routes>
            <Route path="/" element={<div>Home</div>} />
            <Route
              path="/protected"
              element={
                <ProtectedRoute permission="query.execute">
                  <div>Secret Content</div>
                </ProtectedRoute>
              }
            />
          </Routes>
        </PermissionProvider>
      </MemoryRouter>,
    );

    expect(screen.getByText('Secret Content')).toBeInTheDocument();
    expect(screen.queryByText('Home')).not.toBeInTheDocument();
  });

  it('redirects to / when the required permission is missing', () => {
    render(
      <MemoryRouter initialEntries={['/protected']}>
        <PermissionProvider permissions={['query.databases.read']} loaded={true}>
          <Routes>
            <Route path="/" element={<div>Home</div>} />
            <Route
              path="/protected"
              element={
                <ProtectedRoute permission="query.execute">
                  <div>Secret Content</div>
                </ProtectedRoute>
              }
            />
          </Routes>
        </PermissionProvider>
      </MemoryRouter>,
    );

    expect(screen.getByText('Home')).toBeInTheDocument();
    expect(screen.queryByText('Secret Content')).not.toBeInTheDocument();
  });

  it('renders nothing when permissions are not yet loaded', () => {
    render(
      <MemoryRouter initialEntries={['/protected']}>
        <PermissionProvider permissions={[]} loaded={false}>
          <Routes>
            <Route path="/" element={<div>Home</div>} />
            <Route
              path="/protected"
              element={
                <ProtectedRoute permission="query.execute">
                  <div>Secret Content</div>
                </ProtectedRoute>
              }
            />
          </Routes>
        </PermissionProvider>
      </MemoryRouter>,
    );

    // When not loaded, ProtectedRoute should render nothing (not redirect, not show content)
    expect(screen.queryByText('Secret Content')).not.toBeInTheDocument();
    expect(screen.queryByText('Home')).not.toBeInTheDocument();
  });
});
