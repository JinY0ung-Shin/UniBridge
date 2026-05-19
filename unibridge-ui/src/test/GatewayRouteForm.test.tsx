vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getGatewayRoute: vi.fn(),
  saveGatewayRoute: vi.fn(),
  getGatewayUpstreams: vi.fn(),
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useParams: vi.fn(() => ({})), useNavigate: vi.fn(() => vi.fn()) };
});

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useParams } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getGatewayRoute, getGatewayUpstreams, saveGatewayRoute } from '../api/client';
import GatewayRouteForm from '../pages/GatewayRouteForm';
import { renderWithProviders, makeGatewayRoute, makeGatewayUpstream } from './helpers';

const mockedGetGatewayRoute = vi.mocked(getGatewayRoute);
const mockedGetGatewayUpstreams = vi.mocked(getGatewayUpstreams);
const mockedSaveGatewayRoute = vi.mocked(saveGatewayRoute);

function renderWithQueryClient(queryClient: QueryClient) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <GatewayRouteForm />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('GatewayRouteForm', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useParams).mockReturnValue({});
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [], total: 0 });
  });

  it('renders new route form', async () => {
    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'New Route' })).toBeInTheDocument();
    });

    expect(screen.getByPlaceholderText('My API Route')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('myservice/*')).toBeInTheDocument();
    expect(screen.getByText('/api/')).toBeInTheDocument();
  });

  it('renders method checkboxes', async () => {
    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'New Route' })).toBeInTheDocument();
    });

    for (const method of ['GET', 'POST', 'PUT', 'DELETE', 'PATCH']) {
      expect(screen.getByRole('checkbox', { name: method })).toBeInTheDocument();
    }
  });

  it('renders upstream selector with option from query', async () => {
    const upstream = makeGatewayUpstream({ id: 'us-1', name: 'my-upstream' });
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });

    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'my-upstream' })).toBeInTheDocument();
    });
  });

  it('submits multiple service keys for a new route', async () => {
    const user = userEvent.setup();
    const upstream = makeGatewayUpstream({ id: 'us-1', name: 'my-upstream' });
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });
    mockedSaveGatewayRoute.mockResolvedValue(makeGatewayRoute());

    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'my-upstream' })).toBeInTheDocument();
    });

    await user.type(screen.getByPlaceholderText('myservice/*'), 'external/*');
    await user.selectOptions(screen.getAllByRole('combobox')[1], 'us-1');
    await user.click(screen.getByRole('button', { name: '+ Add Header' }));
    await user.click(screen.getByRole('button', { name: '+ Add Header' }));

    const headerNameInputs = screen.getAllByPlaceholderText('Authorization');
    const headerValueInputs = screen.getAllByPlaceholderText('Bearer sk-xxx...');
    await user.type(headerNameInputs[0], 'X-Api-Key');
    await user.type(headerValueInputs[0], 'secret-1');
    await user.type(headerNameInputs[1], 'Authorization');
    await user.type(headerValueInputs[1], 'Bearer secret-2');

    await user.click(screen.getByRole('button', { name: 'Create Route' }));

    await waitFor(() => {
      expect(mockedSaveGatewayRoute).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({
          service_keys: [
            { header_name: 'X-Api-Key', header_value: 'secret-1' },
            { header_name: 'Authorization', header_value: 'Bearer secret-2' },
          ],
        }),
      );
    });
  });

  it('submit button is disabled when no upstream selected', async () => {
    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'New Route' })).toBeInTheDocument();
    });

    const submitButton = screen.getByRole('button', { name: 'Create Route' });
    expect(submitButton).toBeDisabled();
  });

  it('cancel button is present', async () => {
    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'New Route' })).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('shows saved route values when reopening edit form from a fresh detail cache', async () => {
    vi.mocked(useParams).mockReturnValue({ id: 'route-1' });
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: 30_000 },
        mutations: { retry: false },
      },
    });
    const originalRoute = makeGatewayRoute({ name: 'old-route' });
    const savedRoute = makeGatewayRoute({ name: 'new-route' });
    queryClient.setQueryData(['gateway-route', 'route-1'], originalRoute);
    mockedGetGatewayRoute.mockResolvedValue(savedRoute);
    mockedGetGatewayUpstreams.mockResolvedValue({
      items: [makeGatewayUpstream({ id: 'upstream-1' })],
      total: 1,
    });
    mockedSaveGatewayRoute.mockResolvedValue(savedRoute);

    const user = userEvent.setup();
    const firstRender = renderWithQueryClient(queryClient);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Edit Route' })).toBeInTheDocument();
    });
    const nameInput = screen.getByPlaceholderText('My API Route');
    expect(nameInput).toHaveValue('old-route');

    await user.clear(nameInput);
    await user.type(nameInput, 'new-route');
    await user.click(screen.getByRole('button', { name: 'Update Route' }));

    await waitFor(() => {
      expect(mockedSaveGatewayRoute).toHaveBeenCalledWith(
        'route-1',
        expect.objectContaining({ name: 'new-route' }),
      );
    });

    firstRender.unmount();
    renderWithQueryClient(queryClient);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('My API Route')).toHaveValue('new-route');
    });
  });

  it('preserves existing service key values when editing without retyping secrets', async () => {
    vi.mocked(useParams).mockReturnValue({ id: 'route-1' });
    const user = userEvent.setup();
    const route = makeGatewayRoute({
      service_keys: [
        { header_name: 'X-Api-Key', header_value: '***1234' },
        { header_name: 'Authorization', header_value: '***5678' },
      ],
    });
    mockedGetGatewayRoute.mockResolvedValue(route);
    mockedSaveGatewayRoute.mockResolvedValue(route);

    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Edit Route' })).toBeInTheDocument();
    });

    expect(screen.getByDisplayValue('X-Api-Key')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('***1234')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Authorization')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('***5678')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Update Route' }));

    await waitFor(() => {
      expect(mockedSaveGatewayRoute).toHaveBeenCalledWith(
        'route-1',
        expect.objectContaining({
          service_keys: [
            { header_name: 'X-Api-Key', header_value: '' },
            { header_name: 'Authorization', header_value: '' },
          ],
        }),
      );
    });
  });
});
