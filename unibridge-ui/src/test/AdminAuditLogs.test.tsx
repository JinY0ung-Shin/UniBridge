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

    expect(screen.getByRole('textbox', { name: 'Actor filter' })).toHaveAttribute('id', 'admin-audit-actor-filter');
    expect(screen.getByRole('combobox', { name: 'Resource type filter' })).toHaveAttribute(
      'id',
      'admin-audit-resource-type-filter',
    );
    expect(screen.getByRole('combobox', { name: 'Action filter' })).toHaveAttribute('id', 'admin-audit-action-filter');
    expect(screen.getByLabelText('From date')).toHaveAttribute('id', 'admin-audit-from-date');
    expect(screen.getByLabelText('To date')).toHaveAttribute('id', 'admin-audit-to-date');
    expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reset filters' })).toBeDisabled();
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

    const row = screen.getByRole('button', { name: 'Toggle details for admin audit log 1' });
    expect(row).toHaveAttribute('aria-expanded', 'false');
    expect(row).toHaveAttribute('aria-controls', 'admin-audit-log-detail-1');
    await userEvent.click(row);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Before' })).toBeInTheDocument();
    });
    expect(row).toHaveAttribute('aria-expanded', 'true');
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

    await userEvent.type(screen.getByRole('textbox', { name: 'Actor filter' }), 'alice');
    const before = mockedGetAdminAuditLogs.mock.calls.length;
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => {
      const after = mockedGetAdminAuditLogs.mock.calls.length;
      expect(after).toBeGreaterThan(before);
      const lastCall = mockedGetAdminAuditLogs.mock.calls[after - 1];
      expect(lastCall[0]).toEqual(expect.objectContaining({ actor: 'alice' }));
    });
  });

  it('resets draft and applied filters', async () => {
    mockedGetAdminAuditLogs.mockResolvedValue([]);
    renderWithProviders(<AdminAuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('No audit logs found')).toBeInTheDocument();
    });

    const actorInput = screen.getByRole('textbox', { name: 'Actor filter' });
    await userEvent.type(actorInput, 'alice');
    await userEvent.selectOptions(screen.getAllByRole('combobox')[0], 'route');
    await userEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => {
      expect(mockedGetAdminAuditLogs).toHaveBeenCalledWith(
        expect.objectContaining({ actor: 'alice', resource_type: 'route', offset: 0 }),
      );
    });

    await userEvent.click(screen.getByRole('button', { name: 'Reset filters' }));

    expect(actorInput).toHaveValue('');
    expect(screen.getAllByRole('combobox')[0]).toHaveValue('');
    await waitFor(() => {
      expect(mockedGetAdminAuditLogs).toHaveBeenLastCalledWith(
        expect.objectContaining({ actor: undefined, resource_type: undefined, offset: 0 }),
      );
    });
  });

  it('announces pagination controls with page status', async () => {
    const logs = Array.from({ length: 20 }, (_, i) =>
      makeAdminAuditLog({ id: i + 1, actor: `actor${i + 1}` }),
    );
    mockedGetAdminAuditLogs.mockResolvedValue(logs);

    renderWithProviders(<AdminAuditLogs />);

    await waitFor(() => {
      expect(screen.getByText('actor1')).toBeInTheDocument();
    });

    const prevButton = screen.getByRole('button', { name: 'Previous page' });
    const nextButton = screen.getByRole('button', { name: 'Next page' });

    expect(prevButton).toBeDisabled();
    expect(nextButton).toBeEnabled();
    expect(screen.getByRole('status')).toHaveTextContent('Page 1');
  });
});
