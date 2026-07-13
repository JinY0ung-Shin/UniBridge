vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getGatewayRoute: vi.fn(),
  saveGatewayRoute: vi.fn(),
  getGatewayUpstreams: vi.fn(),
  getAlertResourceOwners: vi.fn(),
  setAlertResourceOwner: vi.fn(),
}));

const navigateMock = vi.hoisted(() => vi.fn());

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useParams: vi.fn(() => ({})), useNavigate: vi.fn(() => navigateMock) };
});

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useParams } from 'react-router-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getGatewayRoute,
  getGatewayUpstreams,
  saveGatewayRoute,
  getAlertResourceOwners,
  setAlertResourceOwner,
} from '../api/client';
import GatewayRouteForm from '../pages/GatewayRouteForm';
import { renderWithProviders, makeGatewayRoute, makeGatewayUpstream } from './helpers';

const mockedGetGatewayRoute = vi.mocked(getGatewayRoute);
const mockedGetGatewayUpstreams = vi.mocked(getGatewayUpstreams);
const mockedSaveGatewayRoute = vi.mocked(saveGatewayRoute);
const mockedGetAlertResourceOwners = vi.mocked(getAlertResourceOwners);
const mockedSetAlertResourceOwner = vi.mocked(setAlertResourceOwner);

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
    mockedGetAlertResourceOwners.mockResolvedValue([]);
    mockedSetAlertResourceOwner.mockResolvedValue({
      resource_type: 'route',
      resource_id: 'r1',
      display_name: 'r1',
      emails: [],
      alerts_enabled: true,
    });
  });

  it('renders new route form', async () => {
    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'New Route' })).toBeInTheDocument();
    });

    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveAttribute('id', 'gateway-route-name');
    expect(screen.getByRole('textbox', { name: 'URI' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Methods' })).toHaveAttribute(
      'aria-labelledby',
      'gateway-route-methods-label',
    );
    expect(screen.getByRole('combobox', { name: 'Upstream' })).toHaveAttribute('id', 'gateway-route-upstream');
    expect(screen.getByText('/api/')).toBeInTheDocument();
    expect(screen.getByText('(Upstream)')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'URI' })).toHaveAttribute(
      'aria-describedby',
      'gateway-route-uri-hint',
    );
    expect(document.getElementById('gateway-route-uri-hint')).toHaveTextContent(
      'Must start with /api/',
    );
    expect(screen.getByRole('spinbutton', { name: 'Timeout (seconds)' })).toHaveAttribute(
      'aria-describedby',
      'gateway-route-timeout-hint',
    );
    expect(document.getElementById('gateway-route-timeout-hint')).toHaveTextContent(
      'Leave blank to use the global default timeout',
    );
    expect(screen.getByRole('checkbox', { name: 'Require Authentication (key-auth)' })).toHaveAttribute(
      'aria-describedby',
      'gateway-route-require-auth-hint',
    );
    expect(document.getElementById('gateway-route-require-auth-hint')).toHaveTextContent(
      'Consumer registration required',
    );
    expect(screen.getByRole('textbox', { name: 'Assignee emails' })).toHaveAttribute(
      'aria-describedby',
      'gateway-route-assignees-hint',
    );
    expect(document.getElementById('gateway-route-assignees-hint')).toHaveTextContent(
      'Emails notified of this route',
    );
  });

  it('renders method checkboxes', async () => {
    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'New Route' })).toBeInTheDocument();
    });

    expect(screen.getByRole('group', { name: 'Methods' })).toBeInTheDocument();
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

    await user.type(screen.getByRole('textbox', { name: 'URI' }), 'external/*');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Upstream' }), 'us-1');
    await user.click(screen.getByRole('button', { name: '+ Add Header' }));
    await user.click(screen.getByRole('button', { name: '+ Add Header' }));
    expect(screen.getByRole('button', { name: 'Remove header row 1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove header row 2' })).toBeInTheDocument();

    const headerNameInputs = screen.getAllByRole('textbox', { name: /Header Name/ });
    const headerValueInputs = screen.getAllByLabelText(/Header Value/);
    expect(headerNameInputs[0]).toHaveAttribute('id', 'gateway-route-header-name-1');
    expect(headerValueInputs[0]).toHaveAttribute('id', 'gateway-route-header-value-1');
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

  it('submits all routing controls, filters incomplete keys, and normalizes the URI', async () => {
    const upstream = makeGatewayUpstream({ id: 'us-1', name: 'my-upstream' });
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [upstream], total: 1 });
    mockedSaveGatewayRoute.mockResolvedValue(makeGatewayRoute());
    const user = userEvent.setup();
    renderWithProviders(<GatewayRouteForm />);
    await screen.findByRole('option', { name: 'my-upstream' });

    await user.type(screen.getByRole('textbox', { name: 'Name' }), '  public orders  ');
    await user.type(screen.getByRole('textbox', { name: 'URI' }), '/orders/*');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Status' }), '0');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Upstream' }), 'us-1');
    await user.click(screen.getByRole('checkbox', { name: 'GET' }));
    await user.click(screen.getByRole('checkbox', { name: 'PATCH' }));
    await user.click(screen.getByRole('checkbox', { name: 'Strip URI Prefix' }));
    await user.click(screen.getByRole('checkbox', { name: 'Require Authentication (key-auth)' }));
    await user.type(screen.getByRole('spinbutton', { name: 'Timeout (seconds)' }), '45');

    await user.click(screen.getByRole('button', { name: '+ Add Header' }));
    await user.type(screen.getByRole('textbox', { name: 'Header Name 1' }), ' X-Empty ');
    await user.click(screen.getByRole('button', { name: '+ Add Header' }));
    await user.type(screen.getByLabelText('Header Value 2'), 'secret-without-name');
    await user.click(screen.getByRole('button', { name: '+ Add Header' }));
    await user.type(screen.getByRole('textbox', { name: 'Header Name 3' }), ' Authorization ');
    await user.type(screen.getByLabelText('Header Value 3'), ' Bearer secret ');
    await user.click(screen.getByRole('button', { name: 'Remove header row 2' }));
    await user.click(screen.getByRole('button', { name: 'Create Route' }));

    await waitFor(() => expect(mockedSaveGatewayRoute).toHaveBeenCalledWith(
      expect.any(String),
      {
        name: 'public orders',
        uri: '/api/orders/*',
        methods: ['POST', 'PATCH'],
        upstream_id: 'us-1',
        status: 0,
        require_auth: true,
        strip_prefix: false,
        timeout: 45,
        service_keys: [{ header_name: 'Authorization', header_value: 'Bearer secret' }],
      },
    ));
    expect(navigateMock).toHaveBeenCalledWith('/gateway/routes');
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

  it('cancel navigates back without saving', async () => {
    const user = userEvent.setup();
    renderWithProviders(<GatewayRouteForm />);
    await screen.findByRole('heading', { name: 'New Route' });
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(navigateMock).toHaveBeenCalledWith('/gateway/routes');
    expect(mockedSaveGatewayRoute).not.toHaveBeenCalled();
  });

  it('shows backend and generic save errors', async () => {
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [makeGatewayUpstream()], total: 1 });
    mockedSaveGatewayRoute
      .mockRejectedValueOnce({ response: { data: { detail: 'URI already exists' } } })
      .mockRejectedValueOnce(new Error('network'));
    const user = userEvent.setup();
    renderWithProviders(<GatewayRouteForm />);
    await screen.findByRole('option', { name: 'test-upstream' });
    await user.type(screen.getByRole('textbox', { name: 'URI' }), 'orders/*');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Upstream' }), 'upstream-1');

    await user.click(screen.getByRole('button', { name: 'Create Route' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('URI already exists');
    await user.click(screen.getByRole('button', { name: 'Create Route' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to save route');
    expect(navigateMock).not.toHaveBeenCalled();
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
    const nameInput = screen.getByRole('textbox', { name: 'Name' });
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
      expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue('new-route');
    });
  });

  it('does not rewrite assignees on edit when they are unchanged', async () => {
    vi.mocked(useParams).mockReturnValue({ id: 'route-1' });
    const user = userEvent.setup();
    const route = makeGatewayRoute({ name: 'old-route' });
    mockedGetGatewayRoute.mockResolvedValue(route);
    mockedSaveGatewayRoute.mockResolvedValue(route);
    mockedGetAlertResourceOwners.mockResolvedValue([
      {
        resource_type: 'route',
        resource_id: 'route-1',
        display_name: 'route-1',
        emails: ['a@b.com'],
        alerts_enabled: true,
      },
    ]);

    renderWithProviders(<GatewayRouteForm />);

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Edit Route' })).toBeInTheDocument());
    // assignee field prefilled from loaded owners
    expect(screen.getByRole('textbox', { name: 'Assignee emails' })).toHaveValue('a@b.com');

    await user.clear(screen.getByRole('textbox', { name: 'Name' }));
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'new-route');
    await user.click(screen.getByRole('button', { name: 'Update Route' }));

    await waitFor(() => expect(mockedSaveGatewayRoute).toHaveBeenCalledTimes(1));
    // unchanged assignees must NOT trigger a (potentially destructive) PUT
    expect(mockedSetAlertResourceOwner).not.toHaveBeenCalled();
  });

  it('updates normalized assignees when they change', async () => {
    vi.mocked(useParams).mockReturnValue({ id: 'route-1' });
    const route = makeGatewayRoute();
    mockedGetGatewayRoute.mockResolvedValue(route);
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [makeGatewayUpstream()], total: 1 });
    mockedGetAlertResourceOwners.mockResolvedValue([{
      resource_type: 'route',
      resource_id: 'route-1',
      display_name: 'route-1',
      emails: ['old@example.com'],
      alerts_enabled: true,
    }]);
    mockedSaveGatewayRoute.mockResolvedValue(route);
    const user = userEvent.setup();
    renderWithProviders(<GatewayRouteForm />);
    const assignees = await screen.findByRole('textbox', { name: 'Assignee emails' });

    await user.clear(assignees);
    await user.type(assignees, 'alice@example.com,\n bob@example.com, ,');
    await user.click(screen.getByRole('button', { name: 'Update Route' }));
    await waitFor(() => expect(mockedSetAlertResourceOwner).toHaveBeenCalledWith('route', 'route-1', {
      emails: ['alice@example.com', 'bob@example.com'],
    }));
    expect(navigateMock).toHaveBeenCalledWith('/gateway/routes');
  });

  it('reports an assignee update failure but still completes the saved route flow', async () => {
    vi.mocked(useParams).mockReturnValue({ id: 'route-1' });
    const route = makeGatewayRoute();
    mockedGetGatewayRoute.mockResolvedValue(route);
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [makeGatewayUpstream()], total: 1 });
    mockedGetAlertResourceOwners.mockResolvedValue([]);
    mockedSaveGatewayRoute.mockResolvedValue(route);
    mockedSetAlertResourceOwner.mockRejectedValueOnce(new Error('alerts unavailable'));
    const user = userEvent.setup();
    renderWithProviders(<GatewayRouteForm />);
    const assignees = await screen.findByRole('textbox', { name: 'Assignee emails' });
    await user.type(assignees, 'owner@example.com');
    await user.click(screen.getByRole('button', { name: 'Update Route' }));

    expect(await screen.findByText('An error occurred')).toBeInTheDocument();
    expect(navigateMock).toHaveBeenCalledWith('/gateway/routes');
  });

  it('loads edit route and assignee baselines before mounting the editor', async () => {
    vi.mocked(useParams).mockReturnValue({ id: 'route-1' });
    let resolveRoute!: (value: ReturnType<typeof makeGatewayRoute>) => void;
    mockedGetGatewayRoute.mockReturnValue(new Promise((resolve) => { resolveRoute = resolve; }));
    const loadingRoute = renderWithProviders(<GatewayRouteForm />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading route...');
    resolveRoute(makeGatewayRoute());
    expect(await screen.findByRole('heading', { name: 'Edit Route' })).toBeInTheDocument();
    loadingRoute.unmount();

    mockedGetGatewayRoute.mockResolvedValue(makeGatewayRoute());
    let resolveOwners!: (value: []) => void;
    mockedGetAlertResourceOwners.mockReturnValue(new Promise((resolve) => { resolveOwners = resolve; }));
    renderWithProviders(<GatewayRouteForm />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading route...');
    resolveOwners([]);
    expect(await screen.findByRole('heading', { name: 'Edit Route' })).toBeInTheDocument();
  });

  it('hides the assignee field for users without alert permissions', async () => {
    renderWithProviders(<GatewayRouteForm />, {
      permissions: ['gateway.routes.read', 'gateway.routes.write'],
    });

    await waitFor(() => expect(screen.getByRole('heading', { name: 'New Route' })).toBeInTheDocument());

    expect(screen.getByRole('textbox', { name: 'Name' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Assignee emails' })).not.toBeInTheDocument();
    expect(mockedGetAlertResourceOwners).not.toHaveBeenCalled();
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

    expect(screen.getByLabelText('Header Name 1')).toHaveValue('X-Api-Key');
    expect(screen.getByLabelText('Header Value 1')).toHaveAttribute('placeholder', '***1234');
    expect(screen.getByLabelText('Header Value 1')).toHaveAttribute(
      'aria-describedby',
      'gateway-route-header-value-1-hint gateway-route-service-key-help',
    );
    expect(document.getElementById('gateway-route-header-value-1-hint')).toHaveTextContent(
      'Leave empty to keep current',
    );
    expect(screen.getByLabelText('Header Name 2')).toHaveValue('Authorization');
    expect(screen.getByLabelText('Header Value 2')).toHaveAttribute('placeholder', '***5678');

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

  it('treats a renamed legacy key as new and submits its fresh secret', async () => {
    vi.mocked(useParams).mockReturnValue({ id: 'route-1' });
    const route = makeGatewayRoute({
      service_keys: undefined,
      service_key: { header_name: 'X-Legacy', header_value: '***9999' },
    });
    mockedGetGatewayRoute.mockResolvedValue(route);
    mockedGetGatewayUpstreams.mockResolvedValue({ items: [makeGatewayUpstream()], total: 1 });
    mockedSaveGatewayRoute.mockResolvedValue(route);
    const user = userEvent.setup();
    renderWithProviders(<GatewayRouteForm />);
    const headerName = await screen.findByRole('textbox', { name: 'Header Name 1' });

    await user.clear(headerName);
    await user.type(headerName, 'X-Renamed');
    const headerValue = screen.getByLabelText('Header Value 1');
    expect(headerValue).toHaveAttribute('placeholder', 'Bearer sk-xxx...');
    expect(document.getElementById('gateway-route-header-value-1-hint')).not.toBeInTheDocument();
    await user.type(headerValue, 'replacement-secret');
    await user.click(screen.getByRole('button', { name: 'Update Route' }));
    await waitFor(() => expect(mockedSaveGatewayRoute).toHaveBeenCalledWith('route-1', expect.objectContaining({
      service_keys: [{ header_name: 'X-Renamed', header_value: 'replacement-secret' }],
    })));
  });
});
