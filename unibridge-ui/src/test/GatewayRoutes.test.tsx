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
import { getGatewayRoutes, deleteGatewayRoute, getGatewayRouteCurl } from '../api/client';
import GatewayRoutes from '../pages/GatewayRoutes';
import { renderWithProviders, makeGatewayRoute } from './helpers';

const mockedGetGatewayRoutes = vi.mocked(getGatewayRoutes);
const mockedDeleteGatewayRoute = vi.mocked(deleteGatewayRoute);
const mockedGetGatewayRouteCurl = vi.mocked(getGatewayRouteCurl);

describe('GatewayRoutes', () => {
  beforeEach(() => {
    mockedGetGatewayRoutes.mockResolvedValue({ items: [], total: 0 });
    mockedGetGatewayRouteCurl.mockResolvedValue({ curl: 'curl http://localhost/gateway/route-1' });
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

  it('renders multiple service keys', async () => {
    const route = makeGatewayRoute({
      service_keys: [
        { header_name: 'X-Api-Key', header_value: '***1234' },
        { header_name: 'Authorization', header_value: '***5678' },
      ],
    });
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('X-Api-Key')).toBeInTheDocument();
    });

    expect(screen.getByText('***1234')).toBeInTheDocument();
    expect(screen.getByText('Authorization')).toBeInTheDocument();
    expect(screen.getByText('***5678')).toBeInTheDocument();
  });

  it('renders legacy service key when service_keys is absent', async () => {
    const route = makeGatewayRoute({
      service_key: { header_name: 'X-Legacy-Key', header_value: '***9999' },
      service_keys: undefined,
    });
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('X-Legacy-Key')).toBeInTheDocument();
    });

    expect(screen.getByText('***9999')).toBeInTheDocument();
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

  it('opens cURL modal as an accessible dialog', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('test-route')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'cURL' }));

    const dialog = await screen.findByRole('dialog', { name: 'cURL Sample' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(screen.getByText('curl http://localhost/gateway/route-1')).toBeInTheDocument();
  });

  it('hides write actions for users with read-only route permission', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />, {
      permissions: ['gateway.routes.read'],
    });

    await waitFor(() => {
      expect(screen.getByText('test-route')).toBeInTheDocument();
    });

    expect(screen.queryByRole('button', { name: '+ Add Route' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Test' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'cURL' })).toBeInTheDocument();
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
