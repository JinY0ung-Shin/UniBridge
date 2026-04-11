vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getGatewayRoutes: vi.fn(),
  deleteGatewayRoute: vi.fn(),
  testGatewayRoute: vi.fn(),
  getGatewayRouteCurl: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getGatewayRoutes, deleteGatewayRoute } from '../api/client';
import GatewayRoutes from '../pages/GatewayRoutes';
import { renderWithProviders, makeGatewayRoute } from './helpers';

const mockedGetGatewayRoutes = vi.mocked(getGatewayRoutes);
const mockedDeleteGatewayRoute = vi.mocked(deleteGatewayRoute);

describe('GatewayRoutes', () => {
  beforeEach(() => {
    mockedGetGatewayRoutes.mockResolvedValue({ items: [], total: 0 });
  });

  it('renders routes table', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('test-route')).toBeInTheDocument();
    });

    expect(screen.getByText('/api/test/*')).toBeInTheDocument();
  });

  it('renders empty state when no routes', async () => {
    mockedGetGatewayRoutes.mockResolvedValue({ items: [], total: 0 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('No gateway routes')).toBeInTheDocument();
    });
  });

  it('renders method badges', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('GET')).toBeInTheDocument();
    });

    expect(screen.getByText('POST')).toBeInTheDocument();
  });

  it('shows active status badge', async () => {
    const route = makeGatewayRoute({ status: 1 });
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('Active')).toBeInTheDocument();
    });
  });

  it('renders action buttons', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('test-route')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: 'Test' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'cURL' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('calls deleteGatewayRoute after confirmation', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });
    mockedDeleteGatewayRoute.mockResolvedValue(undefined);

    vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('test-route')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeleteGatewayRoute).toHaveBeenCalledWith('route-1');
    });

    vi.restoreAllMocks();
  });
});
