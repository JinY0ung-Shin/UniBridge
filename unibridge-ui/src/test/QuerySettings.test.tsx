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
  default_row_limit: 10000,
  query_route_timeout: 310,
  gateway_route_timeout: 45,
  blocked_sql_keywords: ['DROP', 'TRUNCATE'],
};

describe('QuerySettingsPage', () => {
  beforeEach(() => {
    mockedGetQuerySettings.mockReset();
    mockedUpdateQuerySettings.mockReset();
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
      expect(screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' })).toHaveValue(60);
    });

    expect(screen.getByRole('spinbutton', { name: 'Max concurrent queries (per user)' })).toHaveValue(5);
    expect(screen.getByRole('textbox', { name: 'Additional blocked keywords' })).toHaveValue('DROP, TRUNCATE');
    expect(screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' })).toHaveAttribute(
      'aria-describedby',
      'query-rate-limit-hint',
    );
    expect(document.getElementById('query-rate-limit-hint')).toHaveTextContent(
      /Maximum number of query requests/,
    );
    expect(screen.getByRole('textbox', { name: 'Additional blocked keywords' })).toHaveAttribute(
      'aria-describedby',
      'blocked-sql-keywords-hint',
    );
    expect(document.getElementById('blocked-sql-keywords-hint')).toHaveTextContent(
      /Comma-separated list of SQL keywords/,
    );
    expect(screen.getByText('No settings changes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('renders error state', async () => {
    mockedGetQuerySettings.mockRejectedValue(new Error('Network error'));
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load settings.')).toBeInTheDocument();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load settings.');
  });

  it('calls updateQuerySettings on save', async () => {
    mockedUpdateQuerySettings.mockResolvedValue({
      ...defaultSettings,
      rate_limit_per_minute: 120,
    });
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' })).toBeInTheDocument();
    });

    const rateLimitInput = screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' });
    await userEvent.clear(rateLimitInput);
    await userEvent.type(rateLimitInput, '120');

    expect(screen.getByText('Unsaved settings changes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mockedUpdateQuerySettings).toHaveBeenCalled();
      expect(mockedUpdateQuerySettings.mock.calls[0][0]).toMatchObject({
        rate_limit_per_minute: 120,
      });
    });
  });

  it('marks the save button as busy while settings are saving', async () => {
    let resolveSave: (value: typeof defaultSettings) => void = () => {};
    mockedUpdateQuerySettings.mockReturnValueOnce(new Promise<typeof defaultSettings>((resolve) => {
      resolveSave = resolve;
    }));
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' })).toBeInTheDocument();
    });

    const rateLimitInput = screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' });
    await userEvent.clear(rateLimitInput);
    await userEvent.type(rateLimitInput, '120');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(screen.getByRole('button', { name: 'Saving...' })).toHaveAttribute('aria-busy', 'true');

    resolveSave({
      ...defaultSettings,
      rate_limit_per_minute: 120,
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('aria-busy', 'false');
    });
  });

  it('discards unsaved changes', async () => {
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' })).toBeInTheDocument();
    });

    const rateLimitInput = screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' });
    await userEvent.clear(rateLimitInput);
    await userEvent.type(rateLimitInput, '120');

    await userEvent.click(screen.getByRole('button', { name: 'Discard changes' }));

    expect(rateLimitInput).toHaveValue(60);
    expect(screen.getByText('No settings changes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(mockedUpdateQuerySettings).not.toHaveBeenCalled();
  });

  it('shows success message after save', async () => {
    mockedUpdateQuerySettings.mockResolvedValue({
      ...defaultSettings,
      rate_limit_per_minute: 120,
    });
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' })).toBeInTheDocument();
    });

    const rateLimitInput = screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' });
    await userEvent.clear(rateLimitInput);
    await userEvent.type(rateLimitInput, '120');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(screen.getByText('Settings saved.')).toBeInTheDocument();
    });
    expect(screen.getByRole('status')).toHaveTextContent('Settings saved.');
  });

  it('announces save failure as an alert', async () => {
    mockedUpdateQuerySettings.mockRejectedValue(new Error('boom'));
    renderWithProviders(<QuerySettingsPage />);

    await waitFor(() => {
      expect(screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' })).toBeInTheDocument();
    });

    const rateLimitInput = screen.getByRole('spinbutton', { name: 'Requests per minute (per user)' });
    await userEvent.clear(rateLimitInput);
    await userEvent.type(rateLimitInput, '120');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Failed to save settings.');
    });
  });
});
