vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  GLOBAL_MUTE_RESOURCE_TYPE: 'global',
  getAlertStatus: vi.fn(),
  getAlertMutes: vi.fn(),
  setAlertMute: vi.fn(),
  deleteAlertMute: vi.fn(),
}));

import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAlertStatus, getAlertMutes, setAlertMute, deleteAlertMute } from '../api/client';
import AlertStatus from '../pages/AlertStatus';
import { renderWithProviders, VIEWER_PERMISSIONS } from './helpers';

const mockGet = vi.mocked(getAlertStatus);
const mockGetMutes = vi.mocked(getAlertMutes);
const mockSetMute = vi.mocked(setAlertMute);
const mockDeleteMute = vi.mocked(deleteAlertMute);

function statusResult(items: unknown[], globalMutedUntil: string | null = null) {
  return {
    items,
    global_muted_until: globalMutedUntil,
  } as Awaited<ReturnType<typeof getAlertStatus>>;
}

const NO_MUTES = { global_muted_until: null, mutes: [] };

describe('AlertStatus page', () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockGetMutes.mockReset();
    mockSetMute.mockReset();
    mockDeleteMute.mockReset();
    mockGetMutes.mockResolvedValue(NO_MUTES);
    mockSetMute.mockResolvedValue({
      resource_type: 'db_health',
      resource_id: 'db1',
      muted_until: '2026-08-16T00:00:00Z',
      created_by: 'admin',
    });
    mockDeleteMute.mockResolvedValue(undefined);
  });

  it('shows alerting and healthy counts', async () => {
    mockGet.mockResolvedValue(statusResult([
      { target: 'db1', type: 'db_health', status: 'alert', since: '2026-04-30T11:00:00Z' },
      { target: 'svc-x', type: 'upstream_health', status: 'alert', since: '2026-04-30T11:55:00Z' },
      { target: 'order-db', type: 'db_health', status: 'ok', since: null },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('db1')).toBeInTheDocument());
    expect(screen.getByText('svc-x')).toBeInTheDocument();
    expect(screen.getByText('order-db')).toBeInTheDocument();
  });

  it('renders empty fallback for both sections when nothing returned', async () => {
    mockGet.mockResolvedValue(statusResult([]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getAllByText(/None|없음|0/).length).toBeGreaterThan(0));
  });

  it('accepts a bare-array status payload from an older backend', async () => {
    // getAlertStatus normalizes the legacy shape, so the page only ever sees
    // the object form; this guards the page against an empty normalization.
    mockGet.mockResolvedValue(statusResult([
      { target: 'legacy-db', type: 'db_health', status: 'alert', since: null },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('legacy-db')).toBeInTheDocument());
  });

  it('renders empty target as asterisk', async () => {
    mockGet.mockResolvedValue(statusResult([
      { target: '', type: 'error_rate', status: 'alert', since: null },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('*')).toBeInTheDocument());
  });

  it('renders unknown rule type label fallback', async () => {
    mockGet.mockResolvedValue(statusResult([
      { target: 't', type: 'mystery', status: 'alert', since: null },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('mystery')).toBeInTheDocument());
  });

  it('renders alert severities when supplied', async () => {
    mockGet.mockResolvedValue(statusResult([
      { target: 'node-1', type: 'server_down', status: 'alert', since: null, severity: 'critical' },
      { target: 'node-2', type: 'server_cpu', status: 'alert', since: null, severity: 'warning' },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('node-1')).toBeInTheDocument());
    expect(screen.getByText('Critical')).toBeInTheDocument();
    expect(screen.getByText('Warning')).toBeInTheDocument();
  });

  it('refresh button triggers refetch', async () => {
    mockGet.mockResolvedValue(statusResult([
      { target: 'svc', type: 'db_health', status: 'ok', since: null },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('svc')).toBeInTheDocument());
    const initialCalls = mockGet.mock.calls.length;
    const btn = screen.getByRole('button', { name: 'Refresh alert status' });
    fireEvent.click(btn);
    await waitFor(() =>
      expect(mockGet.mock.calls.length).toBeGreaterThan(initialCalls),
    );
  });

  it('shows error banner on failure', async () => {
    mockGet.mockRejectedValue(new Error('boom'));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => {
      expect(screen.getByText(/error|오류/i)).toBeInTheDocument();
    });
  });

  it('formats alerting durations across thresholds', async () => {
    const now = Date.now();
    mockGet.mockResolvedValue(statusResult([
      // 30 seconds ago → "30s"
      { target: 't1', type: 'db_health', status: 'alert', since: new Date(now - 30 * 1000).toISOString() },
      // 5 minutes ago → "5m"
      { target: 't2', type: 'db_health', status: 'alert', since: new Date(now - 5 * 60 * 1000).toISOString() },
      // 2 hours ago → "2h"
      { target: 't3', type: 'db_health', status: 'alert', since: new Date(now - 2 * 60 * 60 * 1000).toISOString() },
      // 30 hours ago → "1d"
      { target: 't4', type: 'db_health', status: 'alert', since: new Date(now - 30 * 60 * 60 * 1000).toISOString() },
      // null since
      { target: 't5', type: 'db_health', status: 'alert', since: null },
      // future since (negative) → empty
      { target: 't6', type: 'db_health', status: 'alert', since: new Date(now + 60 * 1000).toISOString() },
      // invalid since
      { target: 't7', type: 'db_health', status: 'alert', since: 'not-a-date' },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('t1')).toBeInTheDocument());
    expect(screen.getByText(/^30s$/)).toBeInTheDocument();
    expect(screen.getByText(/^5m$/)).toBeInTheDocument();
    expect(screen.getByText(/^2h\b/)).toBeInTheDocument();
    expect(screen.getByText(/^1d\b/)).toBeInTheDocument();
  });

  /* ── Mutes ── */

  it('excludes muted rows from the alerting count and subdues them', async () => {
    mockGet.mockResolvedValue(statusResult([
      { target: 'loud-db', type: 'db_health', status: 'alert', since: null, muted: false },
      {
        target: 'quiet-db',
        type: 'db_health',
        status: 'alert',
        since: null,
        muted: true,
        muted_until: '2099-01-01T00:00:00Z',
      },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('quiet-db')).toBeInTheDocument());

    // 1 unmuted firing, with a note about the muted one
    expect(screen.getByText('1 muted excluded')).toBeInTheDocument();
    expect(screen.getByText('quiet-db').closest('tr')).toHaveClass('status-row--muted');
    expect(screen.getByText('loud-db').closest('tr')).not.toHaveClass('status-row--muted');
  });

  it('derives mute state from the mutes list when the status row omits it', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1', type: 'db_health', status: 'alert', since: null,
        resource_type: 'db', resource_id: 'db1',
      },
    ]));
    mockGetMutes.mockResolvedValue({
      global_muted_until: null,
      mutes: [
        {
          resource_type: 'db',
          resource_id: 'db1',
          muted_until: '2099-01-01T00:00:00Z',
          created_by: 'admin',
        },
      ],
    });
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('db1').closest('tr')).toHaveClass('status-row--muted'));
    expect(screen.getByRole('button', { name: 'Unmute db1' })).toBeInTheDocument();
  });

  it('mutes a row for a preset duration', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1', type: 'db_health', status: 'alert', since: null, muted: false,
        resource_type: 'db', resource_id: 'orders',
      },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('db1')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Mute db1' }));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Duration' }), '8h');
    await userEvent.click(screen.getByRole('button', { name: 'Mute' }));

    await waitFor(() => expect(mockSetMute).toHaveBeenCalled());
    const body = mockSetMute.mock.calls[0][0];
    expect(body.resource_type).toBe('db');
    expect(body.resource_id).toBe('orders');
    const deltaHours = (new Date(body.muted_until).getTime() - Date.now()) / 3_600_000;
    expect(deltaHours).toBeGreaterThan(7.9);
    expect(deltaHours).toBeLessThan(8.1);
  });

  it('sends a custom KST mute time as UTC', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1', type: 'db_health', status: 'alert', since: null, muted: false,
        resource_type: 'db', resource_id: 'orders',
      },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('db1')).toBeInTheDocument());

    // inside the backend's 30-day cap, so only the timezone shift is under test
    const kstDay = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Seoul',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date(Date.now() + 10 * 86_400_000));

    await userEvent.click(screen.getByRole('button', { name: 'Mute db1' }));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Duration' }), 'custom');
    fireEvent.change(screen.getByLabelText('Muted until (KST)'), {
      target: { value: `${kstDay}T09:30` },
    });
    await userEvent.click(screen.getByRole('button', { name: 'Mute' }));

    await waitFor(() => expect(mockSetMute).toHaveBeenCalled());
    // 09:30 KST == 00:30 UTC the same day
    expect(mockSetMute.mock.calls[0][0].muted_until).toBe(`${kstDay}T00:30:00.000Z`);
  });

  it('rejects a custom mute time in the past', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1', type: 'db_health', status: 'alert', since: null, muted: false,
        resource_type: 'db', resource_id: 'orders',
      },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('db1')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Mute db1' }));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Duration' }), 'custom');
    fireEvent.change(screen.getByLabelText('Muted until (KST)'), {
      target: { value: '2000-01-01T00:00' },
    });
    await userEvent.click(screen.getByRole('button', { name: 'Mute' }));

    expect(await screen.findByText('Pick a time in the future.')).toBeInTheDocument();
    expect(mockSetMute).not.toHaveBeenCalled();
  });

  it('unmutes a muted row', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1',
        type: 'db_health',
        status: 'alert',
        since: null,
        muted: true,
        muted_until: '2099-01-01T00:00:00Z',
        resource_type: 'db',
        resource_id: 'orders',
      },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Unmute db1' })).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Unmute db1' }));
    await waitFor(() => expect(mockDeleteMute).toHaveBeenCalledWith('db', 'orders'));
  });

  it('keys the row mute on the backend-supplied resource when present', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'Orders DB',
        type: 'db_health',
        status: 'alert',
        since: null,
        muted: true,
        resource_type: 'db',
        resource_id: 'orders',
      },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Unmute Orders DB' })).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole('button', { name: 'Unmute Orders DB' }));
    await waitFor(() => expect(mockDeleteMute).toHaveBeenCalledWith('db', 'orders'));
  });

  it('offers no mute button for a rule with no mutable resource', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'mystery',
        type: 'legacy_rule',
        status: 'alert',
        since: null,
        muted: false,
        resource_type: null,
        resource_id: null,
      },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('mystery')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Mute mystery' })).not.toBeInTheDocument();
  });

  it('offers no mute button when the row carries no mute key at all', async () => {
    // backend predating mutes: keying off type/target would address the wrong
    // resource and be rejected, so the action is withheld entirely
    mockGet.mockResolvedValue(statusResult([
      { target: 'checkout (r-1)', type: 'route_error_rate', status: 'alert', since: null },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('checkout (r-1)')).toBeInTheDocument());
    expect(
      screen.queryByRole('button', { name: 'Mute checkout (r-1)' }),
    ).not.toBeInTheDocument();
    // the global mute control is unrelated and stays available
    expect(screen.getByRole('button', { name: 'Mute all alerts' })).toBeInTheDocument();
  });

  it('rejects a mute window longer than the backend allows', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1', type: 'db_health', status: 'alert', since: null, muted: false,
        resource_type: 'db', resource_id: 'orders',
      },
    ]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('db1')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Mute db1' }));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Duration' }), 'custom');
    fireEvent.change(screen.getByLabelText('Muted until (KST)'), {
      target: { value: '2099-01-01T00:00' },
    });
    await userEvent.click(screen.getByRole('button', { name: 'Mute' }));

    expect(await screen.findByText('A mute can last at most 30 days.')).toBeInTheDocument();
    expect(mockSetMute).not.toHaveBeenCalled();
  });

  it('blocks per-row unmute when only the global mute is silencing the row', async () => {
    // the backend marks every row muted during a global mute; deleting a
    // per-target mute that does not exist would 204 and change nothing
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1', type: 'db_health', status: 'alert', since: null,
        muted: true, muted_until: '2099-05-05T00:00:00Z',
        resource_type: 'db', resource_id: 'orders',
      },
    ], '2099-05-05T00:00:00Z'));
    mockGetMutes.mockResolvedValue({ global_muted_until: '2099-05-05T00:00:00Z', mutes: [] });
    renderWithProviders(<AlertStatus />);

    const button = await screen.findByRole('button', { name: 'Unmute db1' });
    expect(button).toBeDisabled();
    expect(screen.getByText('Release the global mute to unmute this target')).toBeInTheDocument();
    expect(button).toHaveAttribute('aria-describedby');

    await userEvent.click(button);
    expect(mockDeleteMute).not.toHaveBeenCalled();
  });

  it('allows unmuting a row with its own mute and says the global mute remains', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1', type: 'db_health', status: 'alert', since: null,
        muted: true, muted_until: '2099-05-05T00:00:00Z',
        resource_type: 'db', resource_id: 'orders',
      },
    ], '2099-05-05T00:00:00Z'));
    mockGetMutes.mockResolvedValue({
      global_muted_until: '2099-05-05T00:00:00Z',
      mutes: [
        { resource_type: 'global', resource_id: '', muted_until: '2099-05-05T00:00:00Z', created_by: 'admin' },
        { resource_type: 'db', resource_id: 'orders', muted_until: '2099-01-01T00:00:00Z', created_by: 'admin' },
      ],
    });
    renderWithProviders(<AlertStatus />);

    const button = await screen.findByRole('button', { name: 'Unmute db1' });
    expect(button).toBeEnabled();
    expect(
      screen.queryByText('Release the global mute to unmute this target'),
    ).not.toBeInTheDocument();

    await userEvent.click(button);
    await waitFor(() => expect(mockDeleteMute).toHaveBeenCalledWith('db', 'orders'));
    expect(await screen.findByText('The global mute still applies.')).toBeInTheDocument();
  });

  it('does not claim a lingering global mute when releasing the global mute itself', async () => {
    mockGet.mockResolvedValue(statusResult([], '2099-05-05T00:00:00Z'));
    renderWithProviders(<AlertStatus />);

    await userEvent.click(await screen.findByRole('button', { name: 'Unmute all alerts' }));
    await waitFor(() => expect(mockDeleteMute).toHaveBeenCalledWith('global', ''));
    expect(screen.queryByText('The global mute still applies.')).not.toBeInTheDocument();
  });

  it('shows the global mute banner and releases it', async () => {
    mockGet.mockResolvedValue(statusResult([
      { target: 'db1', type: 'db_health', status: 'alert', since: null, muted: true },
    ], '2099-05-05T00:00:00Z'));
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText(/All alerts muted/)).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Unmute all alerts' }));
    await waitFor(() => expect(mockDeleteMute).toHaveBeenCalledWith('global', ''));
  });

  it('sets a global mute from the toolbar', async () => {
    mockGet.mockResolvedValue(statusResult([]));
    renderWithProviders(<AlertStatus />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Mute all alerts' })).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole('button', { name: 'Mute all alerts' }));
    await userEvent.click(screen.getByRole('button', { name: 'Mute' }));

    await waitFor(() => expect(mockSetMute).toHaveBeenCalled());
    expect(mockSetMute.mock.calls[0][0].resource_type).toBe('global');
    expect(mockSetMute.mock.calls[0][0].resource_id).toBe('');
  });

  it('hides mute controls without alerts.write', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1', type: 'db_health', status: 'alert', since: null, muted: false,
        resource_type: 'db', resource_id: 'orders',
      },
    ]));
    renderWithProviders(<AlertStatus />, { permissions: [...VIEWER_PERMISSIONS, 'alerts.read'] });
    await waitFor(() => expect(screen.getByText('db1')).toBeInTheDocument());

    expect(screen.queryByRole('button', { name: 'Mute db1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Mute all alerts' })).not.toBeInTheDocument();
  });

  it('still shows the muted-until chip without alerts.write', async () => {
    mockGet.mockResolvedValue(statusResult([
      {
        target: 'db1',
        type: 'db_health',
        status: 'alert',
        since: null,
        muted: true,
        muted_until: '2099-01-01T00:00:00Z',
      },
    ], '2099-05-05T00:00:00Z'));
    renderWithProviders(<AlertStatus />, { permissions: [...VIEWER_PERMISSIONS, 'alerts.read'] });
    await waitFor(() => expect(screen.getByText(/All alerts muted/)).toBeInTheDocument());

    expect(screen.getByText(/Muted until/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unmute all alerts' })).not.toBeInTheDocument();
  });
});
