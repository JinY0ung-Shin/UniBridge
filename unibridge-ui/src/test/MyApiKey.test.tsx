vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getMyApiKey: vi.fn(),
  createMyApiKey: vi.fn(),
  regenerateMyApiKey: vi.fn(),
  renewMyApiKey: vi.fn(),
  deleteMyApiKey: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getMyApiKey,
  createMyApiKey,
  regenerateMyApiKey,
  renewMyApiKey,
} from '../api/client';
import MyApiKey from '../pages/MyApiKey';
import { renderWithProviders, makeApiKey } from './helpers';

const mockedGetMyApiKey = vi.mocked(getMyApiKey);
const mockedCreateMyApiKey = vi.mocked(createMyApiKey);
const mockedRegenerateMyApiKey = vi.mocked(regenerateMyApiKey);
const mockedRenewMyApiKey = vi.mocked(renewMyApiKey);

const DAY_MS = 24 * 60 * 60 * 1000;

function makeSelfKey(overrides = {}) {
  return makeApiKey({
    name: 'self_abc123',
    description: 'Self-service key for alice',
    api_key: '***1234',
    key_created: false,
    allowed_databases: ['*'],
    allowed_routes: ['query-api', 's3-api'],
    rate_limit_per_minute: 30,
    owner: 'alice-sub-1',
    expires_at: new Date(Date.now() + 10 * DAY_MS).toISOString(),
    ...overrides,
  });
}

describe('MyApiKey', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetMyApiKey.mockResolvedValue(null);
  });

  it('renders empty state with create button when no key exists', async () => {
    renderWithProviders(<MyApiKey />);

    await waitFor(() => {
      expect(screen.getByText('No API key yet')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: 'Create API Key' })).toBeInTheDocument();
  });

  it('creates a key and reveals it once', async () => {
    mockedCreateMyApiKey.mockResolvedValue(
      makeSelfKey({ api_key: 'full-secret-key', key_created: true }),
    );

    renderWithProviders(<MyApiKey />);

    await waitFor(() => {
      expect(screen.getByText('No API key yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Create API Key' }));

    await waitFor(() => {
      expect(screen.getByText('full-secret-key')).toBeInTheDocument();
    });
  });

  it('shows expiry date with remaining days for an active key', async () => {
    mockedGetMyApiKey.mockResolvedValue(makeSelfKey());

    renderWithProviders(<MyApiKey />);

    await waitFor(() => {
      expect(screen.getByText('self_abc123')).toBeInTheDocument();
    });

    expect(screen.getByText('Expires')).toBeInTheDocument();
    expect(screen.getByText('10 days left')).toBeInTheDocument();
    expect(screen.queryByText('Expired')).not.toBeInTheDocument();
  });

  it('shows expired tag and banner for an expired key', async () => {
    mockedGetMyApiKey.mockResolvedValue(
      makeSelfKey({ expires_at: new Date(Date.now() - DAY_MS).toISOString() }),
    );

    renderWithProviders(<MyApiKey />);

    await waitFor(() => {
      expect(screen.getByText('self_abc123')).toBeInTheDocument();
    });

    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(
      screen.getByText('Your API key has expired. Renew it to keep using the same key.'),
    ).toBeInTheDocument();
  });

  it('renews the key (same value) and shows a success toast', async () => {
    mockedGetMyApiKey.mockResolvedValue(makeSelfKey());
    mockedRenewMyApiKey.mockResolvedValue(
      makeSelfKey({ expires_at: new Date(Date.now() + 30 * DAY_MS).toISOString() }),
    );

    renderWithProviders(<MyApiKey />);

    await waitFor(() => {
      expect(screen.getByText('self_abc123')).toBeInTheDocument();
    });

    expect(
      screen.getByText('Renewing extends the expiry by 30 days while keeping the same key value.'),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Renew' }));

    await waitFor(() => {
      expect(mockedRenewMyApiKey).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.getByText('API key renewed for 30 more days.')).toBeInTheDocument();
    });
    // Renewal keeps the same key value — nothing is regenerated.
    expect(mockedRegenerateMyApiKey).not.toHaveBeenCalled();
  });

  it('regenerates the key after confirmation and reveals the new value', async () => {
    mockedGetMyApiKey.mockResolvedValue(makeSelfKey());
    mockedRegenerateMyApiKey.mockResolvedValue(
      makeSelfKey({ api_key: 'brand-new-key', key_created: true }),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<MyApiKey />);

    await waitFor(() => {
      expect(screen.getByText('self_abc123')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Regenerate' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByText('brand-new-key')).toBeInTheDocument();
    });

    vi.restoreAllMocks();
  });
});
