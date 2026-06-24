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
  deleteMyApiKey,
} from '../api/client';
import MyApiKey from '../pages/MyApiKey';
import { renderWithProviders, makeApiKey } from './helpers';

const mockedGetMyApiKey = vi.mocked(getMyApiKey);
const mockedCreateMyApiKey = vi.mocked(createMyApiKey);
const mockedRegenerateMyApiKey = vi.mocked(regenerateMyApiKey);
const mockedRenewMyApiKey = vi.mocked(renewMyApiKey);
const mockedDeleteMyApiKey = vi.mocked(deleteMyApiKey);
const clipboardWriteText = vi.fn();

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
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    });
    clipboardWriteText.mockResolvedValue(undefined);
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
    expect(screen.getByRole('status')).toHaveTextContent(
      'Here is your API key. Copy it now',
    );

    await userEvent.click(screen.getByRole('button', { name: 'Copy your revealed API key' }));
    await waitFor(() => {
      expect(clipboardWriteText).toHaveBeenCalledWith('full-secret-key');
    });
    expect(screen.getByRole('button', { name: 'Your API key was copied' })).toHaveTextContent('Copied!');
  });

  it('marks the create button as busy while creating a key', async () => {
    let resolveCreate: (value: ReturnType<typeof makeSelfKey>) => void = () => {};
    mockedCreateMyApiKey.mockReturnValueOnce(new Promise<ReturnType<typeof makeSelfKey>>((resolve) => {
      resolveCreate = resolve;
    }));

    renderWithProviders(<MyApiKey />);

    await waitFor(() => {
      expect(screen.getByText('No API key yet')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Create API Key' }));

    expect(screen.getByRole('button', { name: 'Saving...' })).toHaveAttribute('aria-busy', 'true');

    resolveCreate(makeSelfKey({ api_key: 'full-secret-key', key_created: true }));

    await waitFor(() => {
      expect(screen.getByText('full-secret-key')).toBeInTheDocument();
    });
  });

  it('shows copy failure feedback when the one-time key cannot be copied', async () => {
    clipboardWriteText.mockRejectedValueOnce(new Error('blocked'));
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

    await userEvent.click(screen.getByRole('button', { name: 'Copy your revealed API key' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to copy API key')).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Copy your revealed API key' })).toHaveTextContent('Copy');
    expect(screen.queryByRole('button', { name: 'Your API key was copied' })).not.toBeInTheDocument();
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
    expect(screen.getByText('30 days left')).toBeInTheDocument();
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

  it('deletes the key after confirmation and returns to the empty state', async () => {
    mockedGetMyApiKey.mockResolvedValue(makeSelfKey());
    mockedDeleteMyApiKey.mockResolvedValue(undefined);
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<MyApiKey />);

    await waitFor(() => {
      expect(screen.getByText('self_abc123')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

    expect(window.confirm).toHaveBeenCalledWith(
      'Delete your API key? This cannot be undone.',
    );
    await waitFor(() => {
      expect(mockedDeleteMyApiKey).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.getByText('No API key yet')).toBeInTheDocument();
    });
    expect(screen.getByText('API key deleted')).toBeInTheDocument();

    vi.restoreAllMocks();
  });
});
