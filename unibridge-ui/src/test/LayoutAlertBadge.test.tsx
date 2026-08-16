vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getCurrentUser: vi.fn(),
  getAlertStatus: vi.fn(),
}));

vi.mock('../components/useAuth', () => ({
  useAuth: () => ({ username: 'tester', logout: vi.fn() }),
}));

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAlertStatus, getCurrentUser } from '../api/client';
import Layout from '../components/Layout';
import { renderWithProviders } from './helpers';

const mockUser = vi.mocked(getCurrentUser);
const mockStatus = vi.mocked(getAlertStatus);

function statusResult(items: unknown[], globalMutedUntil: string | null = null) {
  return { items, global_muted_until: globalMutedUntil } as Awaited<
    ReturnType<typeof getAlertStatus>
  >;
}

function alertsNavLink() {
  return screen.getByRole('link', { name: /Alert Status/ });
}

describe('Layout alert badge', () => {
  beforeEach(() => {
    mockUser.mockReset();
    mockStatus.mockReset();
    mockUser.mockResolvedValue({
      username: 'tester',
      role: 'admin',
      permissions: ['dashboard.read', 'alerts.read'],
    } as Awaited<ReturnType<typeof getCurrentUser>>);
    mockStatus.mockResolvedValue(statusResult([]));
  });

  it('counts unmuted firing alerts on the alerts nav item', async () => {
    mockStatus.mockResolvedValue(statusResult([
      { target: 'db1', type: 'db_health', status: 'alert', since: null },
      { target: 'db2', type: 'db_health', status: 'alert', since: null },
      { target: 'db3', type: 'db_health', status: 'alert', since: null, muted: true },
      { target: 'db4', type: 'db_health', status: 'ok', since: null },
    ]));
    renderWithProviders(<Layout><div /></Layout>);

    await waitFor(() => expect(alertsNavLink().querySelector('.nav-badge')).not.toBeNull());
    const badge = alertsNavLink().querySelector('.nav-badge');
    expect(badge).toHaveTextContent('2');
    expect(badge).toHaveClass('nav-badge--alert');
  });

  it('shows no badge when nothing is firing', async () => {
    mockStatus.mockResolvedValue(statusResult([
      { target: 'db1', type: 'db_health', status: 'ok', since: null },
    ]));
    renderWithProviders(<Layout><div /></Layout>);

    await waitFor(() => expect(alertsNavLink()).toBeInTheDocument());
    expect(alertsNavLink().querySelector('.nav-badge')).toBeNull();
  });

  it('shows a muted indicator instead of the count during a global mute', async () => {
    mockStatus.mockResolvedValue(statusResult([
      { target: 'db1', type: 'db_health', status: 'alert', since: null },
    ], '2099-01-01T00:00:00Z'));
    renderWithProviders(<Layout><div /></Layout>);

    await waitFor(() => expect(alertsNavLink().querySelector('.nav-badge')).not.toBeNull());
    const badge = alertsNavLink().querySelector('.nav-badge');
    expect(badge).toHaveClass('nav-badge--muted');
    expect(badge).toHaveTextContent('Muted');
  });

  it('skips the status query entirely without alerts.read', async () => {
    mockUser.mockResolvedValue({
      username: 'tester',
      role: 'viewer',
      permissions: ['dashboard.read'],
    } as Awaited<ReturnType<typeof getCurrentUser>>);
    renderWithProviders(<Layout><div /></Layout>);

    await waitFor(() => expect(mockUser).toHaveBeenCalled());
    expect(screen.queryByRole('link', { name: /Alert Status/ })).not.toBeInTheDocument();
    expect(mockStatus).not.toHaveBeenCalled();
  });
});
