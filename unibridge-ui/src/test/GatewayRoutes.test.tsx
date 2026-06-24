vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getGatewayRoutes: vi.fn(),
  deleteGatewayRoute: vi.fn(),
  testGatewayRoute: vi.fn(),
  getGatewayRouteCurl: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getGatewayRoutes, deleteGatewayRoute, getGatewayRouteCurl } from '../api/client';
import GatewayRoutes from '../pages/GatewayRoutes';
import { renderWithProviders, makeGatewayRoute } from './helpers';

const mockedGetGatewayRoutes = vi.mocked(getGatewayRoutes);
const mockedDeleteGatewayRoute = vi.mocked(deleteGatewayRoute);
const mockedGetGatewayRouteCurl = vi.mocked(getGatewayRouteCurl);
const clipboardWriteText = vi.fn();

describe('GatewayRoutes', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    });
    clipboardWriteText.mockResolvedValue(undefined);
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

    expect(screen.getByText('Authorization')).toBeInTheDocument();
    expect(screen.queryByText('***1234')).not.toBeInTheDocument();
    expect(screen.queryByText('***5678')).not.toBeInTheDocument();
    expect(screen.getAllByText('Hidden')).toHaveLength(2);
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

    expect(screen.queryByText('***9999')).not.toBeInTheDocument();
    expect(screen.getByText('Hidden')).toBeInTheDocument();
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

  it('filters routes by search text', async () => {
    mockedGetGatewayRoutes.mockResolvedValue({
      items: [
        makeGatewayRoute({ id: 'route-1', name: 'orders-route', uri: '/api/orders/*' }),
        makeGatewayRoute({ id: 'route-2', name: 'billing-route', uri: '/api/billing/*', upstream_id: 'billing-upstream' }),
      ],
      total: 2,
    });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('orders-route')).toBeInTheDocument();
    });

    await userEvent.type(screen.getByRole('searchbox', { name: 'Search routes...' }), 'billing');

    expect(screen.queryByText('orders-route')).not.toBeInTheDocument();
    expect(screen.getByText('billing-route')).toBeInTheDocument();

    await userEvent.clear(screen.getByRole('searchbox', { name: 'Search routes...' }));
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search routes...' }), 'missing');

    expect(screen.getByText('No matching routes')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    expect(screen.getByText('orders-route')).toBeInTheDocument();
    expect(screen.getByText('billing-route')).toBeInTheDocument();
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

    expect(screen.getByRole('button', { name: 'Test route test-route' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show cURL for route test-route' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit route test-route' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete route test-route' })).toBeInTheDocument();
  });

  it('opens cURL modal as an accessible dialog', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('test-route')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Show cURL for route test-route' }));

    const dialog = await screen.findByRole('dialog', { name: 'cURL Sample' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByText('test-route')).toBeInTheDocument();
    expect(screen.getByText('curl http://localhost/gateway/route-1')).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole('button', { name: 'Copy cURL command' }));
    await waitFor(() => {
      expect(clipboardWriteText).toHaveBeenCalledWith('curl http://localhost/gateway/route-1');
    });
    expect(within(dialog).getByRole('button', { name: 'cURL command copied' })).toHaveTextContent('Copied');
    expect(within(dialog).getByRole('status')).toHaveTextContent('Copied');
  });

  it('shows copy failure feedback when cURL clipboard write rejects', async () => {
    clipboardWriteText.mockRejectedValueOnce(new Error('blocked'));
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });

    renderWithProviders(<GatewayRoutes />);

    await waitFor(() => {
      expect(screen.getByText('test-route')).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole('button', { name: 'Show cURL for route test-route' }));

    const dialog = await screen.findByRole('dialog', { name: 'cURL Sample' });
    await userEvent.click(within(dialog).getByRole('button', { name: 'Copy cURL command' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to copy cURL command')).toBeInTheDocument();
    });
    expect(within(dialog).getByRole('button', { name: 'Copy cURL command' })).toHaveTextContent('Copy');
    expect(within(dialog).queryByRole('button', { name: 'cURL command copied' })).not.toBeInTheDocument();
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
    expect(screen.getByRole('button', { name: 'Test route test-route' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show cURL for route test-route' })).toBeInTheDocument();
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

    await userEvent.click(screen.getByRole('button', { name: 'Delete route test-route' }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockedDeleteGatewayRoute).toHaveBeenCalledWith('route-1');
    });

    vi.restoreAllMocks();
  });
});
