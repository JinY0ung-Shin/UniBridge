vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getGatewayUpstreams: vi.fn(),
  saveGatewayUpstream: vi.fn(),
  deleteGatewayUpstream: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getGatewayUpstreams, deleteGatewayUpstream } from '../api/client';
import GatewayUpstreams from '../pages/GatewayUpstreams';
import { renderWithProviders, makeGatewayUpstream } from './helpers';

const mockedGetGatewayUpstreams = vi.mocked(getGatewayUpstreams);
const mockedDeleteGatewayUpstream = vi.mocked(deleteGatewayUpstream);

describe('GatewayUpstreams', () => {
  beforeEach(() => {
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
    expect(screen.getByText('localhost:3000 (w:1)')).toBeInTheDocument();
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

    expect(screen.getByText('Add Upstream')).toBeInTheDocument();
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
