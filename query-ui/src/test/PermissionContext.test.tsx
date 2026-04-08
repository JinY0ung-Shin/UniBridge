import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MemoryRouter, Navigate, Routes, Route } from 'react-router-dom';
import {
  PermissionProvider,
  usePermissions,
} from '../components/PermissionContext';

/* ── Helper: render a component that displays the current permissions ── */

function PermissionsDisplay() {
  const perms = usePermissions();
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

/* ── Recreate ProtectedRoute (not exported from App.tsx) ── */

function ProtectedRoute({
  permission,
  children,
}: {
  permission: string;
  children: React.ReactNode;
}) {
  const perms = usePermissions();
  if (perms.length > 0 && !perms.includes(permission)) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}

/* ── Tests: usePermissions ── */

describe('usePermissions', () => {
  it('returns an empty array when used outside a provider', () => {
    render(<PermissionsDisplay />);
    expect(screen.getByTestId('count')).toHaveTextContent('0');
  });
});

/* ── Tests: PermissionProvider ── */

describe('PermissionProvider', () => {
  it('provides permissions to children', () => {
    const perms = ['query.execute', 'query.databases.read'];
    render(
      <PermissionProvider permissions={perms}>
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
        <PermissionProvider permissions={['query.execute', 'admin.roles.read']}>
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
        <PermissionProvider permissions={['query.databases.read']}>
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

  it('renders children when permissions array is empty (loading state)', () => {
    render(
      <MemoryRouter initialEntries={['/protected']}>
        <PermissionProvider permissions={[]}>
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

    // When perms.length === 0, ProtectedRoute should NOT redirect (loading state)
    expect(screen.getByText('Secret Content')).toBeInTheDocument();
    expect(screen.queryByText('Home')).not.toBeInTheDocument();
  });
});
