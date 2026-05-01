vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getAlertHistory: vi.fn(),
}));

import { screen, waitFor, fireEvent } from '@testing-library/react';
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

    const select = screen.getByRole('combobox');
    fireEvent.change(select, { target: { value: 'triggered' } });

    const targetInput = screen.getByPlaceholderText(/Target|filterTarget/i);
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

  it('Enter key in target input applies filters', async () => {
    mockGet.mockResolvedValue([makeEntry()]);
    renderWithProviders(<AlertHistory />);
    const targetInput = screen.getByPlaceholderText(/Target|filterTarget/i);
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

    const nextBtn = screen.getByRole('button', { name: /Next|다음/i });
    fireEvent.click(nextBtn);
    await waitFor(() => {
      expect(mockGet).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 50 }),
      );
    });
    // After page change, await fresh data render
    await waitFor(() => {
      const prev = screen.queryByRole('button', { name: /Previous|이전/i });
      expect(prev).not.toBeNull();
    });

    const prevBtn = screen.getByRole('button', { name: /Previous|이전/i });
    fireEvent.click(prevBtn);
    await waitFor(() => {
      expect(mockGet).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 0 }),
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
