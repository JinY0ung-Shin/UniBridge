vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getGatewayRoutes: vi.fn(),
  deleteGatewayRoute: vi.fn(),
  testGatewayRoute: vi.fn(),
  getGatewayRouteCurl: vi.fn(),
  getGatewayOpenApiSpec: vi.fn(),
}));

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  getGatewayRoutes,
  deleteGatewayRoute,
  testGatewayRoute,
  getGatewayRouteCurl,
  getGatewayOpenApiSpec,
} from '../api/client';
import GatewayRoutes from '../pages/GatewayRoutes';
import { renderWithProviders, makeGatewayRoute } from './helpers';

const mockedGetGatewayRoutes = vi.mocked(getGatewayRoutes);
const mockedDeleteGatewayRoute = vi.mocked(deleteGatewayRoute);
const mockedTestGatewayRoute = vi.mocked(testGatewayRoute);
const mockedGetGatewayRouteCurl = vi.mocked(getGatewayRouteCurl);
const mockedGetGatewayOpenApiSpec = vi.mocked(getGatewayOpenApiSpec);
const clipboardWriteText = vi.fn();
const originalClipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
const originalCreateObjectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectUrlDescriptor = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

function restoreProperty(target: object, key: PropertyKey, descriptor: PropertyDescriptor | undefined) {
  if (descriptor) {
    Object.defineProperty(target, key, descriptor);
  } else {
    Reflect.deleteProperty(target, key);
  }
}

