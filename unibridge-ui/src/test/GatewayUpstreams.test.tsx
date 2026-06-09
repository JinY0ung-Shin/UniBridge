vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getGatewayUpstreams: vi.fn(),
  saveGatewayUpstream: vi.fn(),
  deleteGatewayUpstream: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getGatewayUpstreams, saveGatewayUpstream, deleteGatewayUpstream } from '../api/client';
import GatewayUpstreams from '../pages/GatewayUpstreams';
import { renderWithProviders, makeGatewayUpstream } from './helpers';

const mockedGetGatewayUpstreams = vi.mocked(getGatewayUpstreams);
const mockedSaveGatewayUpstream = vi.mocked(saveGatewayUpstream);
const mockedDeleteGatewayUpstream = vi.mocked(deleteGatewayUpstream);

describe('GatewayUpstreams', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [], total: 0 });
  });

  it('renders loading state', () => {
    mockedGetGatewayUpstreams.mockReturnValue(new Promise(() => {}));

    renderWithProviders(<GatewayUpstreams />);

    expect(screen.getByText('Loading upstreams...')).toBeInTheDocument();
  });

  it('renders upstreams table', async () => {
    const upstream = makeGatewayUpstream();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('test-upstream')).toBeInTheDocument();
    });

    expect(screen.getByText('roundrobin')).toBeInTheDocument();
    expect(screen.getByText('HTTP')).toBeInTheDocument();
    expect(screen.getByText('localhost:3000 (w:1)')).toBeInTheDocument();
  });

  it('submits https upstreams with the selected scheme and default port', async () => {
    const user = userEvent.setup();
    mockedSaveGatewayUpstream.mockResolvedValue(makeGatewayUpstream({ scheme: 'https', nodes: { 'secure.example.com:443': 1 } }));

    renderWithProviders(<GatewayUpstreams />);

    await user.click(screen.getByRole('button', { name: '+ Add Upstream' }));
    await user.type(screen.getByPlaceholderText('my-backend'), 'secure-api');
    await user.selectOptions(screen.getAllByRole('combobox')[0], 'https');
    await user.type(screen.getByPlaceholderText('e.g. 192.168.1.10 or api.example.com'), 'secure.example.com');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(mockedSaveGatewayUpstream).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          name: 'secure-api',
          scheme: 'https',
          nodes: { 'secure.example.com:443': 1 },
        }),
      );
    });
  });

  it('hides write actions for users with read-only upstream permission', async () => {
    const upstream = makeGatewayUpstream();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });

    renderWithProviders(<GatewayUpstreams />, {
      permissions: ['gateway.upstreams.read'],
    });

    await waitFor(() => {
      expect(screen.getByText('test-upstream')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: '+ Add Upstream' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('renders empty state when no upstreams', async () => {
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [], total: 0 });

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('No upstreams')).toBeInTheDocument();
    });
  });

  it('opens create modal on add button click', async () => {
    renderWithProviders(<GatewayUpstreams />);

    await userEvent.click(screen.getByRole('button', { name: '+ Add Upstream' }));

    const dialog = screen.getByRole('dialog', { name: 'Add Upstream' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });

  it('opens edit modal on edit button click', async () => {
    const upstream = makeGatewayUpstream();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('test-upstream')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));

    expect(screen.getByText('Edit Upstream')).toBeInTheDocument();
  });

  it('calls deleteGatewayUpstream after confirmation', async () => {
    const upstream = makeGatewayUpstream();
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });
    mockedDeleteGatewayUpstream.mockResolvedValue(undefined);

    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<GatewayUpstreams />);

    await waitFor(() => {
      expect(screen.getByText('test-upstream')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeleteGatewayUpstream).toHaveBeenCalledWith('upstream-1');
    });

    vi.restoreAllMocks();
  });
});
