vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAlertStatus: vi.fn(),
}));

import { screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAlertStatus } from '../api/client';
import AlertStatus from '../pages/AlertStatus';
import { renderWithProviders } from './helpers';

const mockGet = vi.mocked(getAlertStatus);

describe('AlertStatus page', () => {
  beforeEach(() => {
    mockGet.mockReset();
  });

  it('shows alerting and healthy counts', async () => {
    mockGet.mockResolvedValue([
      { target: 'db1', type: 'db_health', status: 'alert', since: '2026-04-30T11:00:00Z' },
      { target: 'svc-x', type: 'upstream_health', status: 'alert', since: '2026-04-30T11:55:00Z' },
      { target: 'order-db', type: 'db_health', status: 'ok', since: null },
    ]);
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('db1')).toBeInTheDocument());
    expect(screen.getByText('svc-x')).toBeInTheDocument();
    expect(screen.getByText('order-db')).toBeInTheDocument();
  });

  it('renders empty fallback for both sections when nothing returned', async () => {
    mockGet.mockResolvedValue([]);
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getAllByText(/None|없음|0/).length).toBeGreaterThan(0));
  });

  it('renders empty target as asterisk', async () => {
    mockGet.mockResolvedValue([
      { target: '', type: 'error_rate', status: 'alert', since: null },
    ]);
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('*')).toBeInTheDocument());
  });

  it('renders unknown rule type label fallback', async () => {
    mockGet.mockResolvedValue([
      { target: 't', type: 'mystery', status: 'alert', since: null },
    ]);
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('mystery')).toBeInTheDocument());
  });

  it('refresh button triggers refetch', async () => {
    mockGet.mockResolvedValue([
      { target: 'svc', type: 'db_health', status: 'ok', since: null },
    ]);
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('svc')).toBeInTheDocument());
    const initialCalls = mockGet.mock.calls.length;
    const btn = screen.getByRole('button', { name: /Refresh|Loading|새로고침|로딩/i });
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
    mockGet.mockResolvedValue([
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
    ]);
    renderWithProviders(<AlertStatus />);
    await waitFor(() => expect(screen.getByText('t1')).toBeInTheDocument());
    expect(screen.getByText(/^30s$/)).toBeInTheDocument();
    expect(screen.getByText(/^5m$/)).toBeInTheDocument();
    expect(screen.getByText(/^2h\b/)).toBeInTheDocument();
    expect(screen.getByText(/^1d\b/)).toBeInTheDocument();
  });
});
