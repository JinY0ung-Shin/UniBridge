vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAlertStatus: vi.fn(),
  getUsers: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAlertStatus, getUsers } from '../api/client';
import OpsSummary from '../pages/dashboard/OpsSummary';
import { renderWithProviders } from './helpers';

const mockStatus = vi.mocked(getAlertStatus);
const mockUsers = vi.mocked(getUsers);

function statusResult(items: unknown[], globalMutedUntil: string | null = null) {
  return { items, global_muted_until: globalMutedUntil } as Awaited<
    ReturnType<typeof getAlertStatus>
  >;
}

const STATUS_ITEMS = [
  { target: 'db1', type: 'db_health', status: 'alert', since: null },
  { target: 'db2', type: 'db_health', status: 'alert', since: null, muted: true },
  { target: 'node-1', type: 'server_down', status: 'alert', since: null },
  { target: 'node-2', type: 'server_down', status: 'ok', since: null },
  { target: 'node-3', type: 'server_down', status: 'ok', since: null },
  { target: 'payments', type: 'external_service_down', status: 'ok', since: null },
];

/** Value rendered inside the card whose label matches `label`. */
function cardValue(label: string): string {
  const card = screen.getByText(label).closest('.summary-card');
  return card?.querySelector('.summary-card__value')?.textContent ?? '';
}

describe('Dashboard OpsSummary', () => {
  beforeEach(() => {
    mockStatus.mockReset();
    mockUsers.mockReset();
    mockStatus.mockResolvedValue(statusResult(STATUS_ITEMS));
    mockUsers.mockResolvedValue({
      total: 4,
      users: [
        { id: '1', username: 'a', email: null, enabled: true, role: 'admin', createdTimestamp: 1 },
        { id: '2', username: 'b', email: null, enabled: true, role: null, createdTimestamp: 2 },
        { id: '3', username: 'c', email: null, enabled: true, role: null, createdTimestamp: 3 },
        { id: '4', username: 'd', email: null, enabled: true, role: 'user', createdTimestamp: 4 },
      ],
    });
  });

  it('renders alert, server, external and pending-user cards', async () => {
    renderWithProviders(<OpsSummary />);
    // db1 + node-1 fire unmuted; db2 fires but is muted
    await waitFor(() => expect(cardValue('Firing alerts')).toBe('2'));

    expect(screen.getByText('1 muted excluded')).toBeInTheDocument();
    expect(cardValue('Servers')).toBe('1');
    expect(screen.getByText('3 tracked')).toBeInTheDocument();
    expect(cardValue('External services')).toBe('0');

    await waitFor(() => expect(cardValue('Pending users')).toBe('2'));
    expect(screen.getByText('4 users total')).toBeInTheDocument();
  });

  it('links each card to its detail page', async () => {
    renderWithProviders(<OpsSummary />);
    await waitFor(() => expect(screen.getByText('Firing alerts')).toBeInTheDocument());

    const href = (label: string) =>
      screen.getByText(label).closest('a')?.getAttribute('href');
    expect(href('Firing alerts')).toBe('/alerts/status');
    expect(href('Servers')).toBe('/servers');
    expect(href('External services')).toBe('/external/monitoring');
    expect(href('Pending users')).toBe('/users');
  });

  it('reports a global mute instead of the muted-count note', async () => {
    mockStatus.mockResolvedValue(statusResult(STATUS_ITEMS, '2099-01-01T00:00:00Z'));
    renderWithProviders(<OpsSummary />);
    await waitFor(() => expect(screen.getByText('All alerts muted')).toBeInTheDocument());
    expect(screen.queryByText('1 muted excluded')).not.toBeInTheDocument();
  });

  it('says all healthy when nothing is firing', async () => {
    mockStatus.mockResolvedValue(statusResult([
      { target: 'db1', type: 'db_health', status: 'ok', since: null },
    ]));
    renderWithProviders(<OpsSummary />);
    await waitFor(() => expect(screen.getByText('All healthy')).toBeInTheDocument());
    // no server_down rows at all
    expect(screen.getAllByText('None tracked')).toHaveLength(2);
  });

  it('distinguishes "no data yet" from "nothing tracked"', async () => {
    // the checker publishes no rows until its first cycle completes, so an
    // empty list must not read as "0 servers configured"
    mockStatus.mockResolvedValue(statusResult([]));
    renderWithProviders(<OpsSummary />);
    await waitFor(() => expect(screen.getAllByText('Awaiting first check')).toHaveLength(3));
    expect(screen.queryByText('None tracked')).not.toBeInTheDocument();
    expect(screen.queryByText('All healthy')).not.toBeInTheDocument();
  });

  it('hides alert cards without alerts.read', async () => {
    renderWithProviders(<OpsSummary />, { permissions: ['admin.users.read'] });
    await waitFor(() => expect(screen.getByText('Pending users')).toBeInTheDocument());

    expect(screen.queryByText('Firing alerts')).not.toBeInTheDocument();
    expect(screen.queryByText('Servers')).not.toBeInTheDocument();
    expect(screen.queryByText('External services')).not.toBeInTheDocument();
    expect(mockStatus).not.toHaveBeenCalled();
  });

  it('hides the pending-user card without admin.users.read', async () => {
    renderWithProviders(<OpsSummary />, { permissions: ['alerts.read'] });
    await waitFor(() => expect(screen.getByText('Firing alerts')).toBeInTheDocument());

    expect(screen.queryByText('Pending users')).not.toBeInTheDocument();
    expect(mockUsers).not.toHaveBeenCalled();
  });

  it('renders nothing without either permission', () => {
    renderWithProviders(<OpsSummary />, { permissions: [] });
    expect(screen.queryByText('Operations')).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('shows a per-card error when its query fails', async () => {
    mockStatus.mockRejectedValue(new Error('boom'));
    renderWithProviders(<OpsSummary />);
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBe(3));
    expect(cardValue('Firing alerts')).toBe('—');
    // the users card is unaffected
    await waitFor(() => expect(cardValue('Pending users')).toBe('2'));
  });
});
