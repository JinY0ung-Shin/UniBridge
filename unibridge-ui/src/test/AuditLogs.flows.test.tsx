vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAuditLogs: vi.fn(),
  getAdminDatabases: vi.fn(),
}));

import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAuditLogs, getAdminDatabases } from '../api/client';
import AuditLogs from '../pages/AuditLogs';
import { renderWithProviders, makeAuditLog } from './helpers';

const mockedGetAuditLogs = vi.mocked(getAuditLogs);
const mockedGetAdminDatabases = vi.mocked(getAdminDatabases);

describe('AuditLogs flows', () => {
  beforeEach(() => {
    mockedGetAdminDatabases.mockResolvedValue([
      { alias: 'db-1', db_type: 'postgres' as const, host: 'h', port: 5432, database: 'd', username: 'u', pool_size: 5, max_overflow: 3, query_timeout: 30 },
    ]);
  });

  it('shows error banner when logs query fails', async () => {
    mockedGetAuditLogs.mockRejectedValue(new Error('boom'));
    renderWithProviders(<AuditLogs />);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load audit logs/)).toBeInTheDocument();
    });
  });

  it('shows params and error sections in expanded detail row', async () => {
    const log = makeAuditLog({
      id: 99,
      sql: 'SELECT 1',
      params: '{"x": 1, "y": 2}',
      status: 'error' as const,
      error_message: 'column missing',
    });
    mockedGetAuditLogs.mockResolvedValue([log]);

    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    const row = screen.getByText('SELECT 1').closest('tr')!;
    await userEvent.click(row);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Parameters' })).toBeInTheDocument();
    });
    expect(screen.getByRole('heading', { name: 'Error' })).toBeInTheDocument();
    expect(screen.getByText('column missing')).toBeInTheDocument();
  });

  it('shows raw params text when JSON is malformed', async () => {
    const log = makeAuditLog({
      sql: 'SELECT 2',
      params: 'not-json',
    });
    mockedGetAuditLogs.mockResolvedValue([log]);

    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByText('SELECT 2').closest('tr')!);
    await waitFor(() => {
      expect(screen.getByText('not-json')).toBeInTheDocument();
    });
  });

  it('clicking Search refetches with applied filters', async () => {
    mockedGetAuditLogs.mockResolvedValue([]);
    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('No audit logs found')).toBeInTheDocument();
    });

    await userEvent.type(screen.getByRole('textbox', { name: 'User filter' }), 'alice');
    const initialCallCount = mockedGetAuditLogs.mock.calls.length;
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => {
      const after = mockedGetAuditLogs.mock.calls.length;
      expect(after).toBeGreaterThan(initialCallCount);
      const lastCall = mockedGetAuditLogs.mock.calls[after - 1];
      expect(lastCall[0]).toEqual(expect.objectContaining({ user: 'alice' }));
    });
  });

  it('Next button advances page and Previous returns', async () => {
    const page1 = Array.from({ length: 20 }, (_, i) =>
      makeAuditLog({ id: i + 1, user: `u${i + 1}` }),
    );
    const page2 = [makeAuditLog({ id: 100, user: 'page2user' })];

    mockedGetAuditLogs.mockImplementation(async (params: { offset?: number } | undefined) => {
      return (params?.offset ?? 0) === 0 ? page1 : page2;
    });

    renderWithProviders(<AuditLogs />);
    await waitFor(() => expect(screen.getByText('u1')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(screen.getByText('page2user')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Previous page' }));
    await waitFor(() => expect(screen.getByText('u1')).toBeInTheDocument());
  });

  it('keyboard activation expands and collapses a row', async () => {
    const log = makeAuditLog({ sql: 'SELECT * FROM t' });
    mockedGetAuditLogs.mockResolvedValue([log]);
    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    const row = screen.getByRole('button', { name: 'Toggle details for audit log 1' });
    fireEvent.keyDown(row, { key: 'Enter' });
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Full SQL' })).toBeInTheDocument();
    });
    expect(row).toHaveAttribute('aria-expanded', 'true');

    fireEvent.keyDown(row, { key: ' ' });
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Full SQL' })).not.toBeInTheDocument();
    });
    expect(row).toHaveAttribute('aria-expanded', 'false');
  });
});
