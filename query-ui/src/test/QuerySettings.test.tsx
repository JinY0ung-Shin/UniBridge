vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getQuerySettings: vi.fn(),
  updateQuerySettings: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getQuerySettings, updateQuerySettings } from '../api/client';
import QuerySettingsPage from '../pages/QuerySettings';
import { renderWithProviders } from './helpers';

const mockedGetQuerySettings = vi.mocked(getQuerySettings);
const mockedUpdateQuerySettings = vi.mocked(updateQuerySettings);

const defaultSettings = {
  rate_limit_per_minute: 60,
  max_concurrent_queries: 5,
  blocked_sql_keywords: ['DROP', 'TRUNCATE'],
};

describe('QuerySettingsPage', () => {
  beforeEach(() => {
    mockedGetQuerySettings.mockResolvedValue(defaultSettings);
    mockedUpdateQuerySettings.mockResolvedValue(defaultSettings);
  });

  it('renders loading state', () => {
    mockedGetQuerySettings.mockReturnValue(new Promise(() => {}));
    renderWithProviders(<QuerySettingsPage />);
    expect(screen.getByText('Loading...')).toBeInTheDocument();
  });

  it('renders settings form with data', async () => {
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByDisplayValue('60')).toBeInTheDocument();
    });

    expect(screen.getByDisplayValue('5')).toBeInTheDocument();
    expect(screen.getByDisplayValue('DROP, TRUNCATE')).toBeInTheDocument();
  });

  it('renders error state', async () => {
    mockedGetQuerySettings.mockRejectedValue(new Error('Network error'));
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load settings.')).toBeInTheDocument();
    });
  });

  it('calls updateQuerySettings on save', async () => {
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByDisplayValue('60')).toBeInTheDocument();
    });

    const rateLimitInput = screen.getByDisplayValue('60');
    await userEvent.clear(rateLimitInput);
    await userEvent.type(rateLimitInput, '120');

    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockedUpdateQuerySettings).toHaveBeenCalled();
      expect(mockedUpdateQuerySettings.mock.calls[0][0]).toMatchObject({
        rate_limit_per_minute: 120,
      });
    });
  });

  it('shows success message after save', async () => {
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByDisplayValue('60')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(screen.getByText('Settings saved.')).toBeInTheDocument();
    });
  });
});
