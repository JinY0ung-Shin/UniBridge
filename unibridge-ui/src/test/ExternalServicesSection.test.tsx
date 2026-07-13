vi.mock('../api/client', () => ({
  default: { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
  getExternalServices: vi.fn(),
  createExternalService: vi.fn(),
  updateExternalService: vi.fn(),
  deleteExternalService: vi.fn(),
  testExternalService: vi.fn(),
}));

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createExternalService,
  deleteExternalService,
  getExternalServices,
  testExternalService,
  updateExternalService,
} from '../api/client';
import ExternalServicesSection from '../components/ExternalServicesSection';
import { renderWithProviders } from './helpers';

const mockGetExternalServices = vi.mocked(getExternalServices);
const mockCreateExternalService = vi.mocked(createExternalService);
const mockUpdateExternalService = vi.mocked(updateExternalService);
const mockDeleteExternalService = vi.mocked(deleteExternalService);
const mockTestExternalService = vi.mocked(testExternalService);

function makeService(overrides = {}) {
  return {
    id: 4,
    name: 'orders-api',
    address: 'orders.internal:8080',
    metrics_path: '/metrics',
    scheme: 'https' as const,
    description: 'Orders backend',
    enabled: true,
    status: 'up' as const,
    ...overrides,
  };
}

describe('ExternalServicesSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetExternalServices.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders loading, empty, and query-error states', async () => {
    let resolveServices!: (value: ReturnType<typeof makeService>[]) => void;
    mockGetExternalServices.mockReturnValue(new Promise((resolve) => { resolveServices = resolve; }));
    const loading = renderWithProviders(<ExternalServicesSection />, { permissions: ['servers.read'] });
    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
    resolveServices([]);
    expect(await screen.findByText(/No external services registered/i)).toBeInTheDocument();
    loading.unmount();

    mockGetExternalServices.mockRejectedValueOnce(new Error('offline'));
    renderWithProviders(<ExternalServicesSection />, { permissions: ['servers.read'] });
    expect(await screen.findByRole('alert')).toHaveTextContent('An error occurred');
  });

  it('creates a normalized HTTPS service and restores the default metrics path', async () => {
    mockCreateExternalService.mockResolvedValue(makeService());
    const user = userEvent.setup();
    renderWithProviders(<ExternalServicesSection />, { permissions: ['servers.read', 'servers.write'] });

    await user.click(screen.getByRole('button', { name: '+ Add service' }));
    expect(screen.getByRole('textbox', { name: 'Service name' })).toHaveAttribute(
      'aria-describedby',
      'ext-svc-name-hint',
    );
    await user.type(screen.getByRole('textbox', { name: 'Service name' }), '  inventory-api  ');
    await user.type(screen.getByRole('textbox', { name: 'Service address' }), ' inventory:9000 ');
    await user.clear(screen.getByRole('textbox', { name: 'Metrics path' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Protocol' }), 'https');
    await user.type(screen.getByRole('textbox', { name: 'Description' }), ' inventory service ');
    await user.click(screen.getByRole('checkbox', { name: /Enabled/ }));
    await user.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mockCreateExternalService).toHaveBeenCalledWith({
      name: 'inventory-api',
      address: 'inventory:9000',
      metrics_path: '/metrics',
      scheme: 'https',
      description: 'inventory service',
      enabled: false,
    }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '+ Add service' })).not.toBeInTheDocument());
  });

  it('edits all mutable fields without sending the immutable service name', async () => {
    mockGetExternalServices.mockResolvedValue([makeService({ description: null, enabled: false })]);
    mockUpdateExternalService.mockResolvedValue(makeService());
    const user = userEvent.setup();
    renderWithProviders(<ExternalServicesSection />, { permissions: ['servers.read', 'servers.write'] });
    await screen.findByText('orders-api');

    await user.click(screen.getByRole('button', { name: 'Edit service orders-api' }));
    expect(screen.getByRole('textbox', { name: 'Service name' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Protocol' })).toHaveValue('https');
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveValue('');
    await user.clear(screen.getByRole('textbox', { name: 'Service address' }));
    await user.type(screen.getByRole('textbox', { name: 'Service address' }), ' orders-v2:9443 ');
    await user.clear(screen.getByRole('textbox', { name: 'Metrics path' }));
    await user.type(screen.getByRole('textbox', { name: 'Metrics path' }), ' /internal/metrics ');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Protocol' }), 'http');
    await user.click(screen.getByRole('checkbox', { name: /Enabled/ }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(mockUpdateExternalService).toHaveBeenCalledWith(4, {
      address: 'orders-v2:9443',
      metrics_path: '/internal/metrics',
      scheme: 'http',
      description: '',
      enabled: true,
    }));
  });

  it('shows backend save details and keeps the failed form open', async () => {
    mockCreateExternalService.mockRejectedValue({
      isAxiosError: true,
      response: { data: { detail: 'Address must be host:port' } },
    });
    const user = userEvent.setup();
    renderWithProviders(<ExternalServicesSection />, { permissions: ['servers.read', 'servers.write'] });
    await user.click(screen.getByRole('button', { name: '+ Add service' }));
    await user.type(screen.getByRole('textbox', { name: 'Service name' }), 'bad');
    await user.type(screen.getByRole('textbox', { name: 'Service address' }), 'bad');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    expect(await screen.findByText('Address must be host:port')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: '+ Add service' })).toBeInTheDocument();
    expect(screen.getAllByRole('alert').some((alert) => alert.textContent?.includes('Failed to save service'))).toBe(true);
  });

  it('tests up and down services and renders status/address fallbacks', async () => {
    mockGetExternalServices.mockResolvedValue([
      makeService(),
      makeService({
        id: 5,
        name: 'legacy-api',
        scheme: undefined,
        status: 'down',
        description: null,
      }),
      makeService({ id: 6, name: 'disabled-api', enabled: false, status: null }),
    ]);
    mockTestExternalService
      .mockResolvedValueOnce({ status: 'up', detail: 'scrape succeeded' })
      .mockResolvedValueOnce({ status: 'down', detail: 'connection refused' });
    const user = userEvent.setup();
    renderWithProviders(<ExternalServicesSection />, { permissions: ['servers.read'] });
    await screen.findByText('orders-api');

    expect(screen.getByText('http://orders.internal:8080')).toBeInTheDocument();
    expect(screen.getByText('disabled')).toHaveClass('status-badge--unknown');
    await user.click(screen.getByRole('button', { name: 'Test service orders-api' }));
    expect(await screen.findByText('scrape succeeded')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Test service legacy-api' }));
    expect(await screen.findByText('connection refused')).toBeInTheDocument();
    expect(mockTestExternalService).toHaveBeenNthCalledWith(1, 4);
    expect(mockTestExternalService).toHaveBeenNthCalledWith(2, 5);
  });

  it('honors delete confirmation and surfaces delete failures', async () => {
    mockGetExternalServices.mockResolvedValue([makeService()]);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    mockDeleteExternalService.mockRejectedValueOnce({
      isAxiosError: true,
      response: { data: { detail: 'Service is referenced by a dashboard' } },
    });
    const user = userEvent.setup();
    renderWithProviders(<ExternalServicesSection />, { permissions: ['servers.read', 'servers.write'] });
    await screen.findByText('orders-api');

    await user.click(screen.getByRole('button', { name: 'Delete service orders-api' }));
    expect(mockDeleteExternalService).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Delete service orders-api' }));
    await waitFor(() => expect(mockDeleteExternalService).toHaveBeenCalledWith(4));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(await screen.findByText('Service is referenced by a dashboard')).toBeInTheDocument();
  });

  it('hides registry mutations from read-only users', async () => {
    mockGetExternalServices.mockResolvedValue([makeService()]);
    renderWithProviders(<ExternalServicesSection />, { permissions: ['servers.read'] });
    await screen.findByText('orders-api');
    expect(screen.queryByRole('button', { name: '+ Add service' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit service orders-api' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete service orders-api' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Test service orders-api' })).toBeInTheDocument();
  });
});
