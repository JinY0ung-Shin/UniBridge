vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAlertHistory: vi.fn(),
}));

import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getAlertHistory } from '../api/client';
import AlertHistory from '../pages/AlertHistory';
import { renderWithProviders } from './helpers';

const mockGet = vi.mocked(getAlertHistory);

function makeEntry(overrides = {}) {
  return {
    id: 1,
    rule_id: 1,
    channel_id: 1,
    alert_type: 'triggered' as const,
    target: 'db1',
    message: 'DB down',
    recipients: ['ops@example.com'],
    sent_at: '2026-04-30T12:00:00Z',
    success: true,
    error_detail: null,
    ...overrides,
  };
}

describe('AlertHistory page', () => {
  beforeEach(() => {
    mockGet.mockReset();
  });

  it('shows empty state when no entries', async () => {
    mockGet.mockResolvedValue([]);
    renderWithProviders(<AlertHistory />);
    await waitFor(() => {
      expect(screen.getByText(/No.*history|noHistory/i)).toBeInTheDocument();
    });
  });

  it('renders entries in a table with correct status badges', async () => {
    mockGet.mockResolvedValue([
      makeEntry({ id: 1, alert_type: 'triggered', success: true, message: 'down' }),
      makeEntry({ id: 2, alert_type: 'resolved', success: false, message: 'recover-failed' }),
      makeEntry({ id: 3, alert_type: 'triggered', success: null, message: 'pending' }),
    ]);
    renderWithProviders(<AlertHistory />);
    await waitFor(() => {
      expect(screen.getByText('down')).toBeInTheDocument();
    });
    expect(screen.getByText('recover-failed')).toBeInTheDocument();
    expect(screen.getByText('pending')).toBeInTheDocument();
  });

  it('applies filters and resets to page 0', async () => {
    mockGet.mockResolvedValue([makeEntry()]);
    renderWithProviders(<AlertHistory />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const select = screen.getByRole('combobox', { name: /Alert Type|알림 유형/i });
    expect(select).toHaveAttribute('id', 'alert-history-type-filter');
    fireEvent.change(select, { target: { value: 'triggered' } });

    const targetInput = screen.getByRole('textbox', { name: /Target|대상/i });
    expect(targetInput).toHaveAttribute('id', 'alert-history-target-filter');
    await userEvent.type(targetInput, 'db1');

    const searchBtn = screen.getByRole('button', { name: /Search|검색/i });
    fireEvent.click(searchBtn);

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith(
        expect.objectContaining({
          alert_type: 'triggered',
          target: 'db1',
          limit: 50,
          offset: 0,
        }),
      );
    });
  });

  it('resets draft and applied filters', async () => {
    mockGet.mockResolvedValue([makeEntry()]);
    renderWithProviders(<AlertHistory />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const select = screen.getByRole('combobox', { name: /Alert Type|알림 유형/i });
    fireEvent.change(select, { target: { value: 'triggered' } });
    const targetInput = screen.getByRole('textbox', { name: /Target|대상/i });
    await userEvent.type(targetInput, 'db1');
    fireEvent.click(screen.getByRole('button', { name: /Search|검색/i }));

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith(
        expect.objectContaining({ alert_type: 'triggered', target: 'db1', offset: 0 }),
      );
    });

    fireEvent.click(screen.getByRole('button', { name: /Reset filters|필터 초기화/i }));

    expect(select).toHaveValue('');
    expect(targetInput).toHaveValue('');
    await waitFor(() => {
      expect(mockGet).toHaveBeenLastCalledWith(
        expect.objectContaining({ alert_type: undefined, target: undefined, offset: 0 }),
      );
    });
  });

  it('Enter key in target input applies filters', async () => {
    mockGet.mockResolvedValue([makeEntry()]);
    renderWithProviders(<AlertHistory />);
    const targetInput = screen.getByRole('textbox', { name: /Target|대상/i });
    fireEvent.keyDown(targetInput, { key: 'Enter' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
  });

  it('paginates with Next/Previous buttons', async () => {
    // Create exactly PAGE_SIZE entries so hasMore is true
    const fullPage = Array.from({ length: 50 }, (_, i) =>
      makeEntry({ id: i + 1, message: `entry-${i}` }),
    );
    mockGet.mockResolvedValue(fullPage);
    renderWithProviders(<AlertHistory />);
    await waitFor(() => expect(screen.getByText('entry-0')).toBeInTheDocument());

    expect(screen.getByRole('status')).toHaveTextContent(/Page 1|페이지 1/i);

    const nextBtn = screen.getByRole('button', { name: /Next page|다음 페이지/i });
    fireEvent.click(nextBtn);
    await waitFor(() => {
      expect(mockGet).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 50 }),
      );
    });
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/Page 2|페이지 2/i);
    });
    // After page change, await fresh data render
    await waitFor(() => {
      const prev = screen.queryByRole('button', { name: /Previous page|이전 페이지/i });
      expect(prev).not.toBeNull();
    });

    const prevBtn = screen.getByRole('button', { name: /Previous page|이전 페이지/i });
    fireEvent.click(prevBtn);
    await waitFor(() => {
      expect(mockGet).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 0 }),
      );
    });
  });

  it('renders the rule label, with an em dash for historical rows', async () => {
    mockGet.mockResolvedValue([
      makeEntry({ id: 1, rule_type: 'server_gpu_util', message: 'gpu hot' }),
      makeEntry({ id: 2, rule_type: 'external_service_down', message: 'svc gone' }),
      makeEntry({ id: 3, rule_type: null, message: 'legacy row' }),
    ]);
    renderWithProviders(<AlertHistory />);
    await waitFor(() => expect(screen.getByText('gpu hot')).toBeInTheDocument());

    // scoped to the table: the same labels also appear as filter options
    const table = within(screen.getByRole('table'));
    expect(table.getByText('Server GPU util')).toBeInTheDocument();
    expect(table.getByText('External service down')).toBeInTheDocument();
    expect(table.getByText('—')).toBeInTheDocument();
  });

  it('sends the selected rule as a rule_type query param', async () => {
    mockGet.mockResolvedValue([makeEntry()]);
    renderWithProviders(<AlertHistory />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const ruleSelect = screen.getByRole('combobox', { name: /Rule|규칙/i });
    expect(ruleSelect).toHaveAttribute('id', 'alert-history-rule-filter');
    // every rule identifier is offered, plus the "all" option
    expect(ruleSelect.querySelectorAll('option')).toHaveLength(15);

    fireEvent.change(ruleSelect, { target: { value: 'server_disk_forecast' } });
    fireEvent.click(screen.getByRole('button', { name: /Search|검색/i }));

    await waitFor(() => {
      expect(mockGet).toHaveBeenLastCalledWith(
        expect.objectContaining({ rule_type: 'server_disk_forecast', offset: 0 }),
      );
    });
  });

  it('shows error banner when query fails', async () => {
    mockGet.mockRejectedValue(new Error('boom'));
    renderWithProviders(<AlertHistory />);
    await waitFor(() => {
      expect(screen.getByText(/loadFailed|Failed/i)).toBeInTheDocument();
    });
  });
});
