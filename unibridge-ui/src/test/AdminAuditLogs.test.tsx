vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAdminAuditLogs: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAdminAuditLogs } from '../api/client';
import AdminAuditLogs from '../pages/AdminAuditLogs';
import { renderWithProviders, makeAdminAuditLog } from './helpers';

const mockedGetAdminAuditLogs = vi.mocked(getAdminAuditLogs);

describe('AdminAuditLogs', () => {
  beforeEach(() => {
    mockedGetAdminAuditLogs.mockResolvedValue([]);
  });

  it('renders rows from getAdminAuditLogs', async () => {
    const log = makeAdminAuditLog({ summary: 'Updated route route-1' });
    mockedGetAdminAuditLogs.mockResolvedValue([log]);

    renderWithProviders(<AdminAuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    expect(screen.getByText('Updated route route-1')).toBeInTheDocument();
    expect(screen.getByText('route-1')).toBeInTheDocument();
  });

  it('renders empty state when no logs', async () => {
    mockedGetAdminAuditLogs.mockResolvedValue([]);

    renderWithProviders(<AdminAuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('No audit logs found')).toBeInTheDocument();
    });
  });

  it('shows filter controls', async () => {
    renderWithProviders(<AdminAuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('No audit logs found')).toBeInTheDocument();
    });

    expect(screen.getByPlaceholderText('Actor')).toBeInTheDocument();
    // resource_type + action selects
    expect(screen.getAllByRole('combobox')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument();
  });

  it('expands a row to show before/after JSON', async () => {
    const log = makeAdminAuditLog({
      before: '{"name":"old"}',
      after: '{"name":"new"}',
      error_message: 'something failed',
      status: 'error',
    });
    mockedGetAdminAuditLogs.mockResolvedValue([log]);

    renderWithProviders(<AdminAuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByText('admin').closest('tr')!);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Before' })).toBeInTheDocument();
    });
    expect(screen.getByRole('heading', { name: 'After' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Error Message' })).toBeInTheDocument();
    expect(screen.getByText('something failed')).toBeInTheDocument();

    // before JSON is pretty-printed
    const preTags = document.querySelectorAll('pre.detail-sql');
    expect(preTags.length).toBeGreaterThanOrEqual(2);
    expect(preTags[0].textContent).toContain('"name": "old"');
  });

  it('clicking Search refetches with applied filters', async () => {
    mockedGetAdminAuditLogs.mockResolvedValue([]);
    renderWithProviders(<AdminAuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('No audit logs found')).toBeInTheDocument();
    });

    await userEvent.type(screen.getByPlaceholderText('Actor'), 'alice');
    const before = mockedGetAdminAuditLogs.mock.calls.length;
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => {
      const after = mockedGetAdminAuditLogs.mock.calls.length;
      expect(after).toBeGreaterThan(before);
      const lastCall = mockedGetAdminAuditLogs.mock.calls[after - 1];
      expect(lastCall[0]).toEqual(expect.objectContaining({ actor: 'alice' }));
    });
  });
});
