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

    // Database select dropdown
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    // User text input
    expect(screen.getByPlaceholderText('User')).toBeInTheDocument();
    // Search button
    expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument();
  });

  it('expands row to show full SQL on click', async () => {
    const log = makeAuditLog({ sql: 'SELECT * FROM users' });
    mockedGetAuditLogs.mockResolvedValue([log]);

    renderWithProviders(<AuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('admin')).toBeInTheDocument();
    });

    // Click the row to expand it
    const row = screen.getByText('SELECT * FROM users').closest('tr')!;
    await userEvent.click(row);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Full SQL' })).toBeInTheDocument();
    });

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

    const prevButton = screen.getByRole('button', { name: 'Previous' });
    const nextButton = screen.getByRole('button', { name: 'Next' });

    expect(prevButton).toBeInTheDocument();
    expect(prevButton).toBeDisabled();
    expect(nextButton).toBeInTheDocument();
  });
});