describe('GatewayRoutes', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    });
    clipboardWriteText.mockResolvedValue(undefined);
    mockedGetGatewayRoutes.mockResolvedValue({ items: [], total: 0 });
    mockedGetGatewayRouteCurl.mockResolvedValue({ curl: 'curl http://localhost/gateway/route-1' });
    mockedGetGatewayOpenApiSpec.mockResolvedValue({ openapi: '3.1.0', paths: {} });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    restoreProperty(navigator, 'clipboard', originalClipboardDescriptor);
    restoreProperty(URL, 'createObjectURL', originalCreateObjectUrlDescriptor);
    restoreProperty(URL, 'revokeObjectURL', originalRevokeObjectUrlDescriptor);
  });

  it('renders loading and load-error feedback', async () => {
    let resolveRoutes!: (value: { items: []; total: number }) => void;
    mockedGetGatewayRoutes.mockReturnValue(new Promise((resolve) => { resolveRoutes = resolve; }));
    const loading = renderWithProviders(<GatewayRoutes />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading routes...');
    resolveRoutes({ items: [], total: 0 });
    expect(await screen.findByText('No gateway routes')).toBeInTheDocument();
    loading.unmount();

    mockedGetGatewayRoutes.mockRejectedValueOnce(new Error('offline'));
    renderWithProviders(<GatewayRoutes />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load gateway routes');
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

  it('renders disabled, system, unnamed, and ALL-method fallbacks', async () => {
    mockedGetGatewayRoutes.mockResolvedValue({
      items: [makeGatewayRoute({
        id: 'system-route',
        name: '',
        uri: '/api/system/*',
        methods: undefined,
        upstream_id: '',
        status: 0,
        system: true,
      })],
      total: 1,
    });
    renderWithProviders(<GatewayRoutes />);

    const row = (await screen.findByText('/api/system/*')).closest('tr');
    expect(row).not.toBeNull();
    expect(within(row!).getAllByText('—')).toHaveLength(3);
    expect(within(row!).getByText('ALL')).toHaveClass('method-badge--patch');
    expect(within(row!).getByText('Disabled')).toHaveClass('badge-unknown');
    expect(within(row!).getByText('System')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit route /api/system/*' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete route /api/system/*' })).not.toBeInTheDocument();
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

  it('closes the cURL dialog and reports cURL fetch failures', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });
    const user = userEvent.setup();
    renderWithProviders(<GatewayRoutes />);
    await screen.findByText('test-route');

    await user.click(screen.getByRole('button', { name: 'Show cURL for route test-route' }));
    const dialog = await screen.findByRole('dialog', { name: 'cURL Sample' });
    await user.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog', { name: 'cURL Sample' })).not.toBeInTheDocument();

    mockedGetGatewayRouteCurl.mockRejectedValueOnce(new Error('unavailable'));
    await user.click(screen.getByRole('button', { name: 'Show cURL for route test-route' }));
    expect(await screen.findByText('Failed to generate cURL command')).toBeInTheDocument();
  });

  it('shows pending then success feedback for a reachable route test', async () => {
    const route = makeGatewayRoute();
    mockedGetGatewayRoutes.mockResolvedValue({ items: [route], total: 1 });
    let resolveTest!: (value: {
      reachable: boolean;
      status_code: number;
      response_time_ms: number;
      node: string;
      body: unknown;
    }) => void;
    mockedTestGatewayRoute.mockReturnValue(new Promise((resolve) => { resolveTest = resolve; }));
    const user = userEvent.setup();
    renderWithProviders(<GatewayRoutes />);
    await screen.findByText('test-route');

    await user.click(screen.getByRole('button', { name: 'Test route test-route' }));
    expect(screen.getByText('Testing...')).toHaveClass('test-result--pending');
    expect(screen.getByRole('button', { name: 'Test route test-route' })).toBeDisabled();
    resolveTest({
      reachable: true,
      status_code: 200,
      response_time_ms: 12,
      node: 'upstream-a',
      body: { ok: true },
    });

    expect(await screen.findByText('OK')).toHaveClass('test-result--ok');
    expect(await screen.findByText(/upstream-a/)).toHaveTextContent('"ok": true');
    expect(screen.getByText('test-route — 200 (12ms)')).toBeInTheDocument();
  });

  it('shows failure badges for unreachable and rejected route tests', async () => {
    mockedGetGatewayRoutes.mockResolvedValue({
      items: [
        makeGatewayRoute({ id: 'route-1', name: 'unreachable-route' }),
        makeGatewayRoute({ id: 'route-2', name: 'throwing-route' }),
      ],
      total: 2,
    });
    mockedTestGatewayRoute
      .mockResolvedValueOnce({
        reachable: false,
        status_code: null,
        response_time_ms: 5,
        node: 'upstream-b',
        body: '',
        error: 'connection refused',
      })
      .mockRejectedValueOnce(new Error('network'));
    const user = userEvent.setup();
    renderWithProviders(<GatewayRoutes />);
    await screen.findByText('unreachable-route');

    await user.click(screen.getByRole('button', { name: 'Test route unreachable-route' }));
    expect(await screen.findByText(/upstream-b/)).toHaveTextContent('connection refused');
    expect(screen.getAllByText('Error').some((node) => node.classList.contains('test-result--fail'))).toBe(true);

    await user.click(screen.getByRole('button', { name: 'Test route throwing-route' }));
    await waitFor(() => expect(mockedTestGatewayRoute).toHaveBeenCalledWith('route-2'));
    expect(screen.getAllByText(/Unreachable/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('Error').filter((node) => node.classList.contains('test-result--fail'))).toHaveLength(2);
  });

  it('downloads the OpenAPI document and reports generation failures', async () => {
    const createObjectURL = vi.fn(() => 'blob:openapi');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const user = userEvent.setup();
    renderWithProviders(<GatewayRoutes />);

    await user.click(screen.getByRole('button', { name: 'OpenAPI Spec' }));
    await waitFor(() => expect(mockedGetGatewayOpenApiSpec).toHaveBeenCalledOnce());
    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(click).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:openapi');

    mockedGetGatewayOpenApiSpec.mockRejectedValueOnce(new Error('spec unavailable'));
    await user.click(screen.getByRole('button', { name: 'OpenAPI Spec' }));
    expect(await screen.findByText('Failed to generate OpenAPI spec')).toBeInTheDocument();
  });

  it('navigates to add and edit forms', async () => {
    mockedGetGatewayRoutes.mockResolvedValue({ items: [makeGatewayRoute()], total: 1 });
    const user = userEvent.setup();
    renderWithProviders(<GatewayRoutes />);
    await screen.findByText('test-route');

    await user.click(screen.getByRole('button', { name: '+ Add Route' }));
    expect(window.location.pathname).toBe('/gateway/routes/new');
    window.history.replaceState({}, '', '/');
    await user.click(screen.getByRole('button', { name: 'Edit route test-route' }));
    expect(window.location.pathname).toBe('/gateway/routes/route-1/edit');
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

  });

  it('does not delete when confirmation is declined and surfaces backend delete detail', async () => {
    mockedGetGatewayRoutes.mockResolvedValue({ items: [makeGatewayRoute()], total: 1 });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    mockedDeleteGatewayRoute.mockRejectedValueOnce({ response: { data: { detail: 'System dependency exists' } } });
    const user = userEvent.setup();
    renderWithProviders(<GatewayRoutes />);
    await screen.findByText('test-route');

    await user.click(screen.getByRole('button', { name: 'Delete route test-route' }));
    expect(mockedDeleteGatewayRoute).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Delete route test-route' }));
    await waitFor(() => expect(mockedDeleteGatewayRoute).toHaveBeenCalledWith('route-1'));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(await screen.findByText('System dependency exists')).toBeInTheDocument();
  });
});
