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

import { screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { getGatewayUpstreams } from '../api/client';
import GatewayRouteForm from '../pages/GatewayRouteForm';
import { renderWithProviders, makeGatewayUpstream } from './helpers';

const mockedGetGatewayUpstreams = vi.mocked(getGatewayUpstreams);

describe('GatewayRouteForm', () => {
  beforeEach(() => {
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
});
