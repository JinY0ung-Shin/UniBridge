vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAuditLogs: vi.fn(),
  getAdminDatabases: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAuditLogs, getAdminDatabases } from '../api/client';
import AuditLogs from '../pages/AuditLogs';
import { renderWithProviders, makeAuditLog } from './helpers';

const mockedGetAuditLogs = vi.mocked(getAuditLogs);
const mockedGetAdminDatabases = vi.mocked(getAdminDatabases);

describe('AuditLogs', () => {
  beforeEach(() => {
    mockedGetAdminDatabases.mockResolvedValue([]);
    mockedGetAuditLogs.mockResolvedValue([]);
  });

  it('renders audit logs table', async () => {
    const log = makeAuditLog();
    mockedGetAuditLogs.mockResolvedValue([log]);

    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    expect(screen.getByText('test-db')).toBeInTheDocument();
  });

  it('renders empty state when no logs', async () => {
    mockedGetAuditLogs.mockResolvedValue([]);

    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('No audit logs found')).toBeInTheDocument();
    });
  });

  it('shows filter controls', async () => {
    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('No audit logs found')).toBeInTheDocument();
    });

    expect(screen.getByRole('combobox', { name: 'Database filter' })).toHaveAttribute('id', 'audit-database-filter');
    expect(screen.getByRole('textbox', { name: 'User filter' })).toHaveAttribute('id', 'audit-user-filter');
    expect(screen.getByLabelText('From date')).toHaveAttribute('id', 'audit-from-date');
    expect(screen.getByLabelText('To date')).toHaveAttribute('id', 'audit-to-date');
    // Search button
    expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reset filters' })).toBeDisabled();
  });

  it('resets draft and applied filters', async () => {
    mockedGetAuditLogs.mockResolvedValue([]);
    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('No audit logs found')).toBeInTheDocument();
    });

    const userInput = screen.getByRole('textbox', { name: 'User filter' });
    await userEvent.type(userInput, 'alice');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => {
      expect(mockedGetAuditLogs).toHaveBeenCalledWith(
        expect.objectContaining({ user: 'alice', offset: 0 }),
      );
    });

    await userEvent.click(screen.getByRole('button', { name: 'Reset filters' }));

    expect(userInput).toHaveValue('');
    await waitFor(() => {
      expect(mockedGetAuditLogs).toHaveBeenLastCalledWith(
        expect.objectContaining({ user: undefined, offset: 0 }),
      );
    });
  });

  it('expands row to show full SQL on click', async () => {
    const log = makeAuditLog({ sql: 'SELECT * FROM users' });
    mockedGetAuditLogs.mockResolvedValue([log]);

    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    const row = screen.getByRole('button', { name: 'Toggle details for audit log 1' });
    expect(row).toHaveAttribute('aria-expanded', 'false');
    expect(row).toHaveAttribute('aria-controls', 'audit-log-detail-1');
    await userEvent.click(row);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Full SQL' })).toBeInTheDocument();
    });
    expect(row).toHaveAttribute('aria-expanded', 'true');

    // Full SQL is shown in the detail pre block
    const preTags = document.querySelectorAll('pre.detail-sql');
    expect(preTags.length).toBeGreaterThan(0);
    expect(preTags[0]).toHaveTextContent('SELECT * FROM users');
  });

  it('pagination buttons exist', async () => {
    const logs = Array.from({ length: 20 }, (_, i) =>
      makeAuditLog({ id: i + 1, user: `user${i + 1}` }),
    );
    mockedGetAuditLogs.mockResolvedValue(logs);

    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('user1')).toBeInTheDocument();
    });

    const prevButton = screen.getByRole('button', { name: 'Previous page' });
    const nextButton = screen.getByRole('button', { name: 'Next page' });

    expect(prevButton).toBeInTheDocument();
    expect(prevButton).toBeDisabled();
    expect(nextButton).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Page 1');
  });
});
